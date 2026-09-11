"""WatchlistStore（SQLite CRUD）与 QuoteStreamer（fan-out/背压）单元测试。"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any

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


# ── /watchlist/returns 端点（issue #7；mock 取数，不连网）────────────────────

_TODAY = date(2026, 9, 11)  # 周五


def _cal() -> list[date]:
    """15 个工作日（2026-08-24 ~ 2026-09-11）；T=09-11 → D_3=09-08 / D_5=09-04 / D_10=08-28。"""
    days: list[date] = []
    cur = date(2026, 8, 24)
    while len(days) < 15:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


_CAL = _cal()
_IDX_D3, _IDX_D5, _IDX_D10 = 11, 9, 4  # _CAL 中 09-08 / 09-04 / 08-28 的下标


def _ramp(cal: list[date], base: float = 10.0) -> list[tuple[date, float]]:
    return [(d, base + i) for i, d in enumerate(cal)]


class _FakeMac:
    """AsyncMacClient 替身：按 code 回预置日线；记录调用（校验 QFQ/count/缓存命中）。"""

    def __init__(
        self,
        series: dict[str, list[tuple[date, float]]],
        *,
        fail: tuple[str, ...] = (),
    ) -> None:
        self.series = series
        self.fail = set(fail)
        self.calls: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    async def get_stock_kline(
        self,
        market: Any,
        code: str,
        period: Any,
        start: int = 0,
        count: int = 800,
        times: int = 1,
        **kw: Any,
    ) -> pd.DataFrame:
        self.calls.append(code)
        self.kwargs.append({"market": market, "count": count, "times": times, **kw})
        if code in self.fail:
            raise RuntimeError("MAC 取数失败")
        rows = self.series.get(code)
        if rows is None:  # 板块代码 / 无数据
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "datetime": pd.to_datetime([d for d, _ in rows]),
                "close": [c for _, c in rows],
                "float_shares": 1.0,
            }
        )


class _FakeStd:
    """标准 TdxClient 替身（MAC 缺失时的降级路径）；返回 date 列（非 datetime）。"""

    def __init__(
        self,
        series: dict[str, list[tuple[date, float]]] | None = None,
        *,
        fail: tuple[str, ...] = (),
    ) -> None:
        self.series = series or {}
        self.fail = set(fail)
        self.calls: list[str] = []

    def _df(self, code: str) -> pd.DataFrame:
        self.calls.append(code)
        if code in self.fail:
            raise RuntimeError("标准客户端取数失败")
        rows = self.series.get(code)
        if rows is None:
            return pd.DataFrame()
        return pd.DataFrame(
            {"date": pd.to_datetime([d for d, _ in rows]), "close": [c for _, c in rows]}
        )

    async def get_index_bars(self, market: Any, code: str, *a: Any, **kw: Any) -> pd.DataFrame:
        return self._df(code)

    async def get_security_bars(self, market: Any, code: str, *a: Any, **kw: Any) -> pd.DataFrame:
        return self._df(code)


def _returns_app(
    monkeypatch: Any, tmp_path: Path, mac: Any, std: Any, today: date = _TODAY
) -> tuple[Any, Any]:
    """自选页应用：注入假 MAC / 假标准客户端 + 固定"今天"（不连网）。"""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI

    from easy_tdx.web import watchlist_store as ws
    from easy_tdx.web.errors import register_exception_handlers
    from easy_tdx.web.routers import watchlist as watchlist_mod

    monkeypatch.setenv("EASY_TDX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(watchlist_mod, "_today", lambda: today)
    ws._store = None  # 单例重建 → 用临时配置目录的 db
    watchlist_mod._calendar_cache.clear()  # 进程内缓存不跨测试复用
    watchlist_mod._bars_cache.clear()

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(watchlist_mod.router, prefix="/api/v1")
    app.state.mac_client = mac
    app.state.tdx_client = std
    return app, watchlist_mod


def test_watchlist_returns_ok(monkeypatch, tmp_path):
    """正常锚定：T + 三窗口锚点日期/收盘价，key 用 symbol，取数走 MAC + QFQ。"""
    from fastapi.testclient import TestClient

    from easy_tdx.mac.enums import Adjust

    mac = _FakeMac(
        {
            "000001": _ramp(_CAL, 3000.0),  # 上证指数（交易日历）
            "600519": _ramp(_CAL, 10.0),
            "002594": _ramp(_CAL, 20.0),
        }
    )
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    store = mod.get_watchlist_store()
    store.add("SH", "600519", name="贵州茅台")
    store.add("SZ", "002594", name="比亚迪")

    with TestClient(app) as client:
        resp = client.get("/api/v1/watchlist/returns")

    assert resp.status_code == 200
    body = resp.json()
    assert body["trade_date"] == "2026-09-11"  # T = 日历中 <= 今天的最后一个交易日
    assert set(body["items"]) == {"SH600519", "SZ002594"}
    item = body["items"]["SH600519"]
    assert item["last_close"] == pytest.approx(10.0 + 14)  # 09-11 的 close
    assert item["last_date"] == "2026-09-11"
    assert item["stale_days"] == 0
    assert [(a["days"], a["date"]) for a in item["anchors"]] == [
        (3, "2026-09-08"),
        (5, "2026-09-04"),
        (10, "2026-08-28"),
    ]
    assert item["anchors"][0]["close"] == pytest.approx(10.0 + _IDX_D3)
    assert item["anchors"][1]["close"] == pytest.approx(10.0 + _IDX_D5)
    assert item["anchors"][2]["close"] == pytest.approx(10.0 + _IDX_D10)
    # /bars 同款语义：MAC + QFQ + count=800
    assert {k["adjust"] for k in mac.kwargs} == {Adjust.QFQ}
    assert {k["count"] for k in mac.kwargs} == {800}
    assert set(mac.calls) == {"000001", "600519", "002594"}


def test_watchlist_returns_single_failure_isolated(monkeypatch, tmp_path):
    """单只失败（板块代码取不到）只在该 key 落 error，整表照常 200。"""
    from fastapi.testclient import TestClient

    mac = _FakeMac({"000001": _ramp(_CAL, 3000.0), "600519": _ramp(_CAL, 10.0)}, fail=("881001",))
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    store = mod.get_watchlist_store()
    store.add("SH", "600519", name="贵州茅台")
    store.add("SH", "881001", name="某板块")

    with TestClient(app) as client:
        resp = client.get("/api/v1/watchlist/returns")

    assert resp.status_code == 200  # 板块代码不得 500
    body = resp.json()
    # 失败项只有 error（None 字段不下发）
    assert body["items"]["SH881001"] == {"error": "no_data"}
    assert body["items"]["SH600519"]["anchors"][0]["days"] == 3


def test_watchlist_returns_fetch_failed_when_both_paths_raise(monkeypatch, tmp_path):
    """MAC 抛错 + 标准客户端也抛错 → 该只记 fetch_failed，其余照常。"""
    from fastapi.testclient import TestClient

    mac = _FakeMac({"000001": _ramp(_CAL, 3000.0)}, fail=("600519",))
    std = _FakeStd({"000001": _ramp(_CAL, 3000.0)}, fail=("600519",))
    app, mod = _returns_app(monkeypatch, tmp_path, mac, std)
    store = mod.get_watchlist_store()
    store.add("SH", "600519", name="贵州茅台")

    with TestClient(app) as client:
        resp = client.get("/api/v1/watchlist/returns")

    assert resp.status_code == 200
    assert resp.json()["items"]["SH600519"] == {"error": "fetch_failed"}


def test_watchlist_returns_insufficient_data_null_anchors(monkeypatch, tmp_path):
    """次新股（09-09 才上市）→ 三窗口 close 为 null（前端显示 '-'），不是 500。"""
    from fastapi.testclient import TestClient

    listed = [d for d in _CAL if d >= date(2026, 9, 9)]
    mac = _FakeMac({"000001": _ramp(_CAL, 3000.0), "301999": _ramp(listed, 30.0)})
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    mod.get_watchlist_store().add("SZ", "301999", name="次新股")

    with TestClient(app) as client:
        resp = client.get("/api/v1/watchlist/returns")

    assert resp.status_code == 200
    item = resp.json()["items"]["SZ301999"]
    assert item["anchors"] == [
        {"days": 3, "close": None, "date": None},
        {"days": 5, "close": None, "date": None},
        {"days": 10, "close": None, "date": None},
    ]
    assert item["last_date"] == "2026-09-11"


def test_watchlist_returns_suspended_stock_reports_stale(monkeypatch, tmp_path):
    """长期停牌：回 last_date + stale_days，锚点退到停牌前最后一根。"""
    from fastapi.testclient import TestClient

    halted = [(d, 8.0) for d in _CAL if d <= date(2026, 9, 4)]
    mac = _FakeMac({"000001": _ramp(_CAL, 3000.0), "600001": halted})
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    mod.get_watchlist_store().add("SH", "600001", name="停牌股")

    with TestClient(app) as client:
        resp = client.get("/api/v1/watchlist/returns")

    item = resp.json()["items"]["SH600001"]
    assert item["last_date"] == "2026-09-04"
    assert item["stale_days"] == 5  # 09-07 ~ 09-11
    assert item["anchors"][0]["date"] == "2026-09-04"


def test_watchlist_returns_cached_within_day(monkeypatch, tmp_path):
    """进程内缓存（个股日线 + 日历）：同一天第二次请求零行情请求。"""
    from fastapi.testclient import TestClient

    mac = _FakeMac({"000001": _ramp(_CAL, 3000.0), "600519": _ramp(_CAL, 10.0)})
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    mod.get_watchlist_store().add("SH", "600519", name="贵州茅台")

    with TestClient(app) as client:
        assert client.get("/api/v1/watchlist/returns").status_code == 200
        first = (mac.calls.count("000001"), mac.calls.count("600519"))
        assert client.get("/api/v1/watchlist/returns").status_code == 200
        second = (mac.calls.count("000001"), mac.calls.count("600519"))

    assert (first, second) == ((1, 1), (1, 1))


def test_watchlist_returns_cache_expires_next_day(monkeypatch, tmp_path):
    """缓存 TTL 到次日：跨日后重新取数（不返回昨日锚点）。"""
    from fastapi.testclient import TestClient

    mac = _FakeMac({"000001": _ramp(_CAL, 3000.0), "600519": _ramp(_CAL, 10.0)})
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    mod.get_watchlist_store().add("SH", "600519", name="贵州茅台")

    with TestClient(app) as client:
        client.get("/api/v1/watchlist/returns")
        monkeypatch.setattr(mod, "_today", lambda: _TODAY + timedelta(days=1))
        client.get("/api/v1/watchlist/returns")

    assert mac.calls.count("600519") == 2


def test_watchlist_returns_no_mac_degrades_to_standard_client(monkeypatch, tmp_path):
    """MAC 未连接 → 降级标准 TdxClient（不复权），仍正常返回（日志标注，不静默）。"""
    from fastapi.testclient import TestClient

    std = _FakeStd({"000001": _ramp(_CAL, 3000.0), "600519": _ramp(_CAL, 10.0)})
    app, mod = _returns_app(monkeypatch, tmp_path, None, std)
    mod.get_watchlist_store().add("SH", "600519", name="贵州茅台")

    with TestClient(app) as client:
        resp = client.get("/api/v1/watchlist/returns")

    assert resp.status_code == 200
    body = resp.json()
    assert body["trade_date"] == "2026-09-11"
    assert body["items"]["SH600519"]["anchors"][0]["date"] == "2026-09-08"
    assert "000001" in std.calls  # 日历走标准客户端的 get_index_bars


def test_watchlist_returns_empty_watchlist_no_request(monkeypatch, tmp_path):
    """空自选：直接返回空表，一个行情请求都不发。"""
    from fastapi.testclient import TestClient

    mac = _FakeMac({})
    app, _mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())

    with TestClient(app) as client:
        resp = client.get("/api/v1/watchlist/returns")

    assert resp.status_code == 200
    assert resp.json() == {"trade_date": None, "items": {}}
    assert mac.calls == []


def test_watchlist_returns_empty_calendar_returns_503(monkeypatch, tmp_path):
    """交易日历取不到（指数无数据）→ 503，不静默算错锚点。"""
    from fastapi.testclient import TestClient

    mac = _FakeMac({})  # 000001 也返回空
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    mod.get_watchlist_store().add("SH", "600519", name="贵州茅台")

    with TestClient(app) as client:
        resp = client.get("/api/v1/watchlist/returns")

    assert resp.status_code == 503


def test_watchlist_returns_today_not_trading_day(monkeypatch, tmp_path):
    """今日非交易日（周日）→ T 退回上一交易日，整表正常返回。"""
    from fastapi.testclient import TestClient

    mac = _FakeMac({"000001": _ramp(_CAL, 3000.0), "600519": _ramp(_CAL, 10.0)})
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd(), today=date(2026, 9, 13))
    mod.get_watchlist_store().add("SH", "600519", name="贵州茅台")

    with TestClient(app) as client:
        resp = client.get("/api/v1/watchlist/returns")

    assert resp.status_code == 200
    body = resp.json()
    assert body["trade_date"] == "2026-09-11"
    assert body["items"]["SH600519"]["anchors"][0]["date"] == "2026-09-08"


# ── 日历缓存的刷新时机（盘前启动的 serve 必须能等到今天的 bar） ────────────────


def _at(hour: int, minute: int = 0, second: int = 0) -> Any:
    """2026-09-11（周五）指定时刻的沪市时间。"""
    from datetime import datetime

    from easy_tdx.realtime.session import SHANGHAI_TZ

    return datetime(2026, 9, 11, hour, minute, second, tzinfo=SHANGHAI_TZ)


def test_watchlist_returns_calendar_refetched_after_open(monkeypatch, tmp_path):
    """盘前首取 → 日历缺今天 → 开盘后重取，``T`` 不再整体前移一个交易日。

    这是 serve 常驻 + 机器早开机的真实路径：盘前第一次取数时今天的日线 bar
    还没生成，若日历缓存当天不再刷新，三个锚点会一路错到次日且不报任何错。
    """
    from fastapi.testclient import TestClient

    pre_open = _CAL[:-1]  # 缺 09-11（今天的 bar 尚未生成）
    mac = _FakeMac({"000001": _ramp(pre_open, 3000.0), "600519": _ramp(_CAL, 10.0)})
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    mod.get_watchlist_store().add("SH", "600519", name="贵州茅台")

    # 盘前 08:30（非交易时段）：T 退回 09-10，锚点整体前移一天
    monkeypatch.setattr(mod, "_now", lambda: _at(8, 30))
    with TestClient(app) as client:
        before = client.get("/api/v1/watchlist/returns").json()
    assert before["trade_date"] == "2026-09-10"
    assert before["items"]["SH600519"]["anchors"][0]["date"] == "2026-09-07"

    # 开盘后 10:00（交易时段）：今天的 bar 已生成 → 重取日历 → T 回到今天
    mac.series["000001"] = _ramp(_CAL, 3000.0)
    monkeypatch.setattr(mod, "_now", lambda: _at(10, 0))
    with TestClient(app) as client:
        after = client.get("/api/v1/watchlist/returns?windows=3").json()
    assert after["trade_date"] == "2026-09-11"
    assert after["items"]["SH600519"]["anchors"][0]["date"] == "2026-09-08"


def test_watchlist_returns_calendar_not_refetched_when_confirmed(monkeypatch, tmp_path):
    """日历含今天 = 已确认：交易时段内重复请求也只取一次（不引入额外请求）。"""
    from fastapi.testclient import TestClient

    mac = _FakeMac({"000001": _ramp(_CAL, 3000.0), "600519": _ramp(_CAL, 10.0)})
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    mod.get_watchlist_store().add("SH", "600519", name="贵州茅台")
    monkeypatch.setattr(mod, "_now", lambda: _at(10, 0))

    with TestClient(app) as client:
        for _ in range(3):
            assert client.get("/api/v1/watchlist/returns").status_code == 200

    assert mac.calls.count("000001") == 1  # 日历只取一次


def test_watchlist_returns_calendar_not_refetched_outside_session(monkeypatch, tmp_path):
    """时段外（收盘后/节假日）缺今天不重试——bar 不可能再生成，避免无谓请求。"""
    from fastapi.testclient import TestClient

    pre_open = _CAL[:-1]
    mac = _FakeMac({"000001": _ramp(pre_open, 3000.0), "600519": _ramp(_CAL, 10.0)})
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    mod.get_watchlist_store().add("SH", "600519", name="贵州茅台")
    monkeypatch.setattr(mod, "_now", lambda: _at(20, 0))  # 收盘后

    with TestClient(app) as client:
        for _ in range(3):
            assert client.get("/api/v1/watchlist/returns").status_code == 200

    assert mac.calls.count("000001") == 1
    assert mod._calendar_cache["2026-09-11"][0][-1] == date(2026, 9, 10)


def test_calendar_stale_rules():
    """日历重取规则：含今天 / 时段外一律不重取；缺今天则按间隔重取。"""
    from easy_tdx.web.routers.watchlist import _CalendarEntry, _calendar_stale

    today = date(2026, 9, 11)
    no_today = [d for d in _CAL if d < today]  # "今天"的 bar 始终没生成

    # 含今天 = 已确认：永不重取（正常盘中路径，零额外请求）
    assert not _calendar_stale(_CalendarEntry(_CAL, _at(10, 0)), today, _at(15, 0))
    # 时段外：bar 不可能再生成，不重取
    assert not _calendar_stale(_CalendarEntry(no_today, _at(20, 0)), today, _at(20, 30))
    # 缺今天 + 盘中：未满间隔不重取，满了才重取
    assert not _calendar_stale(_CalendarEntry(no_today, _at(10, 0)), today, _at(10, 0, 59))
    assert _calendar_stale(_CalendarEntry(no_today, _at(10, 0)), today, _at(10, 1, 0))


def test_watchlist_returns_calendar_refresh_rate_limited(monkeypatch, tmp_path):
    """节假日（日历永远缺今天）：连续请求下日历重取被间隔限流，不是每个请求一次。"""
    from fastapi.testclient import TestClient

    no_today = _CAL[:-1]  # 永远是"今天的 bar 没生成"，等价于休市
    mac = _FakeMac({"000001": _ramp(no_today, 3000.0), "600519": _ramp(_CAL, 10.0)})
    app, mod = _returns_app(monkeypatch, tmp_path, mac, _FakeStd())
    mod.get_watchlist_store().add("SH", "600519", name="贵州茅台")

    with TestClient(app) as client:
        for second in range(0, 60, 10):  # 盘中 60 秒内每 10 秒来一次请求
            monkeypatch.setattr(mod, "_now", lambda s=second: _at(9, 15) + timedelta(seconds=s))
            assert client.get("/api/v1/watchlist/returns").status_code == 200

    # 6 次请求全部落在重取间隔内 → 日历与个股日线都只取了 1 次
    assert mac.calls.count("000001") == 1
    assert mac.calls.count("600519") == 1
