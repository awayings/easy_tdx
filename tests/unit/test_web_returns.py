"""``easy_tdx.web.returns`` 纯计算单测（issue #7 口径：按日期锚定，不按 index）。

覆盖 issue 列出的 5 个场景：正常锚定 / 锚点日停牌回退 / 次新数据不足 /
除权日不出现假跌幅 / 今日非交易日退回。
"""

from __future__ import annotations

from datetime import date

import pytest

from easy_tdx.web.returns import (
    compute_stock_returns,
    last_bar_on_or_before,
    resolve_trade_date,
    shift_trade_date,
)

# 15 个连续工作日：2026-08-24(一) ~ 2026-09-11(五)
# → T=09-11 时 D_3=09-08 / D_5=09-04 / D_10=08-28
CALENDAR: list[date] = [
    date(2026, 8, 24),
    date(2026, 8, 25),
    date(2026, 8, 26),
    date(2026, 8, 27),
    date(2026, 8, 28),
    date(2026, 8, 31),
    date(2026, 9, 1),
    date(2026, 9, 2),
    date(2026, 9, 3),
    date(2026, 9, 4),
    date(2026, 9, 7),
    date(2026, 9, 8),
    date(2026, 9, 9),
    date(2026, 9, 10),
    date(2026, 9, 11),
]

TODAY = date(2026, 9, 11)
T = date(2026, 9, 11)


def _series(pairs: dict[date, float]) -> list[tuple[date, float]]:
    return sorted(pairs.items())


def _pct(price: float, anchor: float) -> float:
    """前端算涨跌幅的口径（后端只回锚点，涨跌幅由前端现算）。"""
    return (price / anchor - 1) * 100


# ── 场景 1：正常锚定 ────────────────────────────────────────────────────────


def test_anchor_dates_follow_calendar_offset() -> None:
    """D_3 / D_5 / D_10 取自交易日历（不是自然日，也不是个股自己的序列）。"""
    bars = _series({d: 100.0 for d in CALENDAR})
    result = compute_stock_returns(CALENDAR, bars, today=TODAY)

    assert result is not None
    assert result.trade_date == T
    assert [(a.days, a.date, a.close) for a in result.anchors] == [
        (3, date(2026, 9, 8), 100.0),
        (5, date(2026, 9, 4), 100.0),
        (10, date(2026, 8, 28), 100.0),
    ]
    assert result.last_date == T
    assert result.stale_days == 0


def test_anchor_close_is_the_window_base() -> None:
    """近3日 = 现价 / close(D_3) − 1（锚点收盘价即该窗口基准）。"""
    prices = {d: 10.0 for d in CALENDAR}
    prices[date(2026, 9, 8)] = 8.0  # D_3
    prices[date(2026, 9, 11)] = 10.0  # 现价
    result = compute_stock_returns(CALENDAR, _series(prices), today=TODAY)

    assert result is not None
    d3, d5, d10 = result.anchors
    assert d3.close == 8.0
    assert _pct(10.0, d3.close) == pytest.approx(25.0)
    assert _pct(10.0, d5.close) == pytest.approx(0.0)
    assert _pct(10.0, d10.close) == pytest.approx(0.0)


def test_windows_keep_request_order() -> None:
    """windows 与返回 anchors 同序（调用方按 days 取用）。"""
    bars = _series({d: 1.0 for d in CALENDAR})
    result = compute_stock_returns(CALENDAR, bars, today=TODAY, windows=[10, 3])
    assert result is not None
    assert [a.days for a in result.anchors] == [10, 3]


def test_calendar_may_be_unsorted_and_has_duplicates() -> None:
    """日历输入可乱序/含重复（内部 set + sort 规整）。"""
    bars = _series({d: 1.0 for d in CALENDAR})
    result = compute_stock_returns([*CALENDAR[::-1], T, T], bars, today=TODAY)
    assert result is not None
    assert [a.date for a in result.anchors] == [
        date(2026, 9, 8),
        date(2026, 9, 4),
        date(2026, 8, 28),
    ]


# ── 场景 2：锚点日停牌 → 退到最近一根 bar（返回实际日期）────────────────────


def test_suspended_on_anchor_day_falls_back() -> None:
    """个股 D_3 当日停牌（缺 09-08）→ 锚点退到 09-07 的 bar，并回实际日期。"""
    prices = {d: 10.0 for d in CALENDAR}
    del prices[date(2026, 9, 8)]  # 停牌：个股序列缺这一天
    prices[date(2026, 9, 7)] = 7.5
    result = compute_stock_returns(CALENDAR, _series(prices), today=TODAY)

    assert result is not None
    d3 = result.anchors[0]
    assert d3.days == 3
    assert d3.date == date(2026, 9, 7)  # 实际 bar 日期（不是 D_3）
    assert d3.close == 7.5
    # 停牌不改其余窗口
    assert result.anchors[1].date == date(2026, 9, 4)


