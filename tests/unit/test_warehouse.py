"""本地 K 线仓库测试（DuckDB store + 增量 sync + provisional 状态机 + 健康自检）。"""

from __future__ import annotations

import datetime as _dt
import logging

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("duckdb")

from easy_tdx.warehouse.store import KlineWarehouse  # noqa: E402
from easy_tdx.warehouse.sync import WarehouseSyncer  # noqa: E402


@pytest.fixture()
def wh(tmp_path):
    warehouse = KlineWarehouse(tmp_path / "test.duckdb")
    yield warehouse
    warehouse.close()


def _bars(n: int = 10, start: str = "2024-01-01", base: float = 10.0) -> pd.DataFrame:
    dates = pd.date_range(start, periods=n, freq="B")
    close = base + np.linspace(0, 1, n)
    return pd.DataFrame(
        {
            "datetime": dates,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "vol": 1000.0,
            "amount": close * 1000,
        }
    )


class _FakeClient:
    """返回预置 K 线的假客户端（duck-typed get_stock_kline）。"""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df
        self.calls: list[dict] = []

    def get_stock_kline(self, market, code, period="DAILY", start=0, count=800, adjust="NONE"):
        self.calls.append({"market": market, "count": count, "adjust": adjust})
        return self._df.iloc[max(0, len(self._df) - count) :].reset_index(drop=True)


# ── store：写入 / 查询 ───────────────────────────────────────────────────────


def test_upsert_and_query_roundtrip(wh):
    df = _bars(10)
    added, updated = wh.upsert_bars("SH", "600519", df)
    assert (added, updated) == (10, 0)

    out = wh.query("SH", "600519")
    assert len(out) == 10
    assert list(out.columns)[:5] == ["market", "code", "period", "datetime", "open"]
    assert out["market"].iloc[0] == "SH"
    # 升序
    dts = pd.to_datetime(out["datetime"])
    assert dts.is_monotonic_increasing


def test_upsert_same_bars_updates_not_duplicates(wh):
    df = _bars(10)
    wh.upsert_bars("SH", "600519", df)
    # 同一批再写 → 全部 update，无重复行
    added, updated = wh.upsert_bars("SH", "600519", df)
    assert (added, updated) == (0, 10)
    assert len(wh.query("SH", "600519")) == 10


def test_query_count_takes_latest(wh):
    full = _bars(50)
    wh.upsert_bars("SZ", "000001", full)
    out = wh.query("SZ", "000001", count=10)
    assert len(out) == 10
    # 是最近 10 根（时间仍升序，且末根 = 全量末根）
    last_full = pd.Timestamp(full["datetime"].iloc[-1]).normalize()
    assert pd.Timestamp(out["datetime"].iloc[-1]) == last_full


def test_query_date_range_filter(wh):
    wh.upsert_bars("SZ", "000001", _bars(50))
    out = wh.query("SZ", "000001", start="2024-01-15", end="2024-01-25")
    dts = pd.to_datetime(out["datetime"]).dt.date.astype(str)
    assert (dts >= "2024-01-15").all() and (dts <= "2024-01-25").all()


def test_last_datetime_and_symbols(wh):
    assert wh.last_datetime("SH", "600519") is None
    wh.upsert_bars("SH", "600519", _bars(10))
    wh.upsert_bars("SZ", "000001", _bars(5))
    assert wh.last_datetime("SH", "600519") == pd.Timestamp("2024-01-12")
    syms = wh.symbols()
    assert len(syms) == 2
    assert set(syms["code"]) == {"600519", "000001"}


def test_delete_symbol(wh):
    wh.upsert_bars("SH", "600519", _bars(10))
    assert wh.delete_symbol("SH", "600519") == 10
    assert len(wh.query("SH", "600519")) == 0


def test_missing_optional_columns_filled(wh):
    df = _bars(5).drop(columns=["amount"])
    wh.upsert_bars("SH", "600519", df)
    out = wh.query("SH", "600519")
    assert out["amount"].isna().all()


# ── provisional 状态机 ───────────────────────────────────────────────────────


def test_today_bars_before_close_marked_provisional(wh, monkeypatch):
    """当日 bar 在 15:05 前落盘 → provisional（逐行判定），默认查询忽略。"""
    import datetime as _dt

    import easy_tdx.warehouse.store as store_mod

    today = pd.Timestamp.today().normalize()
    dates = pd.date_range(today - pd.Timedelta(days=10), periods=11, freq="D")
    df = pd.DataFrame(
        {
            "datetime": dates,
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "vol": 100.0,
            "amount": 1000.0,
        }
    )

    class _FixedDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # 固定在当日 10:00（盘中）
            return _dt.datetime(today.year, today.month, today.day, 10, 0)

    monkeypatch.setattr(store_mod, "datetime", _FixedDT)

    added, _ = wh.upsert_bars("SH", "600519", df)
    assert added == 11
    all_rows = wh.query("SH", "600519", include_provisional=True)
    completed = wh.query("SH", "600519")
    assert len(all_rows) == 11
    assert len(completed) == 10  # 仅当日 bar 是 provisional
    # 显式 include_provisional 时当日可见且标记正确
    today_rows = all_rows[all_rows["status"] == "provisional"]
    assert len(today_rows) == 1
    assert pd.Timestamp(today_rows["datetime"].iloc[0]).date() == today.date()


