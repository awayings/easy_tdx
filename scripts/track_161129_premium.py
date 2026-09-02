"""161129 原油LOF 每日溢价跟踪 / 预警 / 图表（方案 A：T-1 市场口径）。

口径与时效
----------
- 溢价口径:  premium_t = close_t / NAV_{t-1} - 1（%），与交易所盘中 IOPV 显示的
            溢价一致（实测该基金 IOPV = 前一交易日净值，基金公司内部净值提前供给）。
- 时效:      QDII 净值 NAV_d 于 d+1 晚约 22:00 披露 → 交易日 t 当晚 22:30 运行本脚本
             即可得出当日完整溢价并触发预警，次日开盘执行，与回测 next_open 一致。
- 阈值:      按 T-1 口径网格回测校准的最优值（买<=2.0% / 卖>=8.0%，+40.1%），
             与 scripts/backtest_161129_premium.py 的策略同源。

频次 / 防封
-----------
- 每次运行 1 次 TDX 请求（增量 15 根 K 线）+ 1 次天天基金请求（净值全量）
- 数据缓存于 ~/.easy_tdx/cache/161129_premium_daily.csv（与回测脚本共用）

用法::

    .venv/bin/python scripts/track_161129_premium.py            # 收盘后：更新/预警/出图
    .venv/bin/python scripts/track_161129_premium.py --chart    # 同时打开图表 HTML
    .venv/bin/python scripts/track_161129_premium.py --live     # 盘中轮询（30s，收盘自停）
    .venv/bin/python scripts/track_161129_premium.py --buy-th 1.5 --sell-th 6 --no-notify

通知（钉钉机器人 webhook，未配置时退回 macOS 桌面通知）::

    export CUSTOM_WEBHOOK_URLS="https://oapi.dingtalk.com/robot/send?access_token=..."
    # 或写入 ~/.easy_tdx/custom_webhook_urls（每行一个，# 注释），cron 环境适用

调度（macOS launchd，每交易日 22:30）::

    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>22</integer><key>Minute</key><integer>30</integer></dict>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from easy_tdx.notify import notify

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_161129_premium as bt  # noqa: E402  — 复用取数/策略/引擎配置

CHART_HTML = Path.home() / ".easy_tdx" / "cache" / f"{bt.CODE}_premium_track.html"

DEFAULT_BUY_TH, DEFAULT_SELL_TH = 2.0, 8.0  # T-1 口径网格最优（见模块 docstring）
LIVE_POLL_SEC = 30


# ── 数据：增量更新（1 次 TDX + 1 次净值请求/天） ───────────────────────────────


def update_dataset(refresh: bool = False) -> pd.DataFrame:
    """缓存数据集增量更新：新 K 线追加、净值回填（当晚披露后自动补齐 T-1 溢价）。"""
    try:
        df = bt.build_dataset(refresh=refresh)
    except FileNotFoundError:
        df = bt.build_dataset(refresh=True)

    fresh = bt.fetch_klines().sort_values("datetime").reset_index(drop=True)
    nav = bt.fetch_nav_series()
    last_dt = df["datetime"].iloc[-1] if len(df) else None
    new_rows = fresh[fresh["datetime"] > last_dt] if last_dt is not None else fresh
    if len(new_rows):
        df = pd.concat([df, new_rows], ignore_index=True).sort_values("datetime").reset_index(drop=True)

    # 净值回填：新披露的 NAV（含回填此前缺失的日期）→ 重算两列溢价（向量化，全序列）
    df["nav"] = df["datetime"].dt.date.map(nav)
    df["premium"] = (df["close"] / df["nav"].shift(bt.DISCLOSURE_LAG) - 1) * 100
    df["premium_lookahead"] = (df["close"] / df["nav"].shift(1) - 1) * 100
    df.to_csv(bt.CACHE, index=False)
    return df


def t1_dataset(df: pd.DataFrame) -> pd.DataFrame:
    """T-1 市场口径数据集（dropna 后供引擎回放；最后一根 bar 溢价可能待披露）。"""
    d = df[df["datetime"] >= pd.Timestamp(bt.BACKTEST_START)].reset_index(drop=True)
    return d.drop(columns=["premium"]).rename(columns={"premium_lookahead": "premium"})


# ── 信号 / 持仓状态（引擎回放，与回测口径一致） ──────────────────────────────


def replay_trades(data: pd.DataFrame, buy_th: float, sell_th: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按阈值回放策略，返回 (成交记录, 完整数据集)。当前持仓 = 成交净头寸。"""
    opt_df = data.dropna(subset=["premium"]).reset_index(drop=True)
    perf, trades = bt.run_backtest(opt_df, buy_th, sell_th)
    return trades, opt_df