def test_anchor_does_not_drift_by_index_when_last_bar_missing() -> None:
    """当日 bar 未入库（盘中）也不影响锚点：按日期锚定，与"最后一根"无关。"""
    bars = _series({d: 10.0 for d in CALENDAR if d < T})  # 今日 bar 还没落库
    result = compute_stock_returns(CALENDAR, bars, today=TODAY)

    assert result is not None
    assert [a.date for a in result.anchors] == [
        date(2026, 9, 8),
        date(2026, 9, 4),
        date(2026, 8, 28),
    ]
    assert result.last_date == date(2026, 9, 10)
    assert result.stale_days == 1


# ── 场景 3：次新股数据不足 → close 为 None ─────────────────────────────────


def test_new_stock_all_windows_null_when_listed_after_d3() -> None:
    """09-09 上市的次新：D_3(09-08) 之前无 bar → 三个窗口全 null。"""
    bars = _series({d: 20.0 for d in CALENDAR if d >= date(2026, 9, 9)})
    result = compute_stock_returns(CALENDAR, bars, today=TODAY)

    assert result is not None
    assert [(a.days, a.close, a.date) for a in result.anchors] == [
        (3, None, None),
        (5, None, None),
        (10, None, None),
    ]
    # 有 last_close 但仍可用于展示（前端显示 '-'）
    assert result.last_close == 20.0
    assert result.last_date == T


def test_new_stock_partial_windows_null() -> None:
    """09-08 上市：近3日有锚点（08 当天首根），近1周/近2周不足 → null。"""
    listed = [d for d in CALENDAR if d >= date(2026, 9, 8)]
    bars = _series({d: 20.0 + i for i, d in enumerate(listed)})
    result = compute_stock_returns(CALENDAR, bars, today=TODAY)

    assert result is not None
    d3, d5, d10 = result.anchors
    assert (d3.close, d3.date) == (20.0, date(2026, 9, 8))
    assert (d5.close, d5.date) == (None, None)
    assert (d10.close, d10.date) == (None, None)


def test_no_bars_returns_none() -> None:
    """该股一根 bar 都没有 → None（端点据此记 error，不影响整表）。"""
    assert compute_stock_returns(CALENDAR, [], today=TODAY) is None


# ── 场景 4：除权日不出现假跌幅（口径 = QFQ）────────────────────────────────


def test_ex_dividend_day_no_fake_drop_under_qfq() -> None:
    """跨除权日：QFQ 序列无假跌幅；同一算法喂不复权序列就会算出假跌幅。

    构造 10 送 3（除权价 = 前收 × 0.7，09-09 除权）：
    - 不复权：09-08 收 10.00 → 09-11 收 7.10，近3日 = −29%（假跌幅，实为除权）
    - 前复权：除权前价格整体 ×0.7 → 09-08 锚点 7.00，近3日 = +1.43%（真实收益）
    """
    qfq = _series(
        {
            **{d: 7.00 for d in CALENDAR if d < date(2026, 9, 9)},
            date(2026, 9, 9): 7.00,
            date(2026, 9, 10): 7.05,
            date(2026, 9, 11): 7.10,
        }
    )
    none_adj = _series(
        {
            **{d: 10.00 for d in CALENDAR if d < date(2026, 9, 9)},
            date(2026, 9, 9): 7.00,
            date(2026, 9, 10): 7.05,
            date(2026, 9, 11): 7.10,
        }
    )

    r_qfq = compute_stock_returns(CALENDAR, qfq, today=TODAY)
    r_none = compute_stock_returns(CALENDAR, none_adj, today=TODAY)
    assert r_qfq is not None and r_none is not None

    # 锚定日期一致（除权不影响交易日历）
    assert [a.date for a in r_qfq.anchors] == [a.date for a in r_none.anchors]
    # 除权日锚点（09-08）在两套口径下价格不同 → 涨跌幅口径截然不同
    assert r_qfq.anchors[0].close == pytest.approx(7.00)
    assert r_none.anchors[0].close == pytest.approx(10.00)
    assert _pct(7.10, r_qfq.anchors[0].close) == pytest.approx(1.4286, abs=1e-4)
    assert _pct(7.10, r_none.anchors[0].close) == pytest.approx(-29.0, abs=0.01)


def test_ex_dividend_day_in_window_does_not_shift_anchor() -> None:
    """除权日恰好是锚点日：按日期锚定取到底就是该日 bar（除权后价），不做插值。"""
    bars = _series(
        {
            **{d: 7.00 for d in CALENDAR if d < date(2026, 9, 8)},
            date(2026, 9, 8): 7.02,
            date(2026, 9, 9): 7.00,
            date(2026, 9, 10): 7.05,
            date(2026, 9, 11): 7.10,
        }
    )
    result = compute_stock_returns(CALENDAR, bars, today=TODAY)
    assert result is not None
    assert (result.anchors[0].close, result.anchors[0].date) == (7.02, date(2026, 9, 8))


# ── 场景 5：今日非交易日 → T 退回最近交易日 ─────────────────────────────────