def test_promote_provisional(wh):
    """过期的 provisional 行（日期 < 今天）转正。"""

    old = pd.DataFrame(
        {
            "datetime": pd.date_range("2024-01-01", periods=3),
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "vol": 100.0,
            "amount": 1000.0,
        }
    )
    wh.upsert_bars("SH", "600519", old, status="provisional")
    assert len(wh.query("SH", "600519")) == 0  # provisional 默认不可见
    n = wh.promote_provisional()
    assert n >= 3
    assert len(wh.query("SH", "600519")) == 3  # 转正后可见


def test_promote_provisional_scoped_to_market_code_and_before(wh):
    """scoped 转正：只转正指定标的且 datetime <= before 的 provisional 行。"""

    def _one(d: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "datetime": pd.date_range(d, periods=1),
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "vol": 100.0,
                "amount": 1000.0,
            }
        )

    wh.upsert_bars("SH", "600519", _one("2024-01-05"), status="provisional")
    wh.upsert_bars("SH", "600519", _one("2024-06-01"), status="provisional")
    wh.upsert_bars("SZ", "000001", _one("2024-01-05"), status="provisional")

    n = wh.promote_provisional(market="SH", code="600519", before=pd.Timestamp("2024-03-01"))
    assert n == 1
    out = wh.query("SH", "600519")  # 默认查询只含 completed
    assert len(out) == 1
    assert pd.Timestamp(out["datetime"].iloc[0]) == pd.Timestamp("2024-01-05")
    all_rows = wh.query("SH", "600519", include_provisional=True)
    assert len(all_rows) == 2  # 2024-06-01 行超出 before，保持 provisional


def _fake_clock(
    store_mod,  # noqa: ANN001 — monkeypatch 目标模块（未用）
    *,
    shanghai: tuple[int, int, int, int],
    local: tuple[int, int, int, int],
):
    """伪造 store 模块时钟：now(tz)=沪时区正确墙钟；now()=本地误判墙钟。

    模拟「UTC 主机」：沪市已 18:00（当日 bar 应为 completed），本地 naive
    时钟却还是 10:00（旧实现会误标 provisional）。
    """

    class _FixedDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is not None:
                y, m, d, hh = shanghai
                return _dt.datetime(y, m, d, hh, 0, tzinfo=tz)
            y, m, d, hh = local
            return _dt.datetime(y, m, d, hh, 0)

    return _FixedDT


def test_provisional_uses_shanghai_clock_not_local(wh, monkeypatch):
    """provisional 判定按沪市墙钟：沪市 18:00（收盘后）当日 bar 必须 completed。

    回归：旧实现用系统本地 now()——UTC 主机上沪市收盘时本地才 10:00，
    当日 bar 被误标 provisional，默认查询隐藏当天数据。
    """
    import easy_tdx.warehouse.store as store_mod

    monkeypatch.setattr(
        store_mod,
        "datetime",
        _fake_clock(store_mod, shanghai=(2026, 9, 7, 18), local=(2026, 9, 7, 10)),
    )

    dates = pd.date_range(
        pd.Timestamp("2026-09-07") - pd.Timedelta(days=10), periods=11, freq="D"
    ).tolist()  # 2026-08-28 .. 2026-09-07（末根 = 沪市「当日」）
    df = pd.DataFrame(
        {
            "datetime": dates,
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "vol": 100.0,
            "amount": 1000.0,
        }
    )
    wh.upsert_bars("SH", "600519", df)
    # 沪市已收盘：全部 11 根都应为 completed（旧实现：当日根 provisional）
    assert len(wh.query("SH", "600519")) == 11


def test_promote_provisional_uses_shanghai_date(wh, monkeypatch):
    """无参转正的「今日」边界按沪市日期：沪市已过 0 点即转正昨日临时行。"""
    import easy_tdx.warehouse.store as store_mod

    monkeypatch.setattr(
        store_mod,
        "datetime",
        _fake_clock(store_mod, shanghai=(2026, 9, 8, 0), local=(2026, 9, 7, 16)),
    )

    old = pd.DataFrame(
        {
            "datetime": pd.date_range("2026-09-07", periods=1),
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "vol": 100.0,
            "amount": 1000.0,
        }
    )
    wh.upsert_bars("SH", "600519", old, status="provisional")
    # 沪市日期已是 9/8 → 9/7 的临时行应转正（旧实现按本地 9/7 → n=0）
    assert wh.promote_provisional() == 1
    assert len(wh.query("SH", "600519")) == 1


