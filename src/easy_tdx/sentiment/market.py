"""市场整体情绪数据与评分（P0-T1/P0-T2）。

- ``fetch_breadth``：TDX market-stat 全市场宽度（涨跌家数、涨停/跌停、两市成交额）。
  880005/880001/880006 部分主机不提供，`get_market_stat` 已内置逐台 fallback。
- ``fetch_index_bars``：上证指数 + 深市全市场指数（399107.SZ，勿用 399001.SZ——
  只含成分股、口径偏小）日线，用于 MA20 趋势与两市成交额（amount 单位=元）。
- ``fetch_board_ranking``：MAC 板块资金/涨跌幅排行（P0-T2）。
- ``score_market``：市场风险分 0-30（越高越不宜买入）+ 情绪标签（偏多/中性/偏空），
  交易计划生成器把该分并入风险评分。
- ``build_snapshot``：组装日频情绪快照 dict（含特征库 snapshot 与评分）。

所有 fetch 函数失败时返回 None（优雅降级，不抛异常），调用方自行处理缺失。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ---- 评分阈值（P0-T1 硬编码常量，见 docs/trading_system_tasks.md）----
TURNOVER_LOW = 8000e8  # 两市成交额（元）低于此视为流动性不足
TURNOVER_HOT = 25000e8  # 高于此视为情绪过热
LIMIT_UP_ACTIVE = 100  # 涨停家数高于此视为赚钱效应强
LIMIT_DOWN_PANIC = 10  # 跌停家数高于此视为恐慌
BREADTH_BEARISH = 0.7  # 下跌家数占比高于此偏空
BREADTH_BULLISH = 0.45  # 低于此偏多


def _round2(x: float | None) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 2)


# ---------------------------------------------------------------------------
# 数据抓取（TDX 原生，均带失败降级）
# ---------------------------------------------------------------------------


def fetch_breadth(client=None) -> dict | None:
    """全市场宽度快照（TDX market-stat）。client 为已连接的 TdxClient（测试注入）。"""
    from easy_tdx.client import TdxClient

    try:
        if client is None:
            with TdxClient.from_best_host() as c:
                df = c.get_market_stat()
        else:
            df = client.get_market_stat()
        row = df.iloc[0]
        return {
            "up_count": int(row["up_count"]),
            "down_count": int(row["down_count"]),
            "neutral_count": int(row["neutral_count"]),
            "suspended_count": int(row["suspended_count"]),
            "total_count": int(row["total_count"]),
            "limit_up_count": int(row["limit_up_count"]),
            "limit_down_count": int(row["limit_down_count"]),
            "total_amount": float(row["total_amount"]),  # 元
            "total_volume": float(row["total_volume"]),  # 手
        }
    except Exception:
        return None


def fetch_index_bars(client=None, count: int = 60) -> dict | None:
    """上证指数（SH 000001）与深市全市场指数（SZ 399107）日线（MAC 协议）。

    Returns:
        {sh: DataFrame, sz: DataFrame}；失败返回 None。
    """
    from easy_tdx.mac.client import MacClient
    from easy_tdx.mac.enums import Period

    try:
        if client is None:
            c = MacClient.from_best_host()
            c.connect()
        else:
            c = client
        sh = c.get_stock_kline(1, "000001", period=Period.DAILY, start=0, count=count)
        sz = c.get_stock_kline(0, "399107", period=Period.DAILY, start=0, count=count)
        if sh.empty and sz.empty:
            return None
        return {"sh": sh, "sz": sz}
    except Exception:
        return None
    finally:
        if client is None and "c" in locals():
            try:
                c.close()
            except Exception:
                pass


def fetch_board_ranking(
    client=None,
    board_type: str = "HY",
    top_n: int = 10,
) -> dict | None:
    """板块排行（P0-T2）：涨跌幅榜、主力净流入榜、主力净流出榜。

    ``get_board_ranking`` 内部对每个板块发一次 board-summary，故只请求一次
    （top_n 个板块），四个榜单在本地 DataFrame 上排序（避免 ~4×top_n 次请求）。

    Returns:
        {gainers, losers, inflow, outflow}，每项为记录列表；失败返回 None。
    """
    from easy_tdx.mac.client import MacClient
    from easy_tdx.mac.enums import BoardType

    bt = BoardType.HY if board_type.upper() in ("HY", "1") else BoardType.GN
    cols = ("name", "code", "change_pct", "amount", "main_net_amount")

    def _top(df: pd.DataFrame, by: str, asc: bool, n: int) -> list[dict]:
        if df.empty:
            return []
        use = [c for c in cols if c in df.columns]
        if by in df.columns:
            df = df.sort_values(by, ascending=asc)
        return df[use].head(n).to_dict("records")

    try:
        if client is None:
            c = MacClient.from_best_host()
            c.connect()
        else:
            c = client
        df = c.get_board_ranking(board_type=bt, top_n=max(top_n, 15))
        return {
            "gainers": _top(df, "change_pct", False, top_n),
            "losers": _top(df, "change_pct", True, top_n),
            "inflow": _top(df, "main_net_amount", False, top_n),
            "outflow": _top(df, "main_net_amount", True, top_n),
        }
    except Exception:
        return None
    finally:
        if client is None and "c" in locals():
            try:
                c.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 市场风险评分（0-30，越高越不宜买入）
# ---------------------------------------------------------------------------


def score_market(
    breadth: dict | None,
    index: dict | None = None,
) -> dict:
    """市场风险分 + 情绪标签。

    Args:
        breadth: fetch_breadth() 结果（None=无数据，宽度部分计 0 分）。
        index: fetch_index_bars() 结果（None=无数据，指数趋势部分计 0 分）。

    Returns:
        {score, label, factors: [{name, points, note}]}
    """
    factors: list[dict] = []
    score = 0.0

    if breadth:
        up = breadth["up_count"]
        down = breadth["down_count"]
        breadth_total = up + down
        down_ratio = down / breadth_total if breadth_total > 0 else 0.5

        if down_ratio >= BREADTH_BEARISH:
            points = 15.0
            note = f"下跌家数占比 {down_ratio:.0%}（>{BREADTH_BEARISH:.0%}）"
        elif down_ratio >= 0.6:
            points = 10.0
            note = f"下跌家数占比 {down_ratio:.0%}（60%-70%）"
        elif down_ratio >= 0.5:
            points = 6.0
            note = f"下跌家数占比 {down_ratio:.0%}（50%-60%）"
        else:
            points = 0.0
            note = f"上涨家数占比 {1 - down_ratio:.0%}（健康）"
        if points:
            factors.append({"name": "涨跌家数", "points": points, "note": note})

        if breadth["limit_up_count"] < 30:
            factors.append(
                {
                    "name": "涨停家数", "points": 5.0,
                    "note": f"涨停 {breadth['limit_up_count']} 家（<30，赚钱效应弱）",
                }
            )
            score += 5.0
        if breadth["limit_down_count"] >= LIMIT_DOWN_PANIC:
            factors.append(
                {
                    "name": "跌停家数", "points": 5.0,
                    "note": f"跌停 {breadth['limit_down_count']} 家（≥{LIMIT_DOWN_PANIC}，恐慌）",
                }
            )
            score += 5.0
        if breadth["limit_up_count"] > LIMIT_UP_ACTIVE and down_ratio < BREADTH_BULLISH:
            factors.append(
                {
                    "name": "情绪过热", "points": 3.0,
                    "note": f"涨停 {breadth['limit_up_count']} 家且普涨，注意亢奋回落",
                }
            )
            score += 3.0
        score += points

    # 两市成交额（上证 + 深市全市场 399107 的指数日线 amount，单位=元）
    turnover = None
    if index and not index["sh"].empty and not index["sz"].empty:
        sh_amt = float(index["sh"]["amount"].iloc[-1])
        sz_amt = float(index["sz"]["amount"].iloc[-1])
        if np.isfinite(sh_amt) and np.isfinite(sz_amt):
            turnover = sh_amt + sz_amt
            if turnover < TURNOVER_LOW:
                factors.append(
                    {
                        "name": "成交额", "points": 5.0,
                        "note": f"两市 {turnover / 1e8:.0f} 亿"
                        f"（<{TURNOVER_LOW / 1e8:.0f} 亿，流动性不足）",
                    }
                )
                score += 5.0
            elif turnover > TURNOVER_HOT:
                factors.append(
                    {
                        "name": "成交额", "points": 3.0,
                        "note": f"两市 {turnover / 1e8:.0f} 亿"
                        f"（>{TURNOVER_HOT / 1e8:.0f} 亿，情绪过热）",
                    }
                )
                score += 3.0

        # 上证指数 vs MA20（趋势破坏）
        sh = index["sh"]
        if len(sh) >= 20:
            close = float(sh["close"].iloc[-1])
            ma20 = float(sh["close"].rolling(20).mean().iloc[-1])
            if close < ma20:
                factors.append(
                    {
                        "name": "指数趋势", "points": 5.0,
                        "note": f"上证 {close:.0f} 位于 MA20（{ma20:.0f}）下方",
                    }
                )
                score += 5.0

    label = "偏空" if score >= 20 else ("中性" if score >= 10 else "偏多")
    return {
        "score": round(min(score, 30.0), 1),
        "label": label,
        "factors": factors,
        "turnover_2m": _round2(turnover) if turnover else None,
    }


# ---------------------------------------------------------------------------
# 日频情绪快照组装
# ---------------------------------------------------------------------------


def build_snapshot(
    breadth: dict | None = None,
    index: dict | None = None,
    board: dict | None = None,
    features: dict | None = None,
    date: str | None = None,
) -> dict:
    """组装 P0-T1 情绪快照 dict（各输入为 None 表示该项无数据）。

    Returns:
        {date, breadth, index, board_ranking, features, market_score}
        index 项压缩为 {sh_close, sh_ma20, sh_change_pct, turnover_2m}。
    """
    index_compact: dict | None = None
    if index and not index["sh"].empty:
        sh = index["sh"]
        close = float(sh["close"].iloc[-1])
        prev = float(sh["close"].iloc[-2]) if len(sh) >= 2 else close
        ma20 = float(sh["close"].rolling(20).mean().iloc[-1]) if len(sh) >= 20 else None
        index_compact = {
            "sh_close": _round2(close),
            "sh_ma20": _round2(ma20),
            "sh_change_pct": round((close / prev - 1) * 100, 2),
        }
    market_score = score_market(breadth, index)
    if index_compact is not None:
        index_compact["turnover_2m"] = market_score.get("turnover_2m")
    return {
        "date": date or pd.Timestamp.now().strftime("%Y-%m-%d"),
        "breadth": breadth,
        "index": index_compact,
        "board_ranking": board,
        "features": features,
        "market_score": market_score,
    }