def current_position(trades: pd.DataFrame) -> int:
    """1=持仓，0=空仓（BUY 记 +1，SELL 记 -1）。"""
    if not len(trades):
        return 0
    return int((trades["direction"] == "BUY").sum() - (trades["direction"] == "SELL").sum())


# ── 图表（单文件 HTML + ECharts，与 web-ui 同技术栈） ─────────────────────────


def write_chart(
    data: pd.DataFrame,
    trades: pd.DataFrame,
    buy_th: float,
    sell_th: float,
    path: Path,
) -> None:
    d = data.dropna(subset=["premium"])
    dates = [str(x.date()) for x in d["datetime"]]
    prices = [round(float(x), 4) for x in d["close"]]
    prem = [round(float(x), 2) for x in d["premium"]]

    def marks(direction: str) -> list[dict]:
        rows = trades[trades["direction"] == direction] if len(trades) else trades
        return [
            {"coord": [str(pd.Timestamp(r.datetime).date()), round(float(r.price), 4)]}
            for _, r in rows.iterrows()
        ]

    payload = json.dumps(
        {"dates": dates, "prices": prices, "premium": prem,
         "buys": marks("BUY"), "sells": marks("SELL"),
         "buy_th": buy_th, "sell_th": sell_th},
        ensure_ascii=False,
    )
    html = _CHART_TEMPLATE.replace("__PAYLOAD__", payload) \
        .replace("__BUY_TH__", str(buy_th)).replace("__SELL_TH__", str(sell_th))
    path.write_text(html, encoding="utf-8")
    print(f"图表已生成: {path}")


