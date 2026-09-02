"""交易计划生成器测试（P2-T3 核心，纯计算注入，无网络）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from easy_tdx.planner import parse_symbol
from easy_tdx.planner.plan import build_plan, compute_position, compute_risk, decide_verdict


def _make_df(n=300, start=10.0, drift=0.012, amp=0.015, seed=7) -> pd.DataFrame:
    """合成日线：持续上涨（上升趋势），用于基线计划。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    rows = []
    price = start
    for d in dates:
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
                "vol": 1000.0,
                "amount": 1e6,
            }
        )
    return pd.DataFrame(rows)


def _bearish_market() -> dict:
    return {
        "score": 24.0,
        "label": "偏空",
        "factors": [{"name": "涨跌家数", "points": 15.0, "note": "下跌家数占比 78%"}],
    }


def test_parse_symbol():
    assert parse_symbol("002594") == ("SZ", "002594")
    assert parse_symbol("600519") == ("SH", "600519")
    assert parse_symbol("688981") == ("SH", "688981")
    assert parse_symbol("sz 002594") == ("SZ", "002594")
    assert parse_symbol("SH600519") == ("SH", "600519")
    with pytest.raises(ValueError):
        parse_symbol("abc")


def test_build_plan_basic_structure():
    df = _make_df()
    plan = build_plan(df, symbol="SZ 000001")
    assert "error" not in plan
    close = float(df["close"].iloc[-1])
    # 止损在参考价下方，止盈在上方
    assert plan["stop_loss"]["price"] < close
    assert plan["stop_loss"]["pct"] < 0
    assert plan["take_profit"][0]["price"] > close
    assert plan["take_profit"][0]["pct"] > 0
    # 风险分 0-100，分量求和与总分一致
    risk = plan["risk"]
    assert 0 <= risk["score"] <= 100
    comp = risk["components"]
    assert sum(comp.values()) == pytest.approx(risk["score"], abs=0.2)
    # 仓位在 [0, 20%] 区间
    assert 0 <= plan["position"]["pct"] <= 20
    # 结构字段齐全
    for key in ("entry", "stop_loss", "take_profit", "trailing_stop", "risk_reward", "verdict"):
        assert key in plan
    assert plan["verdict"] in ("buy", "wait", "avoid")
    assert plan["entry"]["zone"]
    assert plan["entry"]["condition"]


def test_risk_components_factors():
    df = _make_df()
    from easy_tdx.ta import analyze_technical_state

    tech = analyze_technical_state(df)
    risk = compute_risk(tech, public=None, market_score=_bearish_market())
    assert risk["components"]["market"] == pytest.approx(16.0, abs=0.1)  # 24×2/3
    # 舆情无数据 → 0 分
    assert risk["components"]["sentiment"] == 0


def test_public_sentiment_hot_rank_adds_risk():
    df = _make_df()
    from easy_tdx.ta import analyze_technical_state

    tech = analyze_technical_state(df)
    public = {
        "hot_rank": 5,
        "hot_rank_change": 30,
        "news_hits_total": 100,
        "news_articles_3d": 12,
        "news_sentiment": -0.5,
        "available": True,
    }
    risk = compute_risk(tech, public=public, market_score=None)
    assert risk["components"]["sentiment"] == 25  # 15 + 5 + 5 + 5 封顶
    names = [f["name"] for f in risk["factors"]]
    assert "人气榜" in names and "消息密集" in names and "新闻情绪" in names


def test_verdict_risk_reward_forces_wait():
    risk = {"score": 25.0}
    tech = {"trend": "上升趋势", "price_vs_ma20_pct": 3.0}
    verdict, notes = decide_verdict(risk, tech, rr1=0.9)
    assert verdict == "wait"
    assert any("盈亏比" in n for n in notes)
    # 盈亏比达标 → 买入
    assert decide_verdict(risk, tech, rr1=2.0) == ("buy", [])


def test_verdict_high_risk_forces_avoid():
    risk = {"score": 80.0}
    tech = {"trend": "下降趋势", "price_vs_ma20_pct": -5.0}
    verdict, notes = decide_verdict(risk, tech, rr1=3.0)
    assert verdict == "avoid"
    assert any("≥75" in n for n in notes)


def test_verdict_downtrend_forces_wait():
    risk = {"score": 40.0}
    tech = {"trend": "下降趋势", "price_vs_ma20_pct": -3.0}
    verdict, notes = decide_verdict(risk, tech, rr1=2.5)
    assert verdict == "wait"
    assert any("飞刀" in n for n in notes)


def test_high_risk_plan_avoid_position_zero():
    # 集成：风险分 ≥75 → verdict=avoid 且仓位 0。
    # 构造：跳空高开远离全部支撑 + 人气榜第 1 + 负面新闻 + 市场偏空 ——
    # 技术/舆情/市场三路风险全部打满才应触发"观望"（75 分是极端阈值）。
    df = _make_df(drift=0.002, amp=0.005)
    last = df.iloc[-1]
    gap = last["close"] * 1.35  # 跳空 +35%，距最近支撑 >12% → +20
    df.loc[df.index[-1], ["open", "high", "low", "close"]] = [gap, gap, gap, gap]
    plan = build_plan(
        df,
        symbol="SZ 000001",
        market_score={"score": 30.0, "label": "偏空", "factors": []},
        public={
            "hot_rank": 1,
            "hot_rank_change": 0,
            "news_articles_3d": 15,
            "news_sentiment": -0.8,
            "available": True,
        },
    )
    assert plan["risk"]["score"] >= 75
    assert plan["verdict"] == "avoid"
    assert plan["position"]["pct"] == 0


def test_market_bearish_halves_position():
    df = _make_df()
    plan = build_plan(df, symbol="SZ 000001", market_score=_bearish_market())
    base = plan["position"]["pct"]
    plan2 = build_plan(df, symbol="SZ 000001", market_score=None)
    # 偏空市场仓位应显著更低（风险分相同时 ≈ 减半）
    assert base < plan2["position"]["pct"]


def test_compute_position_caps():
    pos = compute_position(30, {"score": 10, "label": "中性"}, 2.0, base_position=0.5)
    assert pos["pct"] <= 20  # 单标的上限
    pos2 = compute_position(80, None, None, base_position=0.3)
    assert pos2["pct"] == 0  # 高风险不开仓


def test_insufficient_df_error():
    df = _make_df(n=10)
    plan = build_plan(df, symbol="SZ 000001")
    assert "error" in plan
