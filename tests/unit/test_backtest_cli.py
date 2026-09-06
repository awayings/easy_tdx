"""回测 CLI 命令测试。"""

from __future__ import annotations

from click.testing import CliRunner


class TestBacktestCLI:
    """测试回测 CLI 命令。"""

    def test_help(self):
        """测试 --help 显示帮助。"""
        from easy_tdx.backtest.cli import backtest

        runner = CliRunner()
        result = runner.invoke(backtest, ["--help"])
        assert result.exit_code == 0
        assert "回测引擎" in result.output
        assert "--strategy-file" in result.output
        assert "--cash" in result.output
        assert "--commission" in result.output

    def test_missing_strategy_fails(self):
        """测试不指定策略则失败。"""
        from easy_tdx.backtest.cli import backtest

        runner = CliRunner()
        result = runner.invoke(backtest, ["SZ", "000001"])
        assert result.exit_code == 1
        assert "必须指定" in result.output or "错误" in result.output


class TestStrategiesCLI:
    """测试内置策略列表命令。"""

    def test_table_output(self):
        """表格输出应包含注册表策略与预设网格信息。"""
        from easy_tdx.backtest.cli import strategies

        runner = CliRunner()
        result = runner.invoke(strategies, [])
        assert result.exit_code == 0
        assert "内置策略" in result.output
        assert "ma_cross" in result.output
        assert "预设网格" in result.output

    def test_json_output(self):
        """JSON 输出应为策略 schema 列表（含 preset_grid）。"""
        import json

        from easy_tdx.backtest.cli import strategies

        runner = CliRunner()
        result = runner.invoke(strategies, ["--output", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        names = {entry["name"] for entry in data}
        assert "ma_cross" in names
        ma_cross = next(e for e in data if e["name"] == "ma_cross")
        assert ma_cross["preset_grid"]["fast"]
        assert all("default" in p for p in ma_cross["params"])


class TestOptimizeCLI:
    """测试参数寻优命令（校验逻辑，不联网）。"""

    def test_help(self):
        """测试 --help 显示帮助。"""
        from easy_tdx.backtest.cli import optimize

        runner = CliRunner()
        result = runner.invoke(optimize, ["--help"])
        assert result.exit_code == 0
        assert "--strategy" in result.output
        assert "--all" in result.output
        assert "--param" in result.output

    def test_strategy_and_all_mutually_exclusive(self):
        """--all 与 --strategy 同时指定应报错（联网之前快速失败）。"""
        from easy_tdx.backtest.cli import optimize

        runner = CliRunner()
        result = runner.invoke(optimize, ["SZ", "000001", "--strategy", "ma_cross", "--all"])
        assert result.exit_code == 1
        assert "二选一" in result.output

    def test_missing_mode_fails(self):
        """--all 与 --strategy 都不指定应报错。"""
        from easy_tdx.backtest.cli import optimize

        runner = CliRunner()
        result = runner.invoke(optimize, ["SZ", "000001"])
        assert result.exit_code == 1
        assert "二选一" in result.output

    def test_unknown_strategy_fails(self):
        """未知策略名应报错并列出可选值（联网之前快速失败）。"""
        from easy_tdx.backtest.cli import optimize

        runner = CliRunner()
        result = runner.invoke(optimize, ["SZ", "000001", "--strategy", "no_such_strat"])
        assert result.exit_code == 1
        assert "未知策略" in result.output
        assert "ma_cross" in result.output

    def test_unknown_param_fails(self):
        """--param 传不存在的参数名应报错（避免网格点被静默清空）。"""
        from easy_tdx.backtest.cli import optimize

        runner = CliRunner()
        result = runner.invoke(
            optimize,
            ["SZ", "000001", "--strategy", "ma_cross", "--param", "no_such=5,10"],
        )
        assert result.exit_code == 1
        assert "未知参数" in result.output

    def test_malformed_param_fails(self):
        """--param 缺少等号应报错。"""
        from easy_tdx.backtest.cli import optimize

        runner = CliRunner()
        result = runner.invoke(
            optimize,
            ["SZ", "000001", "--strategy", "ma_cross", "--param", "fast"],
        )
        assert result.exit_code == 1
        assert "错误" in result.output


class TestPortfolioCLIFlags:
    """测试组合回测命令的新增分析旗标。"""

    def test_help_includes_evaluate_and_wf(self):
        """--help 应列出 --evaluate / --wf / --auto-fees。"""
        from easy_tdx.backtest.cli import portfolio

        runner = CliRunner()
        result = runner.invoke(portfolio, ["--help"])
        assert result.exit_code == 0
        assert "--evaluate" in result.output
        assert "--wf" in result.output
        assert "--auto-fees" in result.output


class TestIgnoredFlagWarnings:
    """静默忽略的旗标组合必须显式告警（stderr），不得无提示吞掉。"""

    def test_combo_with_wf_warns(self):
        """--combo-strategies + --wf：旧码静默忽略，新码应告警并继续报策略文件错误。"""
        from easy_tdx.backtest.cli import backtest

        runner = CliRunner()
        result = runner.invoke(
            backtest,
            [
                "SZ",
                "000001",
                "--combo-strategies",
                "nope_a.py,nope_b.py",
                "--wf",
            ],
        )
        assert "已忽略" in result.output
        assert result.exit_code != 0  # 随后仍因策略文件不存在报错（无网络依赖）

    def test_combo_with_evaluate_warns(self):
        from easy_tdx.backtest.cli import backtest

        runner = CliRunner()
        result = runner.invoke(
            backtest,
            [
                "SZ",
                "000001",
                "--combo-strategies",
                "nope_a.py,nope_b.py",
                "--evaluate",
            ],
        )
        assert "已忽略" in result.output

    def test_single_with_wf_no_warning(self):
        """单策略 + --wf 是合法组合，不告警（在策略校验失败前无「已忽略」字样）。"""
        from easy_tdx.backtest.cli import backtest

        runner = CliRunner()
        result = runner.invoke(backtest, ["SZ", "000001", "--wf"])
        assert "已忽略" not in result.output

    def test_optimize_all_with_param_warns(self):
        """--all + --param：旧码静默忽略自定义网格，新码应告警。"""
        from easy_tdx.backtest.cli import optimize

        runner = CliRunner()
        # 用非法市场名在联网取数前中断，仅验证告警已发出
        result = runner.invoke(optimize, ["XX", "000001", "--all", "--param", "fast=5,10"])
        assert "忽略" in result.output

    def test_optimize_single_with_param_no_warning(self):
        from easy_tdx.backtest.cli import optimize

        runner = CliRunner()
        result = runner.invoke(
            optimize, ["XX", "000001", "--strategy", "ma_cross", "--param", "fast=5,10"]
        )
        assert "已忽略" not in result.output
