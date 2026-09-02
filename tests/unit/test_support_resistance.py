"""支撑/阻力与筹码分布模块测试（P0-T4，纯计算无网络）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from easy_tdx.ta import analyze_technical_state, chip_distribution, find_support_resistance


def _make_df(n=300, start=10.0, drift=0.01, amp=0.02, seed=7, vol=1000.0) -> pd.DataFrame:
    """合成日线：带漂移的正弦波动（前 200 根上涨，后 100 根在 20 元附近震荡）。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    rows = []
    price = start
    for i, d in enumerate(dates):
        if i > 200:
            target = 20.0  # 震荡区间，形成筹码峰
            price = target + (price - target) * 0.7 + rng.normal(0, amp)
        else:
            price = price * (1 + drift + rng.normal(0, amp))
        o = price * (1 + rng.normal(0, amp / 2))
        hi = max(o, price) * (1 + abs(rng.normal(0, amp)))
        lo = min(o, price) * (1 - abs(rng.normal(0, amp)))
        rows.append(
            {
                "datetime": d,
                "open": round(o, 2),
                "high": round(hi, 2),
                "low": round(lo, 2),
                "close": round(price, 2),
                "vol": vol * (1 + rng.random()),
                "amount": 1e6,
            }
        )
    return pd.DataFrame(rows)


def test_supports_below_and_resistances_above():
    df = _make_df()
    sr = find_support_resistance(df)
    close = float(df["close"].iloc[-1])
    assert sr["supports"], "应至少有一个支撑位"
    assert sr["resistances"], "应至少有一个阻力位"
    assert all(s["price"] < close for s in sr["supports"])
    assert all(r["price"] > close for r in sr["resistances"])
    # 支撑按价格降序（最近的在前），阻力升序
    sp = [s["price"] for s in sr["supports"]]
    rp = [r["price"] for r in sr["resistances"]]
    assert sp == sorted(sp, reverse=True)
    assert rp == sorted(rp)
    # 每项带距离
    assert all("distance_pct" in s and s["distance_pct"] >= 0 for s in sr["supports"])
    assert all("distance_pct" in r and r["distance_pct"] >= 0 for r in sr["resistances"])


def test_chip_peak_at_consolidation_zone():
    df = _make_df()
    chip = chip_distribution(df)
    assert 0 <= chip["profit_ratio"] <= 1
    assert chip["peaks"], "震荡区应形成筹码峰"
    # 筹码峰价格应位于震荡区（~20 元）附近
    top = chip["peaks"][0]["price"]
    assert 19.0 <= top <= 21.0
    assert chip["peaks"][0]["ratio"] >= chip["peaks"][-1]["ratio"]


def test_technical_state_fields():
    df = _make_df()
    st = analyze_technical_state(df)
    assert "error" not in st
    assert st["close"] == pytest.approx(float(df["close"].iloc[-1]), abs=0.01)
    assert st["trend"] in ("上升趋势", "下降趋势", "震荡")
    assert 0 <= st["rsi14"] <= 100
    assert st["rsi_state"] in ("超买", "超卖", "中性")
    assert st["atr14_pct"] > 0
    assert st["vol_ratio"] > 0
    assert st["support_resistance"]["supports"] or st["support_resistance"]["resistances"]


def test_technical_state_insufficient_data():
    df = _make_df(n=10)
    st = analyze_technical_state(df)
    assert "error" in st


def test_short_df_returns_empty_levels():
    df = _make_df(n=10)
    assert find_support_resistance(df) == {"supports": [], "resistances": []}