def test_open_conflict_clear_error(tmp_path, monkeypatch):
    """仓库文件被其他进程占用：给可操作的中文错误而非裸 duckdb 异常。"""
    import duckdb as duckdb_mod

    def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise duckdb_mod.IOException("Could not set lock on file")

    monkeypatch.setattr(duckdb_mod, "connect", _raise)
    with pytest.raises(RuntimeError, match="占用"):
        KlineWarehouse(tmp_path / "lock.duckdb")


# ── 健康自检 ─────────────────────────────────────────────────────────────────


def test_health_check_detects_gap_and_stale(wh):
    # 构造缺口：跳过 2 周
    df1 = _bars(5, start="2024-01-01")
    df2 = _bars(5, start="2024-03-01")
    wh.upsert_bars("SH", "600519", pd.concat([df1, df2], ignore_index=True))
    report = wh.health_check()
    assert report["symbols_checked"] == 1
    kinds = [i["kind"] for i in report["issues"]]
    assert "gap" in kinds  # 1 月→3 月的缺口
    assert report["summary"]["stale_symbols"]  # 2024 年数据 → 明显过期


def test_health_check_price_jump(wh):
    """除权式跳空被检出（kind=price_jump）。"""
    closes = [10.0] * 10 + [7.0] * 10
    dates = pd.date_range("2024-01-01", periods=20, freq="B")
    df = pd.DataFrame(
        {
            "datetime": dates,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "vol": 100.0,
            "amount": 1000.0,
        }
    )
    wh.upsert_bars("SH", "600519", df)
    report = wh.health_check(market="SH", code="600519")
    assert any(i["kind"] == "price_jump" for i in report["issues"])


def test_health_check_clean_series_no_issues(wh):
    """连续无跳空数据（工作日）→ 无 gap/price_jump 问题。"""
    wh.upsert_bars("SZ", "000001", _bars(30))
    report = wh.health_check(market="SZ", code="000001")
    assert report["issues"] == []


# ── 增量同步 ─────────────────────────────────────────────────────────────────


def test_sync_initial_full_then_incremental(tmp_path):
    warehouse = KlineWarehouse(tmp_path / "s.duckdb")
    try:
        full = _bars(100)
        client = _FakeClient(full)
        syncer = WarehouseSyncer(client, warehouse, max_bars=800, tail_bars=15)

        s1 = syncer.sync(["SH:600519"])
        assert s1["added"] == 100 and s1["failed"] == 0
        # 首同步请求了全量（count=800）
        assert client.calls[-1]["count"] == 800

        s2 = syncer.sync([("SH", "600519")])
        assert s2["added"] == 0 and s2["updated"] == 15  # 增量只补尾部 15 根
        assert client.calls[-1]["count"] == 15
        assert len(warehouse.query("SH", "600519")) == 100  # 无重复
    finally:
        warehouse.close()


def test_sync_new_bars_appended(tmp_path):
    warehouse = KlineWarehouse(tmp_path / "s2.duckdb")
    try:
        client = _FakeClient(_bars(50))
        syncer = WarehouseSyncer(client, warehouse, tail_bars=20)
        syncer.sync(["SZ:000001"])

        # 行情前滚 5 根：新 bar 接在原末根之后
        end = pd.Timestamp(client._df["datetime"].iloc[-1])
        client._df = pd.concat(
            [client._df, _bars(5, start=str(end + pd.Timedelta(days=1)))], ignore_index=True
        )
        s2 = syncer.sync(["SZ:000001"])
        assert s2["added"] == 5
        assert len(warehouse.query("SZ", "000001")) == 55
    finally:
        warehouse.close()


def test_sync_failure_does_not_break_batch(tmp_path):
    warehouse = KlineWarehouse(tmp_path / "s3.duckdb")
    try:

        class _BadClient:
            def get_stock_kline(self, *a, **kw):
                raise ConnectionError("网络故障")

        syncer = WarehouseSyncer(_BadClient(), warehouse)
        s = syncer.sync(["SH:600519", "SZ:000001"])
        assert s["failed"] == 2
        assert all(d["error"] for d in s["details"])
    finally:
        warehouse.close()


def test_sync_progress_callback(tmp_path):
    warehouse = KlineWarehouse(tmp_path / "s4.duckdb")
    try:
        client = _FakeClient(_bars(20))
        seen: list[tuple[int, int, str]] = []

        def progress(done, total, sym):
            seen.append((done, total, sym))

        WarehouseSyncer(client, warehouse).sync(["SH:600519", "SZ:000001"], progress=progress)
        assert seen == [(1, 2, "SH:600519"), (2, 2, "SZ:000001")]
    finally:
        warehouse.close()


