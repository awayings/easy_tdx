"""决策日志存储层（docs/trading_system_tasks.md P1-T5）。

SQLite 库默认 ``~/.easy_tdx/decisions.db``（遵循 ``EASY_TDX_CONFIG_DIR``）。
每笔交易计划落一行：标的/计划全量 JSON（理由、风险评分、止损止盈点位与
事件条件、仓位）/当时特征快照 JSON/来源。特征快照必须落库——它是 P2-T1
事后回归（N 日后收益 → 胜率/盈亏比 → 可信度）的唯一数据基础。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    verdict TEXT,
    plan_json TEXT NOT NULL,
    snapshot_json TEXT,
    created_at TEXT NOT NULL,
    source TEXT,
    note TEXT
);
"""


def db_path(path: str | Path | None = None) -> Path:
    """decisions.db 路径（默认 ``<EASY_TDX_CONFIG_DIR|~/.easy_tdx>/decisions.db``）。"""
    if path is not None:
        return Path(path)
    base = Path(os.environ.get("EASY_TDX_CONFIG_DIR", str(Path.home() / ".easy_tdx")))
    return base / "decisions.db"


def open_db(path: str | Path | None = None) -> sqlite3.Connection:
    """打开决策库（自动建表）。调用方负责 close。"""
    conn = sqlite3.connect(db_path(path))
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def add_decision(
    symbol: str,
    plan: dict,
    *,
    snapshot: dict | None = None,
    source: str = "",
    note: str = "",
    date: str | None = None,
    path: str | Path | None = None,
) -> int:
    """写入一笔交易计划，返回自增 id。

    Args:
        symbol: 标的（"SZ 002594"）。
        plan: build_plan() 输出的完整计划 dict（含 features 时同时落 snapshot）。
        snapshot: 特征快照 dict；None 时取 plan["features"]。
    """
    features = snapshot if snapshot is not None else plan.get("features")
    conn = open_db(path)
    try:
        cur = conn.execute(
            "INSERT INTO decisions "
            "(date, symbol, verdict, plan_json, snapshot_json, created_at, source, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                date or plan.get("date") or datetime.now().strftime("%Y-%m-%d"),
                symbol,
                plan.get("verdict"),
                json.dumps(plan, ensure_ascii=False, default=str),
                json.dumps(features, ensure_ascii=False, default=str) if features else None,
                datetime.now().isoformat(timespec="seconds"),
                source,
                note,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_decision(decision_id: int, path: str | Path | None = None) -> dict | None:
    """按 id 读取一条决策记录（plan_json/snapshot_json 已反序列化）。"""
    conn = open_db(path)
    try:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["plan_json"] = json.loads(d["plan_json"])
        if d["snapshot_json"]:
            d["snapshot_json"] = json.loads(d["snapshot_json"])
        return d
    finally:
        conn.close()


def list_decisions(
    limit: int = 50,
    symbol: str | None = None,
    path: str | Path | None = None,
) -> list[dict]:
    """按时间倒序列出决策记录（不含大字段，仅摘要行）。"""
    conn = open_db(path)
    try:
        if symbol:
            rows = conn.execute(
                "SELECT id, date, symbol, verdict, source, note, created_at "
                "FROM decisions WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, date, symbol, verdict, source, note, created_at "
                "FROM decisions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