def test_today_not_a_trading_day_falls_back() -> None:
    """2026-09-13 是周日 → T = 09-11，三个锚点与交易日当天完全一致。"""
    bars = _series({d: 10.0 for d in CALENDAR})
    weekend = compute_stock_returns(CALENDAR, bars, today=date(2026, 9, 13))
    friday = compute_stock_returns(CALENDAR, bars, today=TODAY)

    assert weekend is not None and friday is not None
    assert weekend.trade_date == T
    assert [(a.days, a.date) for a in weekend.anchors] == [(a.days, a.date) for a in friday.anchors]


def test_today_before_calendar_returns_none() -> None:
    """日历里没有任何 <= today 的交易日 → None（端点 503，不静默算错）。"""
    assert compute_stock_returns(CALENDAR, _series({T: 10.0}), today=date(2026, 8, 1)) is None
    assert resolve_trade_date(CALENDAR, date(2026, 8, 1)) is None


def test_today_is_in_calendar_uses_it() -> None:
    """今日是交易日且 bar 已入库 → T = 今日。"""
    assert resolve_trade_date(CALENDAR, TODAY) == TODAY
    assert resolve_trade_date(CALENDAR, date(2026, 9, 5)) == date(2026, 9, 4)  # 周六 → 周五
    assert resolve_trade_date([], TODAY) is None


# ── 长期停牌：stale_days ────────────────────────────────────────────────────


def test_stale_days_counts_calendar_gap() -> None:
    """最后一根 bar 停在 09-04 → 到 T(09-11) 相隔 5 个交易日。"""
    bars = _series({d: 10.0 for d in CALENDAR if d <= date(2026, 9, 4)})
    result = compute_stock_returns(CALENDAR, bars, today=TODAY)

    assert result is not None
    assert result.last_date == date(2026, 9, 4)
    assert result.stale_days == 5  # 09-07 / 08 / 09 / 10 / 11
    # 停牌期间锚点仍按日历算：D_3(09-08) 退到 09-04
    assert result.anchors[0].date == date(2026, 9, 4)


def test_stale_days_zero_when_last_bar_is_t() -> None:
    bars = _series({d: 10.0 for d in CALENDAR})
    result = compute_stock_returns(CALENDAR, bars, today=TODAY)
    assert result is not None and result.stale_days == 0


# ── 底层函数边界 ────────────────────────────────────────────────────────────


def test_shift_trade_date_edges() -> None:
    assert shift_trade_date(CALENDAR, T, 3) == date(2026, 9, 8)
    assert shift_trade_date(CALENDAR, T, 14) == date(2026, 8, 24)  # 日历首根
    assert shift_trade_date(CALENDAR, T, 15) is None  # 日历不够长
    assert shift_trade_date(CALENDAR, date(2026, 9, 13), 3) is None  # T 不在日历里
    with pytest.raises(ValueError):
        shift_trade_date(CALENDAR, T, 0)


def test_last_bar_on_or_before_edges() -> None:
    bars = [(date(2026, 9, 8), 1.0), (date(2026, 9, 10), 2.0)]
    assert last_bar_on_or_before(bars, date(2026, 9, 10)) == (date(2026, 9, 10), 2.0)
    assert last_bar_on_or_before(bars, date(2026, 9, 9)) == (date(2026, 9, 8), 1.0)
    assert last_bar_on_or_before(bars, date(2026, 9, 7)) is None  # 早于首根
    assert last_bar_on_or_before(bars, None) is None


def test_calendar_shorter_than_window_gives_null() -> None:
    """日历自身太短（如指数只有 4 根）→ 远期窗口 null，不 IndexError。"""
    short = CALENDAR[-4:]
    bars = _series({d: 10.0 for d in short})
    result = compute_stock_returns(short, bars, today=TODAY)

    assert result is not None
    assert [(a.days, a.date) for a in result.anchors] == [
        (3, short[-4]),  # 4 根日历里 D_3 = 最早一根
        (5, None),
        (10, None),
    ]


def test_anchor_uses_last_bar_on_or_before_dn() -> None:
    """锚点只受 ``date <= D_n`` 约束，与 bar 总数无关（800 根/稀疏序列都一样）。"""
    bars = _series({d: 5.0 for d in [date(2026, 8, 3), date(2026, 9, 11)]})
    result = compute_stock_returns(CALENDAR, bars, today=TODAY)
    assert result is not None
    assert all(a.date == date(2026, 8, 3) for a in result.anchors)
    assert result.stale_days == 0


def test_calendar_fixture_is_what_the_expectations_assume() -> None:
    """守卫：CALENDAR 确实是 15 个升序工作日（上面 D_n 硬编码期望值的依据）。"""
    assert len(CALENDAR) == 15
    assert all(d.weekday() < 5 for d in CALENDAR)
    assert CALENDAR == sorted(CALENDAR)
    assert CALENDAR[0] == date(2026, 8, 24)
    assert CALENDAR[-1] == date(2026, 9, 11)