_CHART_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>161129 原油LOF 溢价跟踪</title>
<script src="https://registry.npmmirror.com/echarts/5.4.3/files/dist/echarts.min.js"></script>
<style>body{margin:0;background:#fff}#c{width:100vw;height:100vh}</style></head>
<body><div id="c"></div>
<script>
const D = __PAYLOAD__;
const buyTh = __BUY_TH__, sellTh = __SELL_TH__;
const chart = echarts.init(document.getElementById('c'));
chart.setOption({
  title: {text: '161129 原油LOF易方达 — 溢价跟踪（T-1 市场口径）', left: 8, top: 6,
           subtext: '阈值: 买入≤' + buyTh + '% / 卖出≥' + sellTh + '%；价格=收盘，溢价=收盘价÷前日净值−1'},
  tooltip: {trigger: 'axis'},
  axisPointer: {link: [{xAxisIndex: 'all'}]},
  grid: [{left: 60, right: 30, top: 64, height: '42%'},
         {left: 60, right: 30, top: '58%', height: '30%'}],
  xAxis: [{type: 'category', data: D.dates, gridIndex: 0, boundaryGap: false},
          {type: 'category', data: D.dates, gridIndex: 1, boundaryGap: false}],
  yAxis: [{type: 'value', scale: true, gridIndex: 0, name: '价格'},
          {type: 'value', scale: true, gridIndex: 1, name: '溢价%'}],
  dataZoom: [{type: 'inside', xAxisIndex: [0, 1], start: 30, end: 100},
             {type: 'slider', xAxisIndex: [0, 1], bottom: 4, start: 30, end: 100}],
  series: [
    {name: '收盘价', type: 'line', data: D.prices, symbol: 'none',
      markPoint: {data: [
        ...D.buys.map(p => ({coord: p.coord, symbol: 'triangle', symbolSize: 11,
          itemStyle: {color: '#d64545'}, label: {show: false}})),
        ...D.sells.map(p => ({coord: p.coord, symbol: 'pin', symbolSize: 11,
          itemStyle: {color: '#3a9d5d'}, label: {show: false}}))]}},
    {name: '溢价率', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: D.premium,
      symbol: 'none', lineStyle: {width: 1.5, color: '#4a7db8'},
      markLine: {symbol: 'none', data: [
        {yAxis: buyTh, name: '买入线', lineStyle: {color: '#d64545', type: 'dashed'},
          label: {formatter: '买入 ' + buyTh + '%', position: 'insideEndTop'}},
        {yAxis: sellTh, name: '卖出线', lineStyle: {color: '#3a9d5d', type: 'dashed'},
          label: {formatter: '卖出 ' + sellTh + '%', position: 'insideEndTop'}}]}}]
});
window.addEventListener('resize', () => chart.resize());
</script></body></html>"""


# ── 收盘后主流程 ──────────────────────────────────────────────────────────────


def daily_run(buy_th: float, sell_th: float, open_chart: bool, do_notify: bool) -> None:
    df = update_dataset(refresh="--refresh" in sys.argv)
    data = t1_dataset(df)

    trades, opt_df = replay_trades(data, buy_th, sell_th)
    pos = current_position(trades)
    latest = opt_df.iloc[-1]
    latest_date = str(latest["datetime"].date())
    prem_now = float(latest["premium"])
    last_bar_missing = bool(np.isnan(data.iloc[-1]["premium"]))

    print(f"== 161129 溢价跟踪（T-1 市场口径，买<={buy_th}% / 卖>={sell_th}%）==")
    print(f"最新完整 bar: {latest_date}  close={latest['close']:.3f}  溢价={prem_now:+.2f}%  持仓={'是' if pos else '否'}")
    if last_bar_missing:
        print(f"注意: 最新一根 {data['datetime'].iloc[-1].date()} 的 NAV 尚未披露（约当晚 22:00 后发布），届时重跑即可补齐")

    signal = ""
    if pos == 0 and prem_now <= buy_th:
        signal = f"买入信号：溢价 {prem_now:+.2f}% <= {buy_th}%（次日开盘执行）"
    elif pos > 0 and prem_now >= sell_th:
        signal = f"卖出信号：溢价 {prem_now:+.2f}% >= {sell_th}%（次日开盘执行）"
    elif pos > 0 and prem_now <= buy_th:
        signal = f"持有中：溢价回落至 {prem_now:+.2f}%（未触发卖出）"
    print(signal or f"无信号：溢价 {prem_now:+.2f}% 在阈值区间内")

    # 仅可执行的买卖信号发桌面通知（“持有中”只是信息性提示）
    if do_notify and signal.startswith(("买入", "卖出")):
        notify("原油LOF 161129 预警", signal)

    write_chart(opt_df, trades, buy_th, sell_th, CHART_HTML)
    if open_chart:
        subprocess.run(["open", str(CHART_HTML)], check=False)


# ── 盘中轮询（近似口径：IOPV 字段优先，未推送则降级昨IOPV） ──────────────────


def live_run(buy_th: float, sell_th: float, do_notify: bool) -> None:
    from easy_tdx import MacClient, Market
    from easy_tdx.codec.bitmap import FieldBit, PresetField

    # 起始持仓：按最新数据回放（与收盘主流程同口径）
    trades, _ = replay_trades(t1_dataset(update_dataset()), buy_th, sell_th)
    pos = current_position(trades)

    fields = PresetField.COMMON + FieldBit.IOPV
    last_msg = ""
    with MacClient.from_best_host() as c:
        while True:
            now = time.localtime()
            hm = now.tm_hour * 100 + now.tm_min
            if hm > 1505:  # 收盘后自动停止
                print("已收盘，退出。")
                break
            row = c.get_stock_quotes([(Market.SZ, bt.CODE)], fields=fields).iloc[0]
            price, iopv, pre_iopv = float(row["close"]), float(row["iopv"]), float(row["pre_iopv"])
            if iopv > 0:
                anchor, tag = iopv, "实时IOPV(T-1口径)"
            else:
                anchor, tag = pre_iopv, "昨IOPV(近似口径)"
            prem = (price / anchor - 1) * 100 if anchor > 0 else float("nan")
            msg = (f"{now.tm_hour:02d}:{now.tm_min:02d} 持仓={'是' if pos else '否'} "
                   f"{price:.3f} 溢价={prem:+.2f}% [{tag}]")
            if msg != last_msg:  # 仅变化时打印/评估，减少噪音
                last_msg = msg
                print(msg)
                if do_notify and not np.isnan(prem) and (
                    (pos == 0 and prem <= buy_th) or (pos > 0 and prem >= sell_th)
                ):
                    notify("原油LOF 盘中溢价", f"现价 {price:.3f} 溢价 {prem:+.2f}%（{tag}）")
            time.sleep(LIVE_POLL_SEC)


def main() -> None:
    args = sys.argv[1:]
    buy_th = float(args[args.index("--buy-th") + 1]) if "--buy-th" in args else DEFAULT_BUY_TH
    sell_th = float(args[args.index("--sell-th") + 1]) if "--sell-th" in args else DEFAULT_SELL_TH
    do_notify = "--no-notify" not in args
    if "--live" in args:
        live_run(buy_th, sell_th, do_notify)
    else:
        daily_run(buy_th, sell_th, "--chart" in args, do_notify)


if __name__ == "__main__":
    main()
