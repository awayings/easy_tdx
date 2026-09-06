"""CLI 参数校验与退出码测试（cmd_warehouse / cmd_formula，#审查修复）。

覆盖：
- ``市场:代码`` 解析辅助：缺冒号/空段 → click.BadParameter（而非裸 ValueError）；
- ``warehouse sync``：failed>0 时 exit 1（对齐 ccpm 口径）、summary 带 source 标注、
  ``--period`` Choice 限定、``--source baostock`` 不支持分钟周期时参数层报错；
- ``warehouse check``：先做「只支持一个标的」校验再解析，单标的缺冒号也报错；
- ``formula screen``：缺冒号标的前置报错（不再裸 traceback）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from easy_tdx.cli.cmd_formula import _parse_symbol as _parse_symbol_formula
from easy_tdx.cli.cmd_warehouse import (
    _BAOSTOCK_PERIODS,
    _PERIOD_CHOICES,
    warehouse_check,
    warehouse_sync,
)
from easy_tdx.cli.cmd_warehouse import (
    _parse_symbol as _parse_symbol_warehouse,
)


class TestParseSymbol:
    @pytest.mark.parametrize("parse", [_parse_symbol_formula, _parse_symbol_warehouse])
    def test_valid(self, parse):
        assert parse("SH:600519") == ("SH", "600519")
        assert parse(" sz:000001 ") == ("SZ", "000001")

    @pytest.mark.parametrize("parse", [_parse_symbol_formula, _parse_symbol_warehouse])
    @pytest.mark.parametrize(
        "bad", ["SH600519", "SH:", ":600519", ":", "SH 600519", "SH:600519:extra"]
    )
    def test_malformed_raises_bad_parameter(self, parse, bad):
        # 旧码：sym.split(":", 1) 裸 ValueError（"SH:600519:extra" 旧码能过但语义错，也收紧）
        from click import BadParameter

        with pytest.raises(BadParameter, match="市场:代码"):
            parse(bad)


class _FakeWarehouse:
    """context-manager 形假的 KlineWarehouse（cmd 只当透传对象用）。"""

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    def __enter__(self) -> _FakeWarehouse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def health_check(self, market: str | None = None, code: str | None = None) -> dict[str, Any]:
        return {"issues": [], "market": market, "code": code}


class _FakeSyncer:
    """可编程结果假 WarehouseSyncer（cmd_warehouse 从 easy_tdx.warehouse 导入它）。"""

    result: dict[str, Any] = {}

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    def sync(self, symbols: Any, period: str, progress: Any = None) -> dict[str, Any]:
        if progress is not None:
            progress(1, len(symbols), str(symbols[0]))
        return dict(self.result)


@pytest.fixture()
def patched_warehouse(monkeypatch):
    """打桩 cmd_warehouse 的全部外部依赖（仓库 / TDX 客户端 / 同步器）。"""
    import easy_tdx.cli.cmd_warehouse as cw
    import easy_tdx.cli.conn as conn_mod
    import easy_tdx.warehouse as wh_pkg

    class _FakeMacClient:
        def __enter__(self) -> _FakeMacClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(cw, "_require_warehouse", lambda db_path: _FakeWarehouse())
    monkeypatch.setattr(conn_mod, "get_mac_client", lambda: _FakeMacClient())
    monkeypatch.setattr(wh_pkg, "WarehouseSyncer", _FakeSyncer)
    return cw


class TestWarehouseSync:
    def _invoke(self, *args: str):
        return CliRunner().invoke(warehouse_sync, list(args), catch_exceptions=False)

    def test_failed_symbols_exit_1(self, patched_warehouse):
        """有标的失败 → exit 1（旧码：failed 只进 summary，命令仍 exit 0）。"""
        _FakeSyncer.result = {
            "total": 2,
            "ok": 1,
            "added": 3,
            "updated": 0,
            "skipped": 0,
            "failed": 1,
            "details": [
                {"symbol": "SH:600519", "added": 3, "updated": 0, "skipped": 0, "error": None},
                {"symbol": "SZ:000001", "added": 0, "updated": 0, "skipped": 0, "error": "boom"},
            ],
        }
        result = self._invoke("--symbols", "SH:600519,SZ:000001", "--source", "tdx")
        assert result.exit_code == 1, result.output

    def test_all_ok_exit_0_and_source_in_summary(self, patched_warehouse):
        """全部成功 → exit 0，summary JSON 带 source 标注（与 /bars 响应呼应）。"""
        _FakeSyncer.result = {
            "total": 1,
            "ok": 1,
            "added": 3,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "details": [
                {"symbol": "SH:600519", "added": 3, "updated": 0, "skipped": 0, "error": None},
            ],
        }
        result = self._invoke("--symbols", "SH:600519", "--source", "tdx")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)  # stdout 仅 summary JSON（进度/错误在 stderr）
        assert payload["source"] == "tdx"
        assert payload["ok"] == 1

    def test_malformed_symbol_no_bare_traceback(self, patched_warehouse):
        """缺冒号标的 → 友好 BadParameter（exit 2），不发网络请求不裸崩。"""
        result = CliRunner().invoke(warehouse_sync, ["--symbols", "SH600519", "--source", "tdx"])
        assert result.exit_code == 2
        assert "市场:代码" in result.output

    def test_period_choice_rejects_unknown(self, patched_warehouse):
        result = CliRunner().invoke(warehouse_sync, ["--symbols", "SH:600519", "--period", "WEEKN"])
        assert result.exit_code == 2

    def test_baostock_rejects_intraday_period(self, patched_warehouse):
        """--source baostock + 分钟周期 → 参数层直接报错（旧码：静默空转 exit 0）。"""
        for period in _PERIOD_CHOICES:
            if period not in _BAOSTOCK_PERIODS:
                result = CliRunner().invoke(
                    warehouse_sync,
                    ["--symbols", "SH:600519", "--source", "baostock", "--period", period],
                )
                assert result.exit_code == 2, (period, result.output)
                assert "baostock" in result.output

    def test_baostock_accepts_daily(self, patched_warehouse, monkeypatch):
        """--source baostock + DAILY 正常放行（不走 TDX 客户端）。"""
        _FakeSyncer.result = {
            "total": 1,
            "ok": 1,
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "details": [],
        }

        # baostock 路径不经过 get_mac_client——若被调用说明走错分支
        import easy_tdx.cli.conn as conn_mod

        def _no_tdx():
            raise AssertionError("baostock source 不应触碰 TDX 客户端")

        monkeypatch.setattr(conn_mod, "get_mac_client", _no_tdx)
        result = self._invoke("--symbols", "SH:600519", "--source", "baostock")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["source"] == "baostock"


class TestWarehouseCheck:
    def test_multiple_symbols_rejected_before_parse(self, monkeypatch):
        """多标的先报「只支持一个」，不再先 split 崩溃（旧码顺序颠倒）。"""
        import easy_tdx.cli.cmd_warehouse as cw

        monkeypatch.setattr(cw, "_require_warehouse", lambda db_path: _FakeWarehouse())
        result = CliRunner().invoke(
            warehouse_check,
            ["--symbols", "SH600519,SZ:000001"],  # 旧码：含冒号绕过校验 → split 裸崩
            catch_exceptions=False,
        )
        assert result.exit_code == 1
        assert "只支持一个标的" in result.output

    def test_single_symbol_missing_colon_rejected(self, monkeypatch):
        import easy_tdx.cli.cmd_warehouse as cw

        monkeypatch.setattr(cw, "_require_warehouse", lambda db_path: _FakeWarehouse())
        result = CliRunner().invoke(
            warehouse_check,
            ["--symbols", "SH600519"],  # 旧码：split(":", 1) 裸 ValueError
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "市场:代码" in result.output

    def test_single_valid_symbol_passes_market_code(self, monkeypatch):
        import easy_tdx.cli.cmd_warehouse as cw

        monkeypatch.setattr(cw, "_require_warehouse", lambda db_path: _FakeWarehouse())
        result = CliRunner().invoke(
            warehouse_check,
            ["--symbols", "SH:600519"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["market"] == "SH" and payload["code"] == "600519"

    def test_issues_still_exit_0_by_design(self, monkeypatch):
        """自检发现 issues → 正常输出并 exit 0（自检结果本身是正常输出，保持原口径）。"""
        import easy_tdx.cli.cmd_warehouse as cw

        class _Wh(_FakeWarehouse):
            def health_check(self, market=None, code=None):
                return {"issues": ["gap"]}

        monkeypatch.setattr(cw, "_require_warehouse", lambda db_path: _Wh())
        result = CliRunner().invoke(
            warehouse_check, ["--symbols", "SH:600519"], catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "gap" in result.output


class TestFormulaScreenSymbolValidation:
    def test_malformed_symbol_fails_fast(self):
        """缺冒号标的前置报错（旧码：循环里裸 ValueError traceback）。"""
        from easy_tdx.cli.cmd_formula import formula_screen

        result = CliRunner().invoke(
            formula_screen,
            ["--symbols", "SH:600519,SH600036", "--formula", "金叉: CROSS(MA(C,5), MA(C,20));"],
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "市场:代码" in result.output
        assert "SH600036" in result.output  # 报错指出坏标的
