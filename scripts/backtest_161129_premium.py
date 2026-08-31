"""161129 原油LOF 低溢价轮动策略回测（复用 easy_tdx 内置回测引擎 + 参数网格寻优器）。

数据链路
--------
- 价格日线:  TDX MAC 协议（easy_tdx MacClient），单次请求 400 根（单页上限 700，未触发翻页）
- 净值历史:  天天基金 pingzhongdata 单请求全量（官方单位净值）。
            与行情 pre_iopv 字段实测一致：2026-08-31 盘中 pre_iopv=1.6928
            == NAV(2026-08-27)。即该基金 IOPV = 最新已披露单位净值。
- 溢价口径:  premium_t = close_t / NAV_{t-DISCLOSURE_LAG} - 1（%）
            QDII 净值 T+1 晚间披露 → t 收盘时最新"已知"净值是 T-2 交易日的，
            用 T-1 会在 t 收盘偷看当晚才披露的净值（未来函数）。
            premium_lookahead（T-1 对照列）保留用于量化这个滞后差。

策略
----
- 入场: 溢价 <= buy_threshold（差值小，便宜时买入）
- 出场: 溢价 >= sell_threshold（回归到高溢价，落袋）
- 成交: next_open（t 收盘生成信号，t+1 开盘成交）；LOF 免印花税 stamp_tax=0
- 策略注册进内置注册表（ParametrizedStrategy + @register_strategy），
  参数扫描直接复用库的 ParamGridOptimizer（含热力图与 200 点上限保护）。

频次 / 防封
-----------
- TDX: 每次全新取数仅 1 个请求；数据缓存于 ~/.easy_tdx/cache，重复运行不再访问行情服务器
- 天天基金: 1 个请求，同样随缓存复用
- 加 --refresh 强制重新取数

用法::

    .venv/bin/python scripts/backtest_161129_premium.py            # 主回测 + 参数扫描
    .venv/bin/python scripts/backtest_161129_premium.py --refresh  # 强制重新取数
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from easy_tdx import MacClient, Market, Period
from easy_tdx.backtest import BacktestEngine
from easy_tdx.backtest.optimizer import ParamGridOptimizer
from easy_tdx.backtest.strategies import Param, ParametrizedStrategy, register_strategy, resolve

CODE = "161129"
NAV_URL = f"https://fund.eastmoney.com/pingzhongdata/{CODE}.js"
CACHE_DIR = Path.home() / ".easy_tdx" / "cache"
CACHE = CACHE_DIR / f"{CODE}_premium_daily.csv"
BACKTEST_START = "2026-01-01"

# QDII 净值披露滞后：NAV_d 于 d+1 交易日晚间披露 → t 收盘时最新已知净值是
# T-2 交易日的（国内普通基金为 T-1，对应 premium_lookahead 对照列的口径）。
DISCLOSURE_LAG = 2

DEFAULT_BUY, DEFAULT_SELL = 1.0, 4.0  # 主回测阈值（%）
BUYS = [0.0, 0.5, 1.0, 1.5, 2.0]     # 参数扫描：买入阈值网格
SELLS = [3.0, 4.0, 5.0, 6.0, 8.0]    # 参数扫描：卖出阈值网格


# ── 数据获取（各 1 次请求，结果缓存） ──────────────────────────────────────────


def fetch_nav_series() -> pd.Series:
    """官方单位净值历史（天天基金单文件接口，一次请求全量）。"""
    req = urllib.request.Request(
        NAV_URL,
        headers={
            "Referer": f"https://fund.eastmoney.com/{CODE}.html",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        },
    )
    text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text)
    if m is None:
        raise RuntimeError("pingzhongdata 解析失败（接口结构可能变更）")
    rows = json.loads(m.group(1))
    # x 为"北京时间零点"编码的 epoch 毫秒：按 UTC 解析会整体偏移 -1 天
    # （实测 NAV(08-27)=1.6928 会被标成 08-26），必须用 Asia/Shanghai 解析。
    s = pd.Series(
        {
            pd.Timestamp(r["x"], unit="ms", tz="Asia/Shanghai").date(): float(r["y"])
            for r in rows
            if r["y"]
        }
    )
    return s.sort_index()


def fetch_klines() -> pd.DataFrame:
    """TDX 日线（一次请求，400 根 < 单页 700 上限，不触发翻页）。"""
    with MacClient.from_best_host() as c:
        return c.get_stock_kline(Market.SZ, CODE, Period.DAILY, count=400)


def build_dataset(refresh: bool = False) -> pd.DataFrame:
    if not refresh and CACHE.exists():
        df = pd.read_csv(CACHE)
        df["datetime"] = pd.to_datetime(df["datetime"])
        return df

    k = fetch_klines().sort_values("datetime").reset_index(drop=True)
    nav = fetch_nav_series()
    k["nav"] = k["datetime"].dt.date.map(nav)
    # 收盘时可用的最新净值 = T-DISCLOSURE_LAG 交易日的净值（无未来函数）
    k["premium"] = (k["close"] / k["nav"].shift(DISCLOSURE_LAG) - 1) * 100
    # 参考列：T-1 口径（等价于当晚净值披露后才决策，存在一天信息超前，仅对照用）
    k["premium_lookahead"] = (k["close"] / k["nav"].shift(1) - 1) * 100
    k = k[k["datetime"] >= pd.Timestamp("2025-01-01")].reset_index(drop=True)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    k.to_csv(CACHE, index=False)
    return k


# ── 策略 ───────────────────────────────────────────────────────────────────────


@register_strategy(
    name="premium_rotation",
    label="低溢价轮动",
    description="溢价率低时买入、高时卖出（数据集需含 premium 列）",
)
class PremiumRotationStrategy(ParametrizedStrategy):
    """低溢价买入、高溢价卖出的 LOF 溢价轮动策略。

    入场条件即需求方所述「收盘价与 pre_iopv 差值小」：
    溢价率（%）低于 buy_threshold 时全仓买入，回归至 sell_threshold 以上清仓。
    """

    params = [
        Param("buy_threshold", float, default=DEFAULT_BUY, min_value=-5.0, max_value=15.0,
              label="买入阈值（%）"),
        Param("sell_threshold", float, default=DEFAULT_SELL, min_value=0.0, max_value=50.0,
              label="卖出阈值（%）"),
    ]
    param_constraints = [("buy_threshold", "sell_threshold")]

    def init(self) -> None:
        pass

    def next(self) -> None:
        prem = self.data.premium[0]
        if self.position["size"] == 0 and prem <= self.p["buy_threshold"]:
            self.buy(size=0)
        elif self.position["size"] > 0 and prem >= self.p["sell_threshold"]:
            self.sell(size=0)


# ── 回测工具 ───────────────────────────────────────────────────────────────────

_ENGINE_KWARGS = dict(
    cash=100_000,
    commission=0.0003,  # 场内 LOF 佣金万三
    min_commission=5.0,
    stamp_tax=0.0,  # 场内基金免印花税
    slippage=0.001,  # 0.1% 滑点（1.7 元价位约 1.7 分/股）
    execution="next_open",
)


def run_backtest(df: pd.DataFrame, buy_threshold: float, sell_threshold: float):
    """按给定阈值跑一次回测，返回 (performance dict, 成交记录 DataFrame)。

    供主回测取完整绩效（含年化）与成交明细；网格扫描走 ParamGridOptimizer。
    """
    strategy = resolve("premium_rotation").build(
        {"buy_threshold": buy_threshold, "sell_threshold": sell_threshold}
    )
    result = BacktestEngine(strategy=strategy, **_ENGINE_KWARGS).run(df)
    return result.performance, result.trades


# ── 主流程 ─────────────────────────────────────────────────────────────────────


def main() -> None:
    refresh = "--refresh" in sys.argv
    print(f"== 数据准备（缓存: {CACHE}，refresh={refresh}）==")
    df = build_dataset(refresh=refresh)

    df26 = df[df["datetime"] >= pd.Timestamp(BACKTEST_START)].reset_index(drop=True)
    if df26.empty:
        raise RuntimeError("2026 年数据为空")
    print(f"回测区间: {df26['datetime'].iloc[0].date()} ~ {df26['datetime'].iloc[-1].date()}，共 {len(df26)} 根日线")
    print(f"溢价序列统计: min={df26['premium'].min():.2f}%  中位={df26['premium'].median():.2f}%  "
          f"max={df26['premium'].max():.2f}%（T-1 对照中位={df26['premium_lookahead'].median():.2f}%）")
    print(f"溢价 NaN 数: {int(df26['premium'].isna().sum())}")
    print(f"最新一根: {df26['datetime'].iloc[-1].date()}  close={df26['close'].iloc[-1]:.3f}  "
          f"premium={df26['premium'].iloc[-1]:.2f}%（锚定 NAV={df26['nav'].iloc[-1 - DISCLOSURE_LAG]:.4f}）\n")

    # 基准：2026 年首个开盘价买入持有至今（含回撤/夏普，口径与引擎一致）
    bh_close = df26["close"]
    bh = bh_close.iloc[-1] / df26["open"].iloc[0] - 1
    bh_dd = float((bh_close / bh_close.cummax() - 1).min())
    bh_ret = bh_close.pct_change().dropna()
    bh_sharpe = float(bh_ret.mean() / bh_ret.std() * np.sqrt(252)) if bh_ret.std() > 0 else 0.0
    print(f"基准（2026 年初开盘买入持有）: 总收益 {bh:+.2%}  回撤 {bh_dd:.2%}  夏普 {bh_sharpe:.2f}\n")

    # 主回测（默认参数）：完整绩效 + 成交明细
    perf, trades = run_backtest(df26, DEFAULT_BUY, DEFAULT_SELL)
    print(f"== 主回测（买入溢价<={DEFAULT_BUY}%，卖出溢价>={DEFAULT_SELL}%）==")
    for label, key, fmt in [
        ("总收益率", "total_return", ".2%"),
        ("年化收益", "annual_return", ".2%"),
        ("最大回撤", "max_drawdown", ".2%"),
        ("夏普", "sharpe", ".2f"),
        ("胜率", "win_rate", ".1%"),
        ("盈亏比", "profit_factor", ".2f"),
        ("交易次数", "total_trades", ".0f"),
    ]:
        print(f"  {label:8s} = {perf.get(key, 0.0):{fmt}}")
    if len(trades):
        print("\n成交记录:")
        cols = [c for c in ["datetime", "direction", "price", "size", "pnl", "commission"] if c in trades.columns]
        print(trades[cols].to_string(index=False))

    # 参数扫描：复用库的 ParamGridOptimizer（网格点上限保护 + 语义约束 + 单点容错）
    print(f"\n== 参数扫描（{len(BUYS) * len(SELLS)} 个组合，ParamGridOptimizer）==")
    opt = ParamGridOptimizer(
        strategy_name="premium_rotation",
        # 键序决定热力图取向：x=卖出阈值（列）、y=买入阈值（行）
        param_grid={"sell_threshold": SELLS, "buy_threshold": BUYS},
        df=df26,
        **_ENGINE_KWARGS,
    ).run()

    hm = opt.heatmap
    if hm:
        x_vals, y_vals = hm["x"], hm["y"]
        cell = {(xi, yi): v for xi, yi, v in hm["data"]}
        print("  行=买入阈值 / 列=卖出阈值（单元格=总收益率%）:")
        print(f"  {'buy\\sell':>8}" + "".join(f"{xv:>9.1f}" for xv in x_vals))
        for yi, yv in enumerate(y_vals):
            row = f"  {yv:>8.1f}" + "".join(f"{cell[(xi, yi)] * 100:>9.2f}" for xi in range(len(x_vals)))
            print(row)

    print("\n逐组合明细（按总收益率降序）:")
    print(f"  {'买入阈%':>6} {'卖出阈%':>6} {'总收益%':>8} {'回撤%':>7} {'夏普':>6} "
          f"{'胜率%':>7} {'交易':>4} {'盈亏比':>6}")
    for r in opt.results:
        pb, ps = r.params["buy_threshold"], r.params["sell_threshold"]
        print(f"  {pb:>6.1f} {ps:>6.1f} {r.total_return * 100:>8.2f} {r.max_drawdown * 100:>7.2f} "
              f"{r.sharpe:>6.2f} {r.win_rate * 100:>7.1f} {r.total_trades:>4} {r.profit_factor:>6.2f}")

    if opt.best:
        b = opt.best
        print(f"\n最优组合: 买入<= {b.params['buy_threshold']:.1f}% / 卖出>= {b.params['sell_threshold']:.1f}% "
              f"→ 总收益 {b.total_return:+.2%}，回撤 {b.max_drawdown:.2%}，"
              f"交易 {b.total_trades} 次（基准持有 {bh:+.2%}）")


if __name__ == "__main__":
    main()
