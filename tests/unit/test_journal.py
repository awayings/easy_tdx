"""决策日志库测试（P1-T5：写入/读取往返无损、快照落库）。"""

from __future__ import annotations

from easy_tdx.journal import add_decision, get_decision, list_decisions

_PLAN = {
    "symbol": "SZ 002594",
    "date": "2026-09-02",
    "verdict": "wait",
    "entry_ref": 88.71,
    "stop_loss": {"price": 86.2, "condition": "收盘价跌破 86.2 元止损"},
    "take_profit": [{"tier": 1, "price": 90.5}],
    "features": {"bond_5_CNTY": 1.7, "fx_USDCNY": 6.72},
}


def test_roundtrip(tmp_path):
    db = tmp_path / "decisions.db"
    decision_id = add_decision(_PLAN["symbol"], _PLAN, source="pytest", note="测试", path=db)
    row = get_decision(decision_id, path=db)
    assert row["symbol"] == "SZ 002594"
    assert row["verdict"] == "wait"
    assert row["source"] == "pytest"
    # plan_json 反序列化后与原始计划一致（无损往返）
    assert row["plan_json"]["stop_loss"]["price"] == 86.2
    assert row["plan_json"]["take_profit"][0]["tier"] == 1
    # 特征快照落库且可读
    assert row["snapshot_json"] == {"bond_5_CNTY": 1.7, "fx_USDCNY": 6.72}


def test_snapshot_from_plan_features(tmp_path):
    db = tmp_path / "decisions.db"
    decision_id = add_decision("SH 600519", _PLAN, path=db)
    row = get_decision(decision_id, path=db)
    assert row["snapshot_json"] == _PLAN["features"]


def test_list_decisions_filter(tmp_path):
    db = tmp_path / "decisions.db"
    add_decision("SZ 002594", _PLAN, path=db)
    add_decision("SH 600519", _PLAN, path=db)
    rows = list_decisions(path=db)
    assert len(rows) == 2
    assert rows[0]["symbol"] == "SH 600519"  # 倒序
    filtered = list_decisions(symbol="SZ 002594", path=db)
    assert len(filtered) == 1
    assert filtered[0]["symbol"] == "SZ 002594"
