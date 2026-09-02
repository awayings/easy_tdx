"""盘中跟踪脚本纯逻辑测试（无网络；poll_once 用 FakeClient 注入）。"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.track_intraday as track  # noqa: E402, I001


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sym(**kw) -> track.WatchSymbol:
    base = dict(
        code="002594",
        market="sz",
        name="比亚迪",
        rise_pct=4.0,
        fall_pct=4.0,
        price_above=None,
        price_below=None,
        pullback_pct=None,
        rebound_pct=None,
    )
    base.update(kw)
    return track.WatchSymbol(**base)


def _cfg(symbols: list[track.WatchSymbol] | None = None, **kw) -> track.WatchlistConfig:
    return track.WatchlistConfig(
        poll_seconds=kw.pop("poll_seconds", 15.0),
        hysteresis_pct=kw.pop("hysteresis_pct", 0.5),
        symbols=symbols or [_sym()],
    )


class FakeClient:
    """get_stock_quotes 返回预设 DataFrame；可配置抛异常。"""

    def __init__(self, frames: list[pd.DataFrame], *, error: Exception | None = None):
        self._frames = list(frames)
        self._error = error

    def get_stock_quotes(self, stocks):
        if self._error:
            raise self._error
        return self._frames.pop(0) if self._frames else pd.DataFrame()


def _quote_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"code": r["code"], "name": r["name"], **r} for r in rows])


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_missing_file_creates_default(tmp_path):
    p = tmp_path / "watchlist.json"
    cfg = track.load_config(p)
    assert p.exists()
    assert cfg.poll_seconds == 15
    assert cfg.hysteresis_pct == 0.5
    assert [(s.code, s.market, s.rise_pct, s.fall_pct) for s in cfg.symbols] == [
        ("002594", "sz", 4.0, 4.0),
        ("159558", "sz", 4.0, 4.0),
        ("161129", "sz", 4.0, 4.0),
    ]
    assert all(s.price_above is None and s.price_below is None for s in cfg.symbols)
    # 波动阈值键缺省 → 默认 4.0
    assert all(s.pullback_pct == 4.0 and s.rebound_pct == 4.0 for s in cfg.symbols)


def test_load_config_custom(tmp_path):
    p = tmp_path / "watchlist.json"
    p.write_text(
        json.dumps(
            {
                "poll_seconds": 30,
                "hysteresis_pct": 0.3,
                "symbols": [
                    {
                        "code": "600519",
                        "market": "sh",
                        "name": "茅台",
                        "rise_pct": 5.0,
                        "fall_pct": None,  # 显式 null → 禁用
                        "price_above": 2000.0,
                        "price_below": 1000.0,
                        "pullback_pct": None,  # 显式 null → 禁用
                        "rebound_pct": 3.0,
                    },
                    {"code": "002594", "market": "sz"},  # 阈值缺省 → 默认 4.0
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = track.load_config(p)
    assert cfg.poll_seconds == 30
    assert cfg.hysteresis_pct == 0.3
    s1, s2 = cfg.symbols
    assert (s1.rise_pct, s1.fall_pct, s1.price_above, s1.price_below) == (
        5.0,
        None,
        2000.0,
        1000.0,
    )
    assert (s1.pullback_pct, s1.rebound_pct) == (None, 3.0)
    assert s2.market == "sz" and s2.name == "002594"
    assert (s2.rise_pct, s2.fall_pct) == (4.0, 4.0)
    assert (s2.pullback_pct, s2.rebound_pct) == (4.0, 4.0)  # 缺省 → 默认


@pytest.mark.parametrize(
    "raw, frag",
    [
        ({"code": "12345", "market": "sz"}, "code"),
        ({"code": "12ab56", "market": "sz"}, "code"),
        ({"code": "123456", "market": "bj"}, "market"),
        ({"code": "123456", "market": "sz", "rise_pct": -1.0}, "rise_pct"),
        ({"code": "123456", "market": "sz", "fall_pct": -0.1}, "fall_pct"),
        ({"code": "123456", "market": "sz", "price_above": 0}, "price_above"),
        ({"code": "123456", "market": "sz", "price_below": -5}, "price_below"),
        ({"code": "123456", "market": "sz", "pullback_pct": -1.0}, "pullback_pct"),
        ({"code": "123456", "market": "sz", "rebound_pct": -0.5}, "rebound_pct"),
    ],
)
def test_load_config_validation_errors(tmp_path, raw, frag):
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"symbols": [raw]}), encoding="utf-8")
    with pytest.raises(ValueError):
        track.load_config(p)


def test_load_config_too_many_symbols(tmp_path):
    p = tmp_path / "w.json"
    symbols = [{"code": f"{i:06d}", "market": "sz"} for i in range(81)]
    p.write_text(json.dumps({"symbols": symbols}), encoding="utf-8")
    with pytest.raises(ValueError, match="80"):
        track.load_config(p)


def test_load_config_bad_numeric_fields(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(
        json.dumps({"symbols": [{"code": "000001", "market": "sz"}], "poll_seconds": 0}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="poll_seconds"):
        track.load_config(p)


# ---------------------------------------------------------------------------
# 计算与窗口
# ---------------------------------------------------------------------------


def test_safe_price():
    assert track.safe_price(10.5) == 10.5
    assert track.safe_price("10.5") == 10.5
    assert track.safe_price(0) is None
    assert track.safe_price(-1) is None
    assert track.safe_price(float("nan")) is None
    assert track.safe_price(None) is None
    assert track.safe_price("abc") is None


def test_pct_change():
    assert track.pct_change(10.4, 10.0) == pytest.approx(4.0)
    assert track.pct_change(10.0, 10.0) == pytest.approx(0.0)
    assert track.pct_change(9.6, 10.0) == pytest.approx(-4.0)
    assert track.pct_change(10.0, None) is None
    assert track.pct_change(10.0, 0) is None
    assert track.pct_change(10.0, -1) is None


@pytest.mark.parametrize(
    "hm, expected",
    [
        (9 * 60 + 14, False),
        (9 * 60 + 15, True),
        (11 * 60 + 29, True),
        (11 * 60 + 30, False),
        (12 * 60 + 59, False),
        (13 * 60, True),
        (14 * 60 + 59, True),
        (15 * 60, False),
    ],
)
def test_in_session(hm, expected):
    assert track.in_session(hm) is expected


def test_seconds_to_next_session():
    assert track.seconds_to_next_session(9 * 60) == 15 * 60  # 09:00 → 09:15
    assert track.seconds_to_next_session(11 * 60 + 40) == 80 * 60  # 午休 → 13:00
    assert track.seconds_to_next_session(15 * 60 + 30) is None  # 收盘后无下一时段


# ---------------------------------------------------------------------------
# evaluate_symbol 状态机
# ---------------------------------------------------------------------------


def test_rise_crossing_and_rearm():
    sym = _sym(rise_pct=4.0)
    states: dict[tuple[str, str], str] = {}
    h = 0.5
    assert track.evaluate_symbol(sym, 103.9, 3.9, h, states) == []  # 未越界
    alerts = track.evaluate_symbol(sym, 104.0, 4.0, h, states)
    assert [(a["kind"], a["threshold"]) for a in alerts] == [("rise", 4.0)]
    assert states[("002594", "rise")] == "triggered"
    assert track.evaluate_symbol(sym, 104.1, 4.1, h, states) == []  # 越界持续不重复
    assert track.evaluate_symbol(sym, 103.5, 3.5, h, states) == []  # 恰好等于缓冲界不重武装
    assert track.evaluate_symbol(sym, 103.49, 3.49, h, states) == []  # 越过缓冲 → 重武装
    assert states[("002594", "rise")] == "armed"
    alerts = track.evaluate_symbol(sym, 104.2, 4.2, h, states)
    assert len(alerts) == 1  # 再穿越再报


def test_fall_crossing_and_rearm():
    sym = _sym(fall_pct=4.0)
    states: dict[tuple[str, str], str] = {}
    h = 0.5
    assert track.evaluate_symbol(sym, 96.1, -3.9, h, states) == []
    assert len(track.evaluate_symbol(sym, 96.0, -4.0, h, states)) == 1
    assert track.evaluate_symbol(sym, 96.4, -3.6, h, states) == []  # -3.6 > -3.5? 否，仍在触发态
    assert states[("002594", "fall")] == "triggered"
    assert track.evaluate_symbol(sym, 96.6, -3.4, h, states) == []  # -3.4 > -3.5 → 重武装
    assert states[("002594", "fall")] == "armed"
    assert len(track.evaluate_symbol(sym, 95.9, -4.1, h, states)) == 1


def test_price_above_crossing_and_rearm():
    sym = _sym(rise_pct=None, fall_pct=None, price_above=100.0)
    states: dict[tuple[str, str], str] = {}
    h = 0.5
    assert track.evaluate_symbol(sym, 99.9, None, h, states) == []
    alerts = track.evaluate_symbol(sym, 100.0, None, h, states)  # pct=None 不影响点位方向
    assert [(a["kind"], a["threshold"]) for a in alerts] == [("above", 100.0)]
    assert track.evaluate_symbol(sym, 101.0, None, h, states) == []
    assert track.evaluate_symbol(sym, 99.49, None, h, states) == []  # 99.5 边界不重武装
    assert track.evaluate_symbol(sym, 99.4, None, h, states) == []  # 越界重武装
    assert states[("002594", "above")] == "armed"
    assert len(track.evaluate_symbol(sym, 100.5, None, h, states)) == 1


def test_price_below_crossing_and_rearm():
    sym = _sym(rise_pct=None, fall_pct=None, price_below=100.0)
    states: dict[tuple[str, str], str] = {}
    h = 0.5
    assert track.evaluate_symbol(sym, 100.1, None, h, states) == []
    assert len(track.evaluate_symbol(sym, 100.0, None, h, states)) == 1
    assert track.evaluate_symbol(sym, 99.0, None, h, states) == []
    assert track.evaluate_symbol(sym, 100.51, None, h, states) == []  # 100.5 边界不重武装
    assert track.evaluate_symbol(sym, 100.6, None, h, states) == []
    assert states[("002594", "below")] == "armed"
    assert len(track.evaluate_symbol(sym, 99.9, None, h, states)) == 1


def test_pct_conditions_disabled_when_pre_close_invalid():
    # 仅 pre_close 无效：涨跌幅方向不评估，点位方向仍评估
    sym = _sym(rise_pct=4.0, price_below=50.0)
    states: dict[tuple[str, str], str] = {}
    assert track.evaluate_symbol(sym, 55.0, None, 0.5, states) == []  # pct=None 不触发 rise
    alerts = track.evaluate_symbol(sym, 49.0, None, 0.5, states)
    assert [a["kind"] for a in alerts] == ["below"]


def test_disabled_directions_never_trigger():
    sym = _sym(rise_pct=None, fall_pct=None)
    states: dict[tuple[str, str], str] = {}
    assert track.evaluate_symbol(sym, 999.0, 999.0, 0.5, states) == []


# ---------------------------------------------------------------------------
# evaluate_symbol 波动口径（基准 = 当日高低点）
# ---------------------------------------------------------------------------


def test_pullback_crossing_and_rearm():
    sym = _sym(rise_pct=None, fall_pct=None, pullback_pct=4.0)
    states: dict[tuple[str, str], str] = {}
    h = 0.5
    # 高点 90、现价 86.3 → 回落 (90-86.3)/90 = 4.11% → 触发
    assert track.evaluate_symbol(sym, 86.6, None, h, states, high=90.0, low=86.0) == []
    alerts = track.evaluate_symbol(sym, 86.3, None, h, states, high=90.0, low=86.0)
    assert [(a["kind"], a["value"]) for a in alerts] == [
        ("pullback", pytest.approx(4.111, abs=0.01))
    ]
    assert states[("002594", "pullback")] == "triggered"
    # 越界持续不重复
    assert track.evaluate_symbol(sym, 86.0, None, h, states, high=90.0, low=86.0) == []
    # 回落收窄到 3.5%（缓冲界）不重武装
    assert track.evaluate_symbol(sym, 86.85, None, h, states, high=90.0, low=86.0) == []
    # 收窄到 3.49% → 重武装
    assert track.evaluate_symbol(sym, 86.86, None, h, states, high=90.0, low=86.0) == []
    assert states[("002594", "pullback")] == "armed"
    # 再回落 → 再报
    assert len(track.evaluate_symbol(sym, 86.3, None, h, states, high=90.0, low=86.0)) == 1


def test_pullback_new_high_resets_naturally():
    # 创新高后 close≈high → 回落幅度自动归零 → 重武装（无需额外逻辑）
    sym = _sym(rise_pct=None, fall_pct=None, pullback_pct=4.0)
    states: dict[tuple[str, str], str] = {}
    assert len(track.evaluate_symbol(sym, 86.3, None, 0.5, states, high=90.0, low=86.0)) == 1
    assert states[("002594", "pullback")] == "triggered"
    # 创新高 92，现价 91.9 → 回落 0.11% → 重武装
    assert track.evaluate_symbol(sym, 91.9, None, 0.5, states, high=92.0, low=86.0) == []
    assert states[("002594", "pullback")] == "armed"
    # 从新高 92 回落 4% → 88.32 → 再报
    alerts = track.evaluate_symbol(sym, 88.3, None, 0.5, states, high=92.0, low=86.0)
    assert len(alerts) == 1
    assert alerts[0]["extreme"] == 92.0


def test_rebound_crossing_and_rearm():
    sym = _sym(rise_pct=None, fall_pct=None, rebound_pct=4.0)
    states: dict[tuple[str, str], str] = {}
    h = 0.5
    # 低点 85、现价 88.4 → 反弹 (88.4-85)/85 = 4.0% → 触发
    alerts = track.evaluate_symbol(sym, 88.4, None, h, states, high=90.0, low=85.0)
    assert [(a["kind"], a["value"]) for a in alerts] == [("rebound", pytest.approx(4.0))]
    assert states[("002594", "rebound")] == "triggered"
    assert track.evaluate_symbol(sym, 88.5, None, h, states, high=90.0, low=85.0) == []
    # 反弹收窄到 3.6% 不重武装；3.4% 重武装
    assert track.evaluate_symbol(sym, 88.06, None, h, states, high=90.0, low=85.0) == []
    assert track.evaluate_symbol(sym, 87.89, None, h, states, high=90.0, low=85.0) == []
    assert states[("002594", "rebound")] == "armed"
    assert len(track.evaluate_symbol(sym, 88.6, None, h, states, high=90.0, low=85.0)) == 1


def test_swing_disabled_when_extremes_invalid():
    # high/low 无效 → 对应方向不评估；其余方向正常
    sym = _sym(rise_pct=None, fall_pct=None, pullback_pct=4.0, rebound_pct=4.0)
    states: dict[tuple[str, str], str] = {}
    assert track.evaluate_symbol(sym, 86.4, None, 0.5, states, high=None, low=None) == []
    assert track.evaluate_symbol(sym, 86.4, None, 0.5, states, high=0.0, low=math.nan) == []


def test_swing_disabled_by_config():
    sym = _sym(rise_pct=None, fall_pct=None, pullback_pct=None, rebound_pct=None)
    states: dict[tuple[str, str], str] = {}
    assert track.evaluate_symbol(sym, 50.0, None, 0.5, states, high=100.0, low=50.0) == []


# ---------------------------------------------------------------------------
# format_alert / should_exit
# ---------------------------------------------------------------------------


def test_format_alert_rise():
    sym = _sym()
    alert = {"kind": "rise", "threshold": 4.0, "close": 321.5, "pct": 4.23}
    text = track.format_alert(sym, alert, datetime(2026, 9, 1, 14, 32, 5))
    assert text == "比亚迪(002594) 涨超阈值: 现价 321.50 (+4.23%), 阈值 +4.0% [14:32:05]"


def test_format_alert_above_without_pct():
    sym = _sym()
    alert = {"kind": "above", "threshold": 350.0, "close": 350.2, "pct": None}
    text = track.format_alert(sym, alert, datetime(2026, 9, 1, 10, 0, 0))
    assert text == "比亚迪(002594) 上破点位: 现价 350.20, 点位 350.00 [10:00:00]"


def test_format_alert_pullback_and_rebound():
    sym = _sym()
    pb = {"kind": "pullback", "threshold": 4.0, "close": 86.4, "pct": None,
          "value": 4.12, "extreme": 90.0}
    text = track.format_alert(sym, pb, datetime(2026, 9, 1, 14, 32, 5))
    assert text == (
        "比亚迪(002594) 自高点回落: 现价 86.40 (回落 4.12%), "
        "高点 90.00, 阈值 4.0% [14:32:05]"
    )
    rb = {"kind": "rebound", "threshold": 4.0, "close": 88.4, "pct": None,
          "value": 4.0, "extreme": 85.0}
    text = track.format_alert(sym, rb, datetime(2026, 9, 1, 14, 32, 5))
    assert text == (
        "比亚迪(002594) 自低点反弹: 现价 88.40 (反弹 4.00%), "
        "低点 85.00, 阈值 4.0% [14:32:05]"
    )


def test_should_exit():
    assert track.should_exit(datetime(2026, 9, 5, 10, 0)) is not None  # 周六
    assert track.should_exit(datetime(2026, 9, 6, 10, 0)) is not None  # 周日
    assert track.should_exit(datetime(2026, 9, 1, 15, 6)) is not None  # 收盘后
    assert track.should_exit(datetime(2026, 9, 1, 15, 5)) is None  # 15:05 临界（<=CLOSE_HM）
    assert track.should_exit(datetime(2026, 9, 1, 10, 0)) is None  # 交易日盘中


# ---------------------------------------------------------------------------
# poll_once 端到端（FakeClient）
# ---------------------------------------------------------------------------


def _rows_2sym() -> list[dict]:
    return [
        {"code": "002594", "name": "比亚迪", "close": 104.2, "pre_close": 100.0},
        {"code": "159558", "name": "半导体设备ETF", "close": 1.01, "pre_close": 1.00},
    ]


def test_poll_once_alert_rearm_alert(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(track, "notify", lambda title, text: sent.append(text))
    syms = [_sym(), _sym(code="159558", name="半导体设备ETF")]
    cfg = _cfg(symbols=syms, hysteresis_pct=0.5)
    stocks = [(0, "002594"), (0, "159558")]
    client = FakeClient([_quote_df(_rows_2sym())] * 4)
    states: dict[tuple[str, str], str] = {}
    kw = dict(poll_no=1, no_notify=False, dry_run=False, verbose=False)
    # 第 1 轮：比亚迪 +4.2% 越界 → 1 告警
    assert track.poll_once(client, cfg, stocks, states, **kw) == 1
    assert len(sent) == 1 and "比亚迪(002594) 涨超阈值" in sent[0]
    # 第 2 轮：仍越界 → 0
    assert track.poll_once(client, cfg, stocks, states, **kw) == 0
    # 第 3 轮：回落到 3.4% → 重武装；第 4 轮再越界 → 再告警
    client2 = FakeClient(
        [
            _quote_df(
                [
                    {"code": "002594", "name": "比亚迪", "close": 103.4, "pre_close": 100.0},
                    {"code": "159558", "name": "半导体设备ETF", "close": 1.01, "pre_close": 1.00},
                ]
            ),
            _quote_df(_rows_2sym()),
        ]
    )
    assert track.poll_once(client2, cfg, stocks, states, **kw) == 0
    assert track.poll_once(client2, cfg, stocks, states, **kw) == 1
    assert len(sent) == 2


def test_poll_once_suspended_and_missing_skipped(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(track, "notify", lambda title, text: sent.append(text))
    syms = [
        _sym(code="002594", rise_pct=4.0, price_below=50.0),  # 停牌 close=0 也不应触发点位
        _sym(code="159558", name="半导体设备ETF"),
    ]
    cfg = _cfg(symbols=syms)
    stocks = [(0, "002594"), (0, "159558")]
    df = _quote_df(
        [
            {"code": "002594", "name": "比亚迪", "close": 0.0, "pre_close": 100.0},
            {"code": "159558", "name": "半导体设备ETF", "close": 1.01, "pre_close": 1.00},
        ]
    )
    client = FakeClient([df])
    states: dict[tuple[str, str], str] = {}
    assert track.poll_once(client, cfg, stocks, states, poll_no=1, no_notify=False,
                           dry_run=False, verbose=False) == 0
    assert sent == []
    # 响应缺该标的（停牌常见）同样跳过
    client2 = FakeClient([_quote_df([_rows_2sym()[1]])])
    assert track.poll_once(client2, cfg, stocks, states, poll_no=2, no_notify=False,
                           dry_run=False, verbose=False) == 0
    assert sent == []


def test_poll_once_no_notify_and_dry_run(monkeypatch, capsys):
    sent: list[str] = []
    monkeypatch.setattr(track, "notify", lambda title, text: sent.append(text))
    cfg = _cfg(symbols=[_sym()])
    stocks = [(0, "002594")]
    states: dict[tuple[str, str], str] = {}
    # no_notify：告警只打日志
    track.poll_once(FakeClient([_quote_df(_rows_2sym())]), cfg, stocks, states,
                    poll_no=1, no_notify=True, dry_run=False, verbose=False)
    assert sent == []
    assert "涨超阈值" in capsys.readouterr().out
    # dry_run：打印 [DRY-RUN] 前缀，不发送
    states2: dict[tuple[str, str], str] = {}
    track.poll_once(FakeClient([_quote_df(_rows_2sym())]), cfg, stocks, states2,
                    poll_no=1, no_notify=False, dry_run=True, verbose=False)
    assert sent == []
    assert "[DRY-RUN]" in capsys.readouterr().out


def test_poll_once_client_error_returns_zero(monkeypatch):
    monkeypatch.setattr(track, "notify", lambda title, text: None)
    cfg = _cfg(symbols=[_sym()])
    client = FakeClient([], error=RuntimeError("连接断开"))
    states: dict[tuple[str, str], str] = {}
    assert track.poll_once(client, cfg, [(0, "002594")], states,
                           poll_no=1, no_notify=False, dry_run=False, verbose=False) == 0


def test_poll_once_pre_close_invalid_price_point_still_works(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(track, "notify", lambda title, text: sent.append(text))
    sym = _sym(rise_pct=4.0, fall_pct=4.0, price_below=100.0)
    cfg = _cfg(symbols=[sym])
    df = _quote_df([{"code": "002594", "name": "比亚迪", "close": 99.0, "pre_close": math.nan}])
    states: dict[tuple[str, str], str] = {}
    assert track.poll_once(FakeClient([df]), cfg, [(0, "002594")], states,
                           poll_no=1, no_notify=False, dry_run=False, verbose=False) == 1
    assert "下破点位" in sent[0]


def test_poll_once_pullback_end_to_end(monkeypatch):
    """报价含 high/low 列：回落越界报一次，重武装后再报。"""
    sent: list[str] = []
    monkeypatch.setattr(track, "notify", lambda title, text: sent.append(text))
    sym = _sym(rise_pct=None, fall_pct=None, pullback_pct=4.0, rebound_pct=None)
    cfg = _cfg(symbols=[sym])
    rows = [{"code": "002594", "name": "比亚迪", "close": 86.3,
             "pre_close": 90.0, "high": 90.0, "low": 86.0}]
    client = FakeClient([_quote_df(rows)] * 2)
    states: dict[tuple[str, str], str] = {}
    kw = dict(poll_no=1, no_notify=False, dry_run=False, verbose=False)
    assert track.poll_once(client, cfg, [(0, "002594")], states, **kw) == 1
    assert "自高点回落" in sent[0] and "高点 90.00" in sent[0]
    assert track.poll_once(client, cfg, [(0, "002594")], states, **kw) == 0  # 越界持续不重复
    # 收窄 → 重武装 → 再回落 → 再报
    client2 = FakeClient(
        [
            _quote_df([{**rows[0], "close": 86.9}]),  # 回落 3.4% → 重武装
            _quote_df(rows),
        ]
    )
    assert track.poll_once(client2, cfg, [(0, "002594")], states, **kw) == 0
    assert track.poll_once(client2, cfg, [(0, "002594")], states, **kw) == 1
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# 配置热加载 reload_config_if_changed
# ---------------------------------------------------------------------------


def _write_watch(tmp_path, name="w.json", rise=4.0, mtime_ns=1_000_000_000):
    import os

    p = tmp_path / name
    p.write_text(
        json.dumps(
            {"poll_seconds": 15, "hysteresis_pct": 0.5,
             "symbols": [{"code": "002594", "market": "sz", "rise_pct": rise, "fall_pct": 4.0}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.utime(p, ns=(mtime_ns, mtime_ns))  # 精确控制 mtime，防同纳秒抖动
    return p


def test_reload_unchanged_returns_same_object(tmp_path):
    p = _write_watch(tmp_path, mtime_ns=1_000_000_000)
    cfg = track.load_config(p)
    new_cfg, new_mtime = track.reload_config_if_changed(p, 1_000_000_000, cfg)
    assert new_cfg is cfg
    assert new_mtime == 1_000_000_000


def test_reload_changed_applies_new_values(tmp_path):
    p = _write_watch(tmp_path, rise=4.0, mtime_ns=1_000_000_000)
    cfg = track.load_config(p)
    _write_watch(tmp_path, rise=3.0, mtime_ns=2_000_000_000)
    new_cfg, new_mtime = track.reload_config_if_changed(p, 1_000_000_000, cfg)
    assert new_cfg is not cfg
    assert new_cfg.symbols[0].rise_pct == 3.0
    assert new_mtime == 2_000_000_000


def test_reload_broken_keeps_old_and_advances_mtime(tmp_path, capsys):
    p = _write_watch(tmp_path, mtime_ns=1_000_000_000)
    cfg = track.load_config(p)
    p.write_text("{ 这不是合法 JSON", encoding="utf-8")
    import os

    os.utime(p, ns=(2_000_000_000, 2_000_000_000))
    new_cfg, new_mtime = track.reload_config_if_changed(p, 1_000_000_000, cfg)
    assert new_cfg is cfg  # 沿用旧配置
    assert new_mtime == 2_000_000_000  # mtime 已推进，不会每轮重试坏文件
    assert "沿用旧配置" in capsys.readouterr().out
    # 同一 mtime 再次调用：不重试解析
    again_cfg, _ = track.reload_config_if_changed(p, 2_000_000_000, cfg)
    assert again_cfg is cfg
    # 修复后（新 mtime）→ 生效
    _write_watch(tmp_path, rise=2.5, mtime_ns=3_000_000_000)
    fixed_cfg, _ = track.reload_config_if_changed(p, 2_000_000_000, cfg)
    assert fixed_cfg is not cfg
    assert fixed_cfg.symbols[0].rise_pct == 2.5


def test_reload_missing_file_regenerates_default(tmp_path, capsys):
    p = _write_watch(tmp_path, rise=1.0, mtime_ns=1_000_000_000)
    cfg = track.load_config(p)
    p.unlink()
    new_cfg, _ = track.reload_config_if_changed(p, 1_000_000_000, cfg)
    assert p.exists()  # 按启动语义重新生成默认配置
    assert new_cfg is not cfg
    assert len(new_cfg.symbols) == len(track.DEFAULT_SYMBOLS)
    assert "已生成默认配置" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# open_client 启动连接重试（睡眠唤醒 WiFi 未就绪场景）
# ---------------------------------------------------------------------------


def test_open_client_retries_then_succeeds(monkeypatch, capsys):
    calls = {"n": 0}

    class FakeMacClient:
        def connect(self):
            pass

        def close(self):
            pass

    def fake_from_best_host():
        calls["n"] += 1
        if calls["n"] < 3:
            raise track.TdxConnectionError("网络未就绪")
        return FakeMacClient()

    monkeypatch.setattr(track.MacClient, "from_best_host", staticmethod(fake_from_best_host))
    monkeypatch.setattr(track.time, "sleep", lambda s: None)
    client = track.open_client()
    assert isinstance(client, FakeMacClient)
    assert calls["n"] == 3
    assert "重试" in capsys.readouterr().out


def test_open_client_gives_up_after_close(monkeypatch):
    def boom():
        raise track.TdxConnectionError("网络未就绪")

    monkeypatch.setattr(track.MacClient, "from_best_host", staticmethod(boom))
    monkeypatch.setattr(track.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        track.time,
        "localtime",
        lambda: track.time.struct_time((2026, 9, 2, 15, 6, 0, 2, 245, 0)),
    )
    with pytest.raises(track.TdxConnectionError):
        track.open_client()
