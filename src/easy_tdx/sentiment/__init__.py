"""市场整体情绪（docs/trading_system_tasks.md P0-T1/P0-T2）。

全市场宽度（涨跌家数/涨停跌停/成交额，TDX market-stat）、两市成交额
（上证 000001.SH + 深市全市场 399107.SZ 指数日线 amount）、板块资金榜
（MAC board-ranking，P0-T2），统一组装为情绪快照 + 0-30 市场风险分。
供 `scripts/market_sentiment.py` 日报与交易计划生成器（P2-T3）复用。
"""

from .market import (
    BREADTH_BEARISH,
    BREADTH_BULLISH,
    LIMIT_DOWN_PANIC,
    LIMIT_UP_ACTIVE,
    TURNOVER_HOT,
    TURNOVER_LOW,
    build_snapshot,
    fetch_board_ranking,
    fetch_breadth,
    fetch_index_bars,
    score_market,
)

__all__ = [
    "BREADTH_BEARISH",
    "BREADTH_BULLISH",
    "LIMIT_DOWN_PANIC",
    "LIMIT_UP_ACTIVE",
    "TURNOVER_HOT",
    "TURNOVER_LOW",
    "build_snapshot",
    "fetch_board_ranking",
    "fetch_breadth",
    "fetch_index_bars",
    "score_market",
]