class _ScriptedClient:
    """按调用序返回预置 DataFrame 的假客户端（末帧可重复）。"""

    def __init__(self, frames: list[pd.DataFrame]) -> None:
        self._frames = frames
        self.calls: list[int] = []

    def get_stock_kline(self, market, code, period="DAILY", start=0, count=800, adjust="NONE"):
        self.calls.append(count)
        idx = min(len(self.calls) - 1, len(self._frames) - 1)
        return self._frames[idx].copy()


def test_sync_refetch_full_when_tail_gap(tmp_path, caplog):
    """增量尾部覆盖不到上次同步点（首 bar 晚于 existing_last）→ 全量重拉补缺。

    回归：旧实现固定只拉 tail_bars 根——超过 15 个交易日未同步的标的，
    中间日期永不补齐且无任何告警。
    """
    warehouse = KlineWarehouse(tmp_path / "gap.duckdb")
    try:
        source_full = _bars(130)  # 2024-01-01 起 130 个工作日
        initial = source_full.iloc[:100]  # 首同步窗口（末根 idx99）
        stale_tail = source_full.iloc[115:]  # 增量窗口：首根 idx115 > idx99 → 有缺口
        client = _ScriptedClient([initial, stale_tail, source_full])
        syncer = WarehouseSyncer(client, warehouse, max_bars=800, tail_bars=15)

        with caplog.at_level(logging.WARNING, logger="easy_tdx.warehouse.sync"):
            syncer.sync(["SH:600519"])
            syncer.sync(["SH:600519"])

        assert client.calls == [800, 15, 800]  # 第二次 sync 触发了全量重拉
        rows = warehouse.query("SH", "600519")
        assert len(rows) == 130  # 无缺口
        bridge = pd.Timestamp(source_full["datetime"].iloc[100])
        dts = pd.to_datetime(rows["datetime"])
        assert (dts == bridge).any()  # 缺口桥接 bar 已补上
        assert "缺口" in caplog.text
    finally:
        warehouse.close()


def test_sync_failure_keeps_provisional(tmp_path):
    """拉取失败：不转正 provisional，盘中临时值不会被洗成 completed。"""
    warehouse = KlineWarehouse(tmp_path / "keep.duckdb")
    try:
        old = pd.DataFrame(
            {
                "datetime": pd.date_range("2024-01-01", periods=3),
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "vol": 100.0,
                "amount": 1000.0,
            }
        )
        warehouse.upsert_bars("SH", "600519", old, status="provisional")

        class _BadClient:
            def get_stock_kline(self, *a, **kw):
                raise ConnectionError("断网")

        s = WarehouseSyncer(_BadClient(), warehouse).sync(["SH:600519"])
        assert s["failed"] == 1
        # 仍为 provisional：默认查询不可见（旧实现 sync 前盲转正 → 可见）
        assert len(warehouse.query("SH", "600519")) == 0
        assert len(warehouse.query("SH", "600519", include_provisional=True)) == 3
    finally:
        warehouse.close()


def test_sync_promotes_only_up_to_fetched_max(tmp_path):
    """转正上界 = 本次成功拉到的最大 datetime：未覆盖到的行保持 provisional。"""
    warehouse = KlineWarehouse(tmp_path / "bound.duckdb")
    try:

        def _one(d: str) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "datetime": pd.date_range(d, periods=1),
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "vol": 100.0,
                    "amount": 1000.0,
                }
            )

        warehouse.upsert_bars("SH", "600519", _one("2024-01-05"), status="provisional")
        warehouse.upsert_bars("SH", "600519", _one("2024-06-01"), status="provisional")

        fetched = _bars(11, start="2024-01-10")  # 最大 datetime 2024-01-24
        client = _ScriptedClient([fetched])
        WarehouseSyncer(client, warehouse, tail_bars=15).sync(["SH:600519"])

        completed = warehouse.query("SH", "600519")
        # 01-05 行 <= 拉取上界 → 已转正；06-01 行超出上界 → 保持 provisional
        assert len(completed) == 12
        all_rows = warehouse.query("SH", "600519", include_provisional=True)
        assert len(all_rows) == 13
        stale = all_rows[all_rows["status"] == "provisional"]
        assert len(stale) == 1
        assert pd.Timestamp(stale["datetime"].iloc[0]) == pd.Timestamp("2024-06-01")
    finally:
        warehouse.close()


def test_missing_duckdb_helpful_error(tmp_path, monkeypatch):
    """duckdb 未安装时给出安装指引（模拟 ImportError）。"""
    import builtins

    real_import = builtins.__import__

    def _no_duckdb(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("No module named 'duckdb'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_duckdb)
    with pytest.raises(ImportError, match=r"easy-tdx\[warehouse\]"):
        KlineWarehouse(tmp_path / "x.duckdb")
