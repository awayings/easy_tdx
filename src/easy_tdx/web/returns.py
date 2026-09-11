"""自选列表「近 3 日 / 近 1 周 / 近 2 周」涨跌幅的锚点计算（纯函数，零 IO）。

口径（issue #7 定稿，勿改）：

- ``T`` = 交易日历中 ``<=`` 今天的最后一个交易日
- ``D_n`` = 交易日历中 ``T`` 往前数 ``n`` 个交易日的**日期**
- 锚点收盘 = 个股日线中 ``date <= D_n`` 的**最后一根 bar**（返回其实际日期）

两个设计要点（别"优化"掉）：

1. **按日期锚定而不是按 index 往回数**：当日 bar 是否已入库不定（盘中未收盘就没有），
   按 index 数会在收盘瞬间跳变；按日期 ``<=`` 锚定天然稳定。
2. **日历用上证指数而不是个股自己的序列**：个股停牌会缺日期，用它自己的序列数
   ``n`` 天会数错。

本模块只做"日历 + 个股序列 + windows → 锚点"的纯计算，取数与缓存见
:mod:`easy_tdx.web.routers.watchlist`。涨跌幅由前端用实时价现算（后端只回锚点收盘价）。
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

__all__ = [
    "StockReturns",
    "WindowAnchor",
    "compute_stock_returns",
    "last_bar_on_or_before",
    "resolve_trade_date",
    "shift_trade_date",
]

#: 默认窗口（交易日）：近 3 日 / 近 1 周 / 近 2 周
DEFAULT_WINDOWS: tuple[int, ...] = (3, 5, 10)


@dataclass(frozen=True)
class WindowAnchor:
    """单个窗口的锚点。

    ``close`` / ``date`` 为 ``None`` = 该窗口数据不足（次新股 / 长期停牌），
    前端显示 ``-``。
    """

    days: int
    close: float | None
    date: date | None


@dataclass(frozen=True)
class StockReturns:
    """一只标的的锚点计算结果。

    Attributes:
        trade_date: 日历锚定出的 ``T``。
        last_close: 个股最后一根 bar 的收盘价（前端无实时报价时兜底算涨跌幅）。
        last_date: 该 bar 的日期。
        stale_days: ``last_date`` 到 ``T`` 之间相隔的交易日数（``T`` 当日有 bar = 0）。
        anchors: 与请求的 ``windows`` 同序的锚点列表。
    """

    trade_date: date
    last_close: float | None
    last_date: date | None
    stale_days: int
    anchors: tuple[WindowAnchor, ...]


def resolve_trade_date(calendar: Sequence[date], today: date) -> date | None:
    """取交易日历中 ``<= today`` 的最后一个交易日（今日非交易日则自动回退）。

    Args:
        calendar: 交易日历（可乱序，内部排序；通常来自上证指数日线的日期列）。
        today: 今天的日历日。

    Returns:
        ``T``；日历为空或全部晚于 ``today`` 时返回 ``None``。
    """
    ordered = sorted(calendar)
    idx = bisect_left(ordered, today)
    # bisect_left：idx 是第一个 >= today 的位置；today 本身在日历里则取它
    if idx < len(ordered) and ordered[idx] == today:
        return ordered[idx]
    return ordered[idx - 1] if idx > 0 else None


def shift_trade_date(calendar: Sequence[date], t: date, n: int) -> date | None:
    """取交易日历中 ``t`` 往前数 ``n`` 个交易日的日期。

    Args:
        calendar: 交易日历。
        t: 基准交易日（应由 :func:`resolve_trade_date` 得到）。
        n: 交易日偏移（≥ 1）。

    Returns:
        ``D_n``；``t`` 不在日历中或往前不足 ``n`` 个交易日时返回 ``None``
        （次新股 / 日历过短）。
    """
    if n < 1:
        raise ValueError(f"交易日偏移必须 ≥ 1，收到 {n}")
    ordered = sorted(calendar)
    idx = bisect_left(ordered, t)
    if idx >= len(ordered) or ordered[idx] != t:
        return None
    back = idx - n
    return ordered[back] if back >= 0 else None


def last_bar_on_or_before(
    bars: Sequence[tuple[date, float]], target: date | None
) -> tuple[date, float] | None:
    """取个股序列中 ``date <= target`` 的最后一根 bar（按日期锚定，非按 index）。

    Args:
        bars: ``(日期, 收盘价)`` 升序序列。
        target: 锚定日期 ``D_n``；``None`` 直接返回 ``None``。

    Returns:
        ``(实际日期, 收盘价)``；锚定日停牌时退到最近一根（返回其真实日期），
        序列中没有任何 ``date <= target`` 的 bar 时返回 ``None``。
    """
    if target is None:
        return None
    found: tuple[date, float] | None = None
    for bar_date, close in bars:
        if bar_date > target:
            break  # bars 升序：后面只会更晚
        found = (bar_date, close)
    return found


def compute_stock_returns(
    calendar: Sequence[date],
    bars: Sequence[tuple[date, float]],
    *,
    today: date,
    windows: Sequence[int] = DEFAULT_WINDOWS,
) -> StockReturns | None:
    """按交易日历锚定个股各窗口的锚点收盘价。

    Args:
        calendar: 交易日历（上证指数日线日期，见模块 docstring 设计要点 2）。
        bars: 个股日线 ``(日期, 收盘价)`` 升序序列（QFQ 口径，见 issue #6）。
        today: 今天的日历日。
        windows: 交易日偏移列表（默认 3/5/10）。

    Returns:
        :class:`StockReturns`；日历为空、无 ``T`` 或个股无任何 bar 时返回 ``None``
        （调用方记 ``error``，不影响整表）。
    """
    ordered_cal = sorted(set(calendar))
    trade_date = resolve_trade_date(ordered_cal, today)
    if trade_date is None:
        return None
    series = sorted(bars)
    if not series:
        return None

    last_date, last_close = series[-1]
    stale_days = (
        sum(1 for d in ordered_cal if last_date < d <= trade_date) if last_date < trade_date else 0
    )

    anchors: list[WindowAnchor] = []
    for n in windows:
        d_n = shift_trade_date(ordered_cal, trade_date, n)
        bar = last_bar_on_or_before(series, d_n)
        anchors.append(
            WindowAnchor(
                days=n,
                close=None if bar is None else bar[1],
                date=None if bar is None else bar[0],
            )
        )

    return StockReturns(
        trade_date=trade_date,
        last_close=last_close,
        last_date=last_date,
        stale_days=stale_days,
        anchors=tuple(anchors),
    )
