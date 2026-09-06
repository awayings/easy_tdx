"""WatchlistStore（SQLite CRUD）与 QuoteStreamer（fan-out/背压）单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd
import pytest

from easy_tdx.models.enums import Market
from easy_tdx.web.quote_streamer import INDEX_SYMBOLS, QuoteStreamer, _is_trading_hours
from easy_tdx.web.watchlist_store import WatchlistStore

# ── WatchlistStore ──────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path) -> WatchlistStore:
    return WatchlistStore(db_path=tmp_path / "watchlist.db")


def test_add_list_remove_roundtrip(store: WatchlistStore) -> None:
    assert store.list_all() == []
    item = store.add("SH", "600000", name="浦发银行")
    assert item.symbol == "SH600000"
    store.add("SZ", "000001", name="平安银行")
    store.add("BJ", "920002", name="万达轴承")

    items = store.list_all()
    assert [i.symbol for i in items] == ["SH600000", "SZ000001", "BJ920002"]
    # 按加入顺序排列
    assert [i.sort_order for i in items] == [1, 2, 3]


def test_add_is_idempotent(store: WatchlistStore) -> None:
    store.add("SH", "600000", name="浦发银行")
    store.add("sh", "600000", name="浦发银行(更名)")  # 小写市场码归一
    items = store.list_all()
    assert len(items) == 1
    assert items[0].name == "浦发银行(更名)"
    assert items[0].sort_order == 1  # 幂等：不改变排序


def test_remove_missing_returns_false(store: WatchlistStore) -> None:
    assert store.remove("SZ", "399006") is False
    store.add("SZ", "399006", name="创业板指")
    assert store.remove("SZ", "399006") is True
    assert store.remove("SZ", "399006") is False


def test_symbols_for_streamer(store: WatchlistStore) -> None:
    store.add("SH", "600000", name="浦发银行")
    assert store.symbols() == [("SH", "600000")]


# ── QuoteStreamer ───────────────────────────────────────────────────────────


def _fake_df(symbols: list[tuple[Market, str]]) -> pd.DataFrame:
    rows = []
    for mkt, code in symbols:
        rows.append(
            {
                "market": mkt,
                "code": code,
                "price": 10.5,
                "pre_close": 10.0,
                "open": 10.1,
                "high": 10.8,
                "low": 9.9,
                "vol": 12345.0,
                "amount": 1_234_500.0,
                "bid1": 10.49,
                "bid_vol1": 100,
                "ask1": 10.51,
                "ask_vol1": 120,
                "bid2": 10.48,
                "bid_vol2": 90,
                "unknown_5": 0,  # 应被白名单过滤
            }
        )
    return pd.DataFrame(rows)


def _make_streamer() -> tuple[QuoteStreamer, list[list[tuple[Market, str]]]]:
    calls: list[list[tuple[Market, str]]] = []

    async def fetch(symbols: list[tuple[Market, str]]) -> pd.DataFrame:
        calls.append(symbols)
        return _fake_df(symbols)

    async def watch() -> list[tuple[Market, str]]:
        return [(Market.SZ, "000001")]

    return QuoteStreamer(fetch, watch, trading_interval=0.01, idle_interval=0.01), calls


def test_streamer_fanout_and_backpressure() -> None:
    streamer, calls = _make_streamer()
    qid1, q1 = streamer.subscribe()
    qid2, q2 = streamer.subscribe()

    async def run_once() -> None:
        streamer.start()
        await asyncio.sleep(0.05)  # 至少完成一轮轮询
        await streamer.stop()

    asyncio.run(run_once())

    assert calls, "应至少发起一次行情拉取"
    # 订阅集合 = 指数 + 自选
    assert (Market.SZ, "000001") in calls[0]
    assert set(INDEX_SYMBOLS).issubset(set(calls[0]))

    for q in (q1, q2):
        msg = q.get_nowait()
        assert msg["type"] == "quotes_updated"
        assert msg["count"] == len(calls[0])
        rec = msg["quotes"][0]
        assert rec["market"] in {"SH", "SZ", "BJ"}
        assert rec["symbol"]
        assert "unknown_5" not in rec  # 白名单生效
        assert rec["price"] == 10.5
        # 五档字段（bid_vol1 语义，非 bid1_vol）必须完整透传（Issue：盘口无数据）
        assert rec["bid1"] == 10.49
        assert rec["bid_vol1"] == 100
        assert rec["ask1"] == 10.51
        assert rec["ask_vol1"] == 120
        assert rec["bid_vol2"] == 90

    streamer.unsubscribe(qid1)
    streamer.unsubscribe(qid2)
    assert streamer.subscriber_count == 0


def test_streamer_backpressure_drops_oldest() -> None:
    """队列满（maxsize=2）时丢最旧保最新——第 3 条消息应顶掉第 1 条。"""
    streamer, _ = _make_streamer()
    qid, q = streamer.subscribe()
    for i in range(3):
        streamer._fan_out({"type": "quotes_updated", "seq": i})
    seqs = [q.get_nowait()["seq"] for _ in range(2)]
    assert seqs == [1, 2]
    streamer.unsubscribe(qid)


def test_is_trading_hours() -> None:
    from datetime import datetime, timedelta
    from datetime import timezone as dt_timezone

    tz = dt_timezone(timedelta(hours=8))
    assert _is_trading_hours(datetime(2026, 9, 1, 10, 0, tzinfo=tz)) is True  # 周二盘中
    assert _is_trading_hours(datetime(2026, 9, 1, 3, 0, tzinfo=tz)) is False  # 凌晨
    assert _is_trading_hours(datetime(2026, 9, 5, 10, 0, tzinfo=tz)) is False  # 周六


# ── /watchlist 端点 code 格式校验（v1.32.6）─────────────────────────────────


def _watch_app(monkeypatch, tmp_path):
    from fastapi import FastAPI

    from easy_tdx.web import watchlist_store as ws
    from easy_tdx.web.errors import register_exception_handlers
    from easy_tdx.web.routers import watchlist as watchlist_mod

    monkeypatch.setenv("EASY_TDX_CONFIG_DIR", str(tmp_path / "cfg"))
    ws._store = None

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(watchlist_mod.router, prefix="/api/v1")
    return app


def test_watchlist_add_rejects_non_numeric_code(monkeypatch, tmp_path):
    """code 非 6 位数字 → 422（旧实现可把 'abcdef' 存进自选并喂给轮询器）。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = _watch_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        bad = client.post("/api/v1/watchlist", json={"market": "SZ", "code": "abcdef", "name": "x"})
        assert bad.status_code == 422
        short = client.post(
            "/api/v1/watchlist", json={"market": "SZ", "code": "00001", "name": "x"}
        )
        assert short.status_code == 422
        ok = client.post(
            "/api/v1/watchlist", json={"market": "SZ", "code": "000001", "name": "平安银行"}
        )
        assert ok.status_code == 200


def test_watchlist_remove_validates_code_format(monkeypatch, tmp_path):
    """remove 路径 code 非 6 位数字 → 422，不触达存储。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = _watch_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        resp = client.delete("/api/v1/watchlist/SZ/abc123")
        assert resp.status_code == 422
