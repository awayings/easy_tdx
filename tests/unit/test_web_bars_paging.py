"""count>800 的分页取数测试（离线）。

TDX 协议单次 get_security_bars 最多返回 800 根：旧实现里 multiseed /
rotation / formula 的单次调用在 count>800 时被服务器静默截断。本文件钉死
"分页取全量 + 页序正确 + 数据起点提前停止"行为。
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

pytest.importorskip("fastapi")

_PAGE_CAP = 800


class _CappedBarsClient:
    """模拟 TDX 服务器：单次最多返回 _PAGE_CAP 根，start 为回看偏移。"""

    def __init__(self, total_bars: int = 3000):
        self.total_bars = total_bars
        self.calls: list[tuple[int, int]] = []  # (start, count)

    def _make_page(self, start: int, n: int) -> pd.DataFrame:
        """start 偏移处往前 n 根（升序页）；越过数据起点则截断为 0 根。"""
        hi = self.total_bars - start  # 本页最旧一根的全局序号（0 起）
        lo = max(0, hi - n)
        if hi <= 0:
            return pd.DataFrame()
        dates = pd.date_range("2020-01-01", periods=self.total_bars, freq="B")
        idx = dates[lo:hi]
        return pd.DataFrame(
            {
                "date": idx,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "vol": 1000.0,
                "amount": 10000.0,
            }
        )

    async def get_security_bars(self, market, code, category, start, count, **kw):
        self.calls.append((int(start), int(count)))
        return self._make_page(int(start), int(count))


# ── 共享分页辅助 ───────────────────────────────────────────────────────────────


def test_fetch_bars_paged_requests_multiple_pages():
    """count=2000 → 3 次请求（800/800/400），拼齐 2000 根且时间升序。"""
    from easy_tdx.web.routers.backtest import _fetch_bars_paged

    fake = _CappedBarsClient(total_bars=3000)
    df = asyncio.run(_fetch_bars_paged(fake, "SZ:000001", "DAY", 2000))

    assert fake.calls == [(0, 800), (800, 800), (1600, 400)]
    assert len(df) == 2000
    dates = pd.to_datetime(df["date"])
    assert dates.is_monotonic_increasing  # 页序拼接后必须升序


def test_fetch_bars_paged_stops_at_data_start():
    """数据起点不足一页时提前停止，不多发请求。"""
    from easy_tdx.web.routers.backtest import _fetch_bars_paged

    fake = _CappedBarsClient(total_bars=1000)
    df = asyncio.run(_fetch_bars_paged(fake, "SH:600519", "DAY", 2000))

    # 第二页只回 200 根（不足一页）= 数据起点，循环不再发第三笔请求
    assert fake.calls == [(0, 800), (800, 800)]
    assert len(df) == 1000


def test_fetch_bars_paged_small_count_single_call():
    """count≤800 仍单页取齐（不多打请求）。"""
    from easy_tdx.web.routers.backtest import _fetch_bars_paged

    fake = _CappedBarsClient(total_bars=3000)
    df = asyncio.run(_fetch_bars_paged(fake, "SZ:000001", "DAY", 250))
    assert fake.calls == [(0, 250)]
    assert len(df) == 250


def test_fetch_bars_paged_empty_returns_empty_df():
    from easy_tdx.web.routers.backtest import _fetch_bars_paged

    fake = _CappedBarsClient(total_bars=0)
    df = asyncio.run(_fetch_bars_paged(fake, "SZ:000001", "DAY", 800))
    assert df.empty


# ── multiseed / rotation 端点（取数在 handler 内完成，POST 返回即可断言）──────


def _app_with(fake_client):
    from fastapi import FastAPI

    from easy_tdx.web.errors import register_exception_handlers
    from easy_tdx.web.routers import backtest as backtest_mod

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(backtest_mod.router, prefix="/api/v1")
    app.state.tdx_client = fake_client
    app.state.mac_client = None
    app.state.ex_client = None
    return app


def test_multiseed_fetches_full_count_via_paging():
    """multiseed count=900（>800）→ 每标的 2 次请求，不再被 800 截断。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    fake = _CappedBarsClient(total_bars=3000)
    with TestClient(_app_with(fake)) as client:
        resp = client.post(
            "/api/v1/backtest/multiseed/run/async",
            json={
                "strategy": "ma_cross",
                "params": {"fast": 3, "slow": 6},
                "stocks": ["SZ:000001", "SH:600519"],
                "count": 900,
            },
        )
        assert resp.status_code == 202, resp.text

    # 2 标的 × 2 页
    assert fake.calls == [(0, 800), (800, 100), (0, 800), (800, 100)]


def test_rotation_fetches_full_count_via_paging():
    """rotation count=900（>800）→ 每标的 2 次请求。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    fake = _CappedBarsClient(total_bars=3000)
    with TestClient(_app_with(fake)) as client:
        resp = client.post(
            "/api/v1/backtest/rotation/run/async",
            json={
                "stocks": ["SZ:000001", "SH:600519"],
                "count": 900,
            },
        )
        assert resp.status_code == 202, resp.text

    assert fake.calls == [(0, 800), (800, 100), (0, 800), (800, 100)]


# ── formula 取数路径 ──────────────────────────────────────────────────────────


def test_formula_resolve_df_pages_full_count():
    """formula _resolve_df symbol 路径 count=2000 → 3 页拼齐且升序。"""
    from easy_tdx.web.routers.backtest import _fetch_bars_paged  # noqa: F401  需已存在
    from easy_tdx.web.routers.formula import FormulaComputeRequest, _resolve_df

    fake = _CappedBarsClient(total_bars=3000)
    df = asyncio.run(
        _resolve_df(fake, FormulaComputeRequest(text="C", symbol="SZ:000001", count=2000))
    )
    assert fake.calls == [(0, 800), (800, 800), (1600, 400)]
    assert len(df) == 2000
    dates = pd.to_datetime(df["date"])
    assert dates.is_monotonic_increasing
