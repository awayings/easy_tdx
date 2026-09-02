"""交易计划生成器（docs/trading_system_tasks.md P2-T3）。

"念动想买入"时一条命令出计划：``easy-tdx plan 002594``。

数据组装流程（全部 easy_tdx 原生 + 东财免费公开接口，无 tushare）：

1. 日线：本地 .day（`~/new_tdx/vipdoc`）优先，缺失时 fallback TDX 协议
   （MAC kline，800 根/页自动翻页）。
2. 盘中参考价：MAC 实时报价（交易时段内），收盘后自动退回最新收盘。
3. 市场整体情绪：TDX market-stat 宽度 + 上证/深市指数日线 → 0-30 风险分。
4. 个股公共舆情：东财人气榜排名 + 个股新闻情绪（离线时自动降级）。
5. 宏观特征快照：本地特征库（launchd 23:00 同步，纯本地读）。

以上全部失败也不阻断——最坏情况退化为"纯技术面"计划（技术分 + 止损
止盈点位照常输出）。计划结构见 `plan.build_plan` 文档。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .plan import build_plan

__all__ = ["build_full_plan", "build_plan", "load_daily_df", "parse_symbol"]


def parse_symbol(symbol: str) -> tuple[str, str]:
    """解析标的代码 → (market, code)。

    支持 ``"SZ 002594"`` / ``"SZ002594"`` / ``"002594"``（裸代码按股票池约定：
    6xx/68x/9xx 归沪，其余归深）。
    """
    s = symbol.strip().upper().replace(" ", "")
    if len(s) > 6:
        market, code = s[:2], s[2:8]
    else:
        code = s
        market = "SH" if (code.startswith(("6", "9")) or code.startswith("68")) else "SZ"
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"无法解析标的代码: {symbol!r}")
    return market, code


def _market_int(market: str) -> int:
    from easy_tdx.models.enums import Market

    return int(Market[market])


def load_daily_df(market: str, code: str) -> pd.DataFrame:
    """加载日线 DataFrame（datetime/open/high/low/close/vol/amount）。

    本地 .day 优先；文件缺失或数据 <30 根时 fallback TDX 协议（MAC kline）。
    两者都失败抛 ValueError。
    """
    from easy_tdx.exceptions import TdxFileNotFoundError, TdxOfflineError
    from easy_tdx.offline.daily_bar import find_daily_bar_file, read_daily_bars

    bars = []
    try:
        fp = find_daily_bar_file(_market_int(market), code)
        bars = read_daily_bars(fp)
    except (TdxFileNotFoundError, TdxOfflineError):
        bars = []

    if len(bars) >= 30:
        return pd.DataFrame(
            [
                {
                    "datetime": pd.Timestamp(b.year, b.month, b.day),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "vol": b.vol,
                    "amount": b.amount,
                }
                for b in bars
            ]
        )

    # fallback：TDX 协议日线（MAC，800 根）
    from easy_tdx.mac.client import MacClient
    from easy_tdx.mac.enums import Period

    c = MacClient.from_best_host()
    try:
        c.connect()
        df = c.get_stock_kline(_market_int(market), code, period=Period.DAILY, count=800)
    finally:
        try:
            c.close()
        except Exception:
            pass
    if df.empty:
        raise ValueError(f"无日线数据（本地 .day 缺失且协议拉取失败）: {market} {code}")
    return df


def fetch_realtime_quote(market: str, code: str) -> dict | None:
    """盘中实时报价（MAC）。

    Returns: {price, change_pct, high, low, name}；失败/非交易时段返回 None。
    """
    from easy_tdx.mac.client import MacClient

    c = MacClient.from_best_host()
    try:
        c.connect()
        df = c.get_stock_quotes([(_market_int(market), code)])
        if df.empty:
            return None
        row = df.iloc[0]
        price = float(row["close"])
        pre = row.get("pre_close")
        change_pct = round((price / float(pre) - 1) * 100, 2) if pre and np.isfinite(pre) else None
        if not np.isfinite(price) or price <= 0:
            return None
        return {
            "price": round(price, 2),
            "change_pct": change_pct,
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "name": str(row.get("name") or ""),
        }
    except Exception:
        return None
    finally:
        try:
            c.close()
        except Exception:
            pass


def build_full_plan(
    market: str,
    code: str,
    *,
    base_position: float = 0.10,
    with_public: bool = True,
    with_features: bool = True,
    market_score: dict | None = None,
) -> dict:
    """端到端交易计划：日线 + 市场情绪 + 舆情 + 宏观特征 → 计划 dict。

    Args:
        market: SZ/SH。
        code: 6 位代码。
        base_position: 基础仓位比例（默认 10%）。
        with_public: False 时跳过舆情抓取（离线/测试用）。
        with_features: False 时跳过特征快照。
        market_score: 预取的市场评分（批量模式复用，避免每股重复请求）。

    Returns:
        build_plan() 输出；日线加载失败返回 {"symbol": ..., "error": ...}。
    """
    symbol = f"{market} {code}"
    try:
        df = load_daily_df(market, code)
    except Exception as e:  # noqa: BLE001 - 数据加载失败统一降级为错误计划
        return {"symbol": symbol, "error": str(e)}

    from easy_tdx import features as feature_lib
    from easy_tdx.public_sentiment import analyze_stock_public
    from easy_tdx.sentiment import fetch_breadth, fetch_index_bars, score_market

    # 盘中参考价（收盘后报价与收盘一致，无副作用）
    entry_ref = None
    quote = fetch_realtime_quote(market, code)
    if quote:
        entry_ref = quote["price"]
        last = df.iloc[-1]
        # 盘中：追加一根当日进行中 bar（真实日内高低点、vol=0），让技术状态/
        # 止损止盈锚定实时价；收盘后 .day 已含当日 bar（收盘价=报价），自动跳过。
        today = pd.Timestamp.now().normalize()
        if entry_ref != last["close"] and last["datetime"] < today:
            df = pd.concat(
                [
                    df,
                    pd.DataFrame(
                        [
                            {
                                "datetime": today,
                                "open": quote.get("open", entry_ref),
                                "high": quote.get("high", entry_ref),
                                "low": quote.get("low", entry_ref),
                                "close": entry_ref,
                                "vol": 0.0,
                                "amount": 0.0,
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )

    if market_score is None:
        breadth = fetch_breadth()
        index_bars = fetch_index_bars(count=60)
        market_score = score_market(breadth, index_bars)

    public = analyze_stock_public(market, code) if with_public else None
    features = feature_lib.snapshot() if with_features else None

    return build_plan(
        df,
        symbol=symbol,
        name=(quote or {}).get("name", ""),
        entry_ref=entry_ref,
        entry_ref_is_realtime=bool(quote),
        market_score=market_score,
        public=public,
        features=features,
        base_position=base_position,
    )
