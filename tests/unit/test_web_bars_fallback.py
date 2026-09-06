"""/bars、/bars/index 的 baostock 兜底集成测试（v1.32.6 修复项）。

覆盖：
- 数字周期字符串（category="4"）归一后也能走兜底（旧实现直接透传原串，
  baostock 频率查表落空 → 兜底静默失效，维持原错误）；
- 指数兜底必须传 is_index=True（baostock 指数 vol 股→手），个股路径不传；
- fetch_bars 真故障抛 RuntimeError 时按"兜底不可用"处理，维持原 TDX 错误。
"""

from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

# ── 测试替身 ─────────────────────────────────────────────────────────────────


class _RaisingMac:
    async def get_stock_kline(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("MAC 连接失败")


class _RaisingTdx:
    async def get_security_bars(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("标准协议连接失败")

    async def get_index_bars(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("标准协议连接失败")


def _bars_app(mac_client, tdx_client):
    from fastapi import FastAPI

    from easy_tdx.web.errors import register_exception_handlers
    from easy_tdx.web.routers import bars

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(bars.router, prefix="/api/v1")
    app.state.tdx_client = tdx_client
    app.state.mac_client = mac_client
    return app


def _fallback_df(n: int = 5) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-09-04", periods=n)
    return pd.DataFrame(
        {
            "date": dates.normalize(),
            "open": [10.0] * n,
            "close": [10.5] * n,
            "high": [11.0] * n,
            "low": [9.5] * n,
            "vol": [100000.0] * n,
            "amount": [1050000.0] * n,
        }
    )


def _install_fake_bs_module(monkeypatch: pytest.MonkeyPatch, rows: int = 10) -> dict:
    """装一个最小可用的 baostock 模块替身，返回 captured 观测点。"""
    from easy_tdx.sources import baostock as bs_source

    captured: dict = {}

    def _login():
        lg = types.SimpleNamespace()
        lg.error_code = "0"
        lg.error_msg = "ok"
        return lg

    def query_history_k_data_plus(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        captured["calls"] = captured.get("calls", 0) + 1
        data = [
            [f"2026-08-{d:02d}", "10.0", "10.5", "11.0", "9.5", "100000", "1050000", "1"]
            for d in range(1, rows + 1)
        ]
        rs = types.SimpleNamespace()
        rs.error_code = "0"
        rs.error_msg = "ok"
        rs._rows = data
        rs._i = 0

        rs.next = lambda: rs._i < len(rs._rows)  # type: ignore[method-assign]
        rs.get_row_data = lambda: rs._rows[rs._i]  # type: ignore[method-assign]

        def _advance():
            row = rs._rows[rs._i]
            rs._i += 1
            return row

        rs.get_row_data = _advance  # type: ignore[method-assign]
        return rs

    mod = types.ModuleType("baostock")
    mod.login = _login  # type: ignore[attr-defined]
    mod.logout = lambda: None  # type: ignore[attr-defined]
    mod.query_history_k_data_plus = query_history_k_data_plus  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "baostock", mod)
    monkeypatch.delenv("EASY_TDX_BAOSTOCK", raising=False)
    monkeypatch.setattr(bs_source, "_logged_in", False)
    return captured


# ── 项11：数字周期字符串归一后再兜底 ──────────────────────────────────────────


def test_bars_numeric_category_still_falls_back(monkeypatch):
    """category="4"（=DAY 的数字形式）TDX 全败时也应命中 baostock 兜底。

    旧实现把原串 "4" 透传给 fetch_bars，_FREQ_BY_CATEGORY.get("4") 落空
    返回 None → 兜底静默失效，客户端拿到 500。
    """
    captured = _install_fake_bs_module(monkeypatch)
    with TestClient(
        _bars_app(_RaisingMac(), _RaisingTdx()), raise_server_exceptions=False
    ) as client:
        resp = client.get(
            "/api/v1/bars", params={"market": "SH", "code": "600519", "category": "4"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "baostock"
    assert body["count"] > 0
    assert captured["frequency"] == "d"  # 归一成 DAY 后映射到日线


# ── 项12：is_index 传递与异常语义 ────────────────────────────────────────────


def test_index_fallback_passes_is_index_true(monkeypatch):
    """/bars/index 兜底必须带 is_index=True（指数 vol 股→手 ÷100）。"""
    from easy_tdx.sources import baostock as bs_source

    calls: dict = {}

    def fake_fetch(market, code, category, start, count, adjust, is_index=False):
        calls["is_index"] = is_index
        return _fallback_df()

    monkeypatch.setattr(bs_source, "is_enabled", lambda: True)
    monkeypatch.setattr(bs_source, "fetch_bars", fake_fetch)

    with TestClient(_bars_app(None, _RaisingTdx())) as client:
        resp = client.get(
            "/api/v1/bars/index", params={"market": "SH", "code": "000001", "category": "DAY"}
        )
    assert resp.status_code == 200
    assert resp.json()["source"] == "baostock"
    assert calls["is_index"] is True


def test_bars_stock_fallback_keeps_is_index_false(monkeypatch):
    """个股路径兜底 is_index=False（vol 保持股口径）。"""
    from easy_tdx.sources import baostock as bs_source

    calls: dict = {}

    def fake_fetch(market, code, category, start, count, adjust, is_index=False):
        calls["is_index"] = is_index
        return _fallback_df()

    monkeypatch.setattr(bs_source, "is_enabled", lambda: True)
    monkeypatch.setattr(bs_source, "fetch_bars", fake_fetch)

    with TestClient(_bars_app(_RaisingMac(), _RaisingTdx())) as client:
        resp = client.get("/api/v1/bars", params={"market": "SH", "code": "600519"})
    assert resp.status_code == 200
    assert calls["is_index"] is False


def test_bars_fallback_exception_keeps_original_tdx_error(monkeypatch):
    """fetch_bars 真故障抛 RuntimeError → 按"兜底不可用"处理，重抛原 TDX 异常。

    响应错误详情须是标准协议的失败原因，而非 baostock 的失败原因（baostock
    的失败只记日志），且不返回空数据伪装成功。
    """
    from easy_tdx.sources import baostock as bs_source

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("baostock 拉取失败: 网络异常")

    monkeypatch.setattr(bs_source, "is_enabled", lambda: True)
    monkeypatch.setattr(bs_source, "fetch_bars", boom)

    with TestClient(
        _bars_app(_RaisingMac(), _RaisingTdx()), raise_server_exceptions=False
    ) as client:
        resp = client.get("/api/v1/bars", params={"market": "SH", "code": "600519"})
    assert resp.status_code == 500
    assert "标准协议连接失败" in resp.json()["detail"]
    assert "baostock" not in resp.json()["detail"]


def test_index_fallback_exception_keeps_original_tdx_error(monkeypatch):
    """/bars/index 同语义：baostock 异常不吞掉原 TDX 错误。"""
    from easy_tdx.sources import baostock as bs_source

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("baostock 拉取失败: 网络异常")

    monkeypatch.setattr(bs_source, "is_enabled", lambda: True)
    monkeypatch.setattr(bs_source, "fetch_bars", boom)

    with TestClient(_bars_app(None, _RaisingTdx()), raise_server_exceptions=False) as client:
        resp = client.get(
            "/api/v1/bars/index", params={"market": "SH", "code": "000001", "category": "DAY"}
        )
    assert resp.status_code == 500
    assert "标准协议连接失败" in resp.json()["detail"]
