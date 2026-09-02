"""市场情绪评分与快照组装测试（P0-T1，纯计算无网络）。"""

from __future__ import annotations

import pandas as pd
import pytest

from easy_tdx.sentiment import build_snapshot, score_market


def _breadth(**overrides) -> dict:
    base = {
        "up_count": 2500,
        "down_count": 2500,
        "neutral_count": 50,
        "suspended_count": 7,
        "total_count": 5057,
        "limit_up_count": 80,
        "limit_down_count": 3,
        "total_amount": 1e12,
        "total_volume": 6e8,
    }
    base.update(overrides)
    return base


def _index(close=3500.0, ma20=3400.0, amount_sh=5e11, amount_sz=6e11) -> dict:
    n = 30
    closes = [ma20] * (n - 1) + [close]
    sh = pd.DataFrame({"close": closes, "amount": [amount_sh] * n})
    sz = pd.DataFrame({"close": [2000.0] * n, "amount": [amount_sz] * n})
    return {"sh": sh, "sz": sz}


def test_bearish_market_scores_high():
    b = _breadth(up_count=1000, down_count=4000, limit_up_count=20, limit_down_count=15)
    ms = score_market(b, _index())
    assert ms["score"] >= 20
    assert ms["label"] == "偏空"
    names = [f["name"] for f in ms["factors"]]
    assert "涨跌家数" in names and "涨停家数" in names and "跌停家数" in names


def test_bullish_market_scores_low():
    b = _breadth(up_count=3500, down_count=1500, limit_up_count=120)
    ms = score_market(b, _index())
    assert ms["score"] < 10
    assert ms["label"] == "偏多"


def test_turnover_extremes():
    # 缩量（<8000 亿）
    ms = score_market(
        _breadth(up_count=2800, down_count=2000),
        _index(amount_sh=3e11, amount_sz=4e11),
    )
    assert any(f["name"] == "成交额" and "流动性不足" in f["note"] for f in ms["factors"])
    # 过热（>2.5 万亿）
    ms = score_market(_breadth(), _index(amount_sh=1.2e12, amount_sz=1.4e12))
    assert any(f["name"] == "成交额" and "过热" in f["note"] for f in ms["factors"])


def test_index_below_ma20_adds_risk():
    ms = score_market(_breadth(up_count=2800, down_count=2000), _index(close=3300.0, ma20=3400.0))
    assert any(f["name"] == "指数趋势" for f in ms["factors"])


def test_no_data_returns_zero_score():
    ms = score_market(None, None)
    assert ms["score"] == 0
    assert ms["label"] == "偏多"


def test_score_capped_at_30():
    b = _breadth(up_count=0, down_count=5000, limit_up_count=0, limit_down_count=100)
    ms = score_market(b, _index(close=3000.0, ma20=3400.0))
    assert ms["score"] == pytest.approx(30.0)


def test_build_snapshot_structure():
    snap = build_snapshot(
        breadth=_breadth(),
        index=_index(),
        board={"gainers": [{"name": "玻璃玻纤"}]},
        features={"bond_5_CNTY": 1.7},
        date="2026-09-02",
    )
    assert snap["date"] == "2026-09-02"
    assert snap["breadth"]["up_count"] == 2500
    assert snap["index"]["sh_close"] == 3500.0
    assert snap["index"]["sh_change_pct"] == pytest.approx(2.94, abs=0.01)
    assert snap["index"]["turnover_2m"] == pytest.approx(1.1e12)
    assert snap["board_ranking"]["gainers"][0]["name"] == "玻璃玻纤"
    assert snap["features"]["bond_5_CNTY"] == 1.7
    assert "market_score" in snap


def test_build_snapshot_without_data():
    snap = build_snapshot()
    assert snap["breadth"] is None
    assert snap["index"] is None
    assert snap["market_score"]["score"] == 0
