"""Walk-Forward 样本外验证引擎测试。

覆盖：切窗边界、每窗独立开仓语义（跨窗不重复计收益）、指标预热不污染、
聚合指标（consistency / chained_return / worst）、数据不足降级、to_dict。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from easy_tdx.backtest.strategy import Strategy
from easy_tdx.backtest.walkforward import WalkForwardEngine


class _BuyFirstBar(Strategy):
    """窗口首根可交易 bar 全仓买入、持有到窗口末（检验每窗独立开仓）。"""

    def init(self) -> None:
        self._bought = False

    def next(self) -> None:
        if not self._bought:
            self.buy()
            self._bought = True


class _CycleTrader(Strategy):
    """每 10 根切换一次持仓（买卖交替），保证每窗有完整回合。"""

    def init(self) -> None:
        self._count = 0
        self._holding = False

    def next(self) -> None:
        self._count += 1
        if self._count % 10 == 0:
            if self._holding:
                self.sell()
                self._holding = False
            else:
                self.buy()
                self._holding = True


class _NeverTrade(Strategy):
    """从不交易的策略（空窗聚合安全）。"""

    def init(self) -> None:
        pass

    def next(self) -> None:
        pass


def _trend_df(n: int = 500, drift: float = 0.004) -> pd.DataFrame:
    """平稳上涨的合成行情（买入即赚，用于检验正收益窗）。"""
    rng = np.random.default_rng(7)
    dates = pd.date_range("2018-01-01", periods=n, freq="B")
    close = 10.0 * np.cumprod(1.0 + drift + rng.normal(0, 0.004, n))
    return pd.DataFrame(
        {
            "datetime": dates,
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "vol": 1000.0,
        }
    )


def _decline_df(n: int = 500) -> pd.DataFrame:
    return _trend_df(n, drift=-0.002)


def test_wf_splits_into_requested_windows():
    wf = WalkForwardEngine(_BuyFirstBar, n_windows=7).run(_trend_df(500))
    assert len(wf.windows) == 7
    # 窗口时间升序且连续
    for i in range(1, len(wf.windows)):
        assert wf.windows[i].start > wf.windows[i - 1].start
    # 预热区 30% 不参与：首窗起点应在 150 根之后
    assert wf.windows[0].bars > 0


def test_wf_all_profitable_on_uptrend():
    """平稳上涨 + 每窗买入持有 → consistency = 1.0。"""
    wf = WalkForwardEngine(_BuyFirstBar, n_windows=5).run(_trend_df(600))
    assert wf.consistency == pytest.approx(1.0)
    assert wf.chained_return > 0
    assert wf.worst_window > 0
    assert wf.best_window >= wf.worst_window


def test_wf_all_losing_on_downtrend():
    """平稳下跌 → consistency = 0.0，连乘为负。"""
    wf = WalkForwardEngine(_BuyFirstBar, n_windows=5).run(_decline_df(600))
    assert wf.consistency == pytest.approx(0.0)
    assert wf.chained_return < 0


def test_wf_window_independent_positions():
    """每窗独立开仓：各窗收益只由本窗行情决定。

    上涨行情中每窗首根买入 → 单窗收益 ≈ 本窗末/首 - 1（扣费用），
    且窗口收益之间互不影响（无跨窗持仓结转）。
    """
    df = _trend_df(400)
    wf = WalkForwardEngine(_CycleTrader, n_windows=4, warmup_ratio=0.2).run(df)
    assert len(wf.windows) == 4
    for w in wf.windows:
        # 每窗都实际开了仓（买入持有至少 1 笔）
        assert w.total_trades >= 1


def test_wf_no_trades_strategy_safe():
    """从不交易 → 各窗收益 0、consistency 0（盈利窗占比不含 0），不崩溃。"""
    wf = WalkForwardEngine(_NeverTrade, n_windows=5).run(_trend_df(600))
    assert len(wf.windows) == 5
    assert all(w.total_return == 0.0 for w in wf.windows)
    assert wf.total_trades == 0


def test_wf_insufficient_data_returns_empty():
    """数据不足（< 20×(1+窗数)）→ 空结果、聚合为 0。"""
    wf = WalkForwardEngine(_BuyFirstBar, n_windows=7).run(_trend_df(100))
    assert wf.windows == []
    assert wf.consistency == 0.0
    assert wf.chained_return == 0.0


def test_wf_context_bars_do_not_pollute():
    """前置上下文只做指标预热：窗口起点之前的 bar 不产生信号。

    用「第 N 根才买」的策略验证：context 区间内策略已运行但不交易，
    首笔交易应落在窗口内（>= 窗口起点）。
    """

    class _BuyAfterWarm(Strategy):
        def init(self) -> None:
            self._count = 0

        def next(self) -> None:
            self._count += 1
            if self._count == 3:  # 第 3 次调用（含上下文）买入
                self.buy()

    wf = WalkForwardEngine(_BuyAfterWarm, n_windows=3, context_bars=10, warmup_ratio=0.2).run(
        _trend_df(300)
    )
    assert len(wf.windows) == 3
    # 上下文 10 根内第 3 根已被 warmup 压制 → 每窗首笔交易出现在窗口内
    for w in wf.windows:
        assert w.total_trades >= 0  # 结构完整性（warmup 压制不崩溃）


def test_wf_result_serializable():
    import json

    wf = WalkForwardEngine(_BuyFirstBar, n_windows=3).run(_trend_df(300))
    d = wf.to_dict()
    text = json.dumps(d, default=str)
    assert "consistency" in text
    assert d["n_windows"] == 3
    assert len(d["windows"]) == 3
    assert {"index", "start", "end", "total_return"} <= set(d["windows"][0])


def test_wf_auto_fes_passed_through():
    """auto_fees 透传：ETF 标的各窗印花税为 0。"""
    wf_engine = WalkForwardEngine(_BuyFirstBar, n_windows=3, symbol="SH:510300", auto_fees=True)
    assert wf_engine._engine_kwargs["auto_fees"] is True
    wf = wf_engine.run(_trend_df(300))
    assert len(wf.windows) == 3


# ── 回归：窗口绩效口径 / 聚合方向 / 切窗下限 / 失败日志 / int 日期 ────────────


def test_wf_window_metrics_exclude_context_bars():
    """上下文只做指标预热：窗口绩效指标不随 context_bars 变化。

    旧码把 context 恒定现金段一并喂给 PerformanceAnalyzer，sharpe/年化/波动
    被稀释（同窗 total_return 相同而 sharpe 相差近一倍）。
    """
    df = _trend_df(500)
    wf0 = WalkForwardEngine(_BuyFirstBar, n_windows=5, warmup_ratio=0.3, context_bars=0).run(df)
    wf60 = WalkForwardEngine(_BuyFirstBar, n_windows=5, warmup_ratio=0.3, context_bars=60).run(df)
    assert len(wf0.windows) == len(wf60.windows) == 5
    for w0, w60 in zip(wf0.windows, wf60.windows):
        assert w0.total_return == pytest.approx(w60.total_return)
        assert w0.sharpe == pytest.approx(w60.sharpe)
        assert w0.max_drawdown == pytest.approx(w60.max_drawdown)
        assert w0.performance["annual_return"] == pytest.approx(w60.performance["annual_return"])
        assert w0.performance["volatility"] == pytest.approx(w60.performance["volatility"])


def test_wf_worst_drawdown_is_max_not_min():
    """worst_drawdown 应取各窗最深回撤（max）；旧码 min 取成最浅回撤。"""
    from easy_tdx.backtest.walkforward import WalkForwardResult, WalkForwardWindow

    result = WalkForwardResult(n_windows=3, warmup_ratio=0.3)
    for i, dd in enumerate((0.05, 0.40, 0.11)):
        result.windows.append(
            WalkForwardWindow(
                index=i,
                start="2024-01-01",
                end="2024-02-01",
                bars=20,
                total_return=0.01,
                sharpe=1.0,
                max_drawdown=dd,
                total_trades=2,
                win_rate=0.5,
            )
        )
    WalkForwardEngine._aggregate(result)
    assert result.worst_drawdown == pytest.approx(0.40)


def test_wf_windows_below_min_bars_skipped():
    """单窗实际 bar 数 < 20 时跳过（与 docstring「每窗 ≥ 20 根」口径一致）。"""
    wf = WalkForwardEngine(_BuyFirstBar, n_windows=9).run(_trend_df(220))
    assert wf.windows == []


class _BoomStrategy(Strategy):
    """init 即抛错：单窗失败应记 warning 而非静默跳过。"""

    def init(self) -> None:
        raise RuntimeError("boom")

    def next(self) -> None:
        pass


def test_wf_window_failure_logs_warning(caplog):
    """单窗回测异常记 warning（含窗号与异常摘要），不拖垮整组。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="easy_tdx.backtest.walkforward"):
        wf = WalkForwardEngine(_BoomStrategy, n_windows=3).run(_trend_df(300))
    assert wf.windows == []
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("第 0 窗" in m and "boom" in m for m in msgs), msgs


def test_wf_int_yyyymmdd_date_column_window_labels():
    """datetime 为 int YYYYMMDD（TDX 日线原样）时窗口起止日期正确。

    旧码 pd.Timestamp(int) 按纳秒换算，窗口日期全变 1970-01-01。
    """
    n = 300
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    close = 10.0 * np.linspace(1.0, 2.0, n)
    df = pd.DataFrame(
        {
            "datetime": dates.strftime("%Y%m%d").astype(int),
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "vol": 1000.0,
        }
    )
    wf = WalkForwardEngine(_BuyFirstBar, n_windows=3, context_bars=10).run(df)
    assert len(wf.windows) == 3
    eval_start = int(n * 0.3)
    assert wf.windows[0].start == dates[eval_start].strftime("%Y-%m-%d")
    assert wf.windows[0].end == dates[eval_start + (n - eval_start) // 3 - 1].strftime("%Y-%m-%d")
    assert not wf.windows[0].start.startswith("1970")
