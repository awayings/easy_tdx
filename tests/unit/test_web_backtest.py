"""回测 Web API 测试（离线，无网络）。

覆盖：
- 策略注册表（Param schema、校验、序列化）
- 请求模型校验（数据来源二选一、参数范围）
- 结果序列化（numpy/datetime/NaN 清洗）
- 后台任务执行器（提交/轮询/失败/LRU 淘汰）
- router 端到端（策略枚举、同步回测、后台任务、错误路径）
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")


# ── 测试夹具 ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def sample_ohlcv() -> list[dict[str, object]]:
    """带趋势的合成 OHLCV（确保均线策略能产生交易）。"""
    np.random.seed(42)
    n = 300
    close = 10 + np.cumsum(np.random.randn(n) * 0.2 + 0.05)
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    return [
        {
            "datetime": d.strftime("%Y-%m-%d"),
            "open": float(c - np.random.rand() * 0.1),
            "high": float(c + np.random.rand() * 0.2),
            "low": float(c - np.random.rand() * 0.2),
            "close": float(c),
            "vol": float(np.random.randint(1000, 10000)),
            "amount": float(c * 5000),
        }
        for d, c in zip(dates, close, strict=True)
    ]


# ---------------------------------------------------------------------------
# 策略注册表
# ---------------------------------------------------------------------------


def test_registry_has_builtin_strategies():
    """导入 strategies 包后应有 5 个内置策略注册。"""
    from easy_tdx.backtest.strategies import get_registry

    reg = get_registry()
    names = reg.names()
    assert "ma_cross" in names
    assert "macd" in names
    assert "boll_breakout" in names
    assert "rsi_reversal" in names
    assert "kdj_cross" in names
    assert "fsl" in names
    assert len(names) >= 19


def test_strategy_schema_serialization():
    """策略 schema 应含 name/label/description/params 列表。"""
    from easy_tdx.backtest.strategies import get_registry

    entry = get_registry().get("ma_cross")
    schema = entry.to_schema()
    assert schema["name"] == "ma_cross"
    assert schema["label"] == "双均线交叉"
    assert isinstance(schema["description"], str)
    param_names = [p["name"] for p in schema["params"]]
    assert param_names == ["fast", "slow"]


def test_strategy_build_with_default_params():
    """无参数构造应使用默认值。"""
    from easy_tdx.backtest.strategies import get_registry

    entry = get_registry().get("ma_cross")
    inst = entry.build()
    assert inst.p["fast"] == 5
    assert inst.p["slow"] == 20


def test_strategy_build_with_custom_params():
    """自定义参数应覆盖默认值。"""
    from easy_tdx.backtest.strategies import get_registry

    inst = get_registry().get("ma_cross").build({"fast": 10, "slow": 30})
    assert inst.p["fast"] == 10
    assert inst.p["slow"] == 30


def test_strategy_rejects_unknown_param():
    """未知参数应抛 ValueError。"""
    from easy_tdx.backtest.strategies import get_registry

    with pytest.raises(ValueError, match="未知参数"):
        get_registry().get("ma_cross").build({"foo": 1})


def test_strategy_rejects_out_of_range_param():
    """超出范围的参数应抛 ValueError。"""
    from easy_tdx.backtest.strategies import get_registry

    with pytest.raises(ValueError, match="上限"):
        get_registry().get("ma_cross").build({"fast": 999})


def test_strategy_rejects_inverted_period_params():
    """快/慢周期倒挂（fast≥slow）应抛 ValueError（issue #39）。"""
    from easy_tdx.backtest.strategies import get_registry

    with pytest.raises(ValueError, match="必须小于"):
        get_registry().get("ma_cross").build({"fast": 30, "slow": 20})
    # 相等同样无效：两线重合，交叉永不触发
    with pytest.raises(ValueError, match="必须小于"):
        get_registry().get("ma_cross").build({"fast": 20, "slow": 20})


@pytest.mark.parametrize(
    "name,params",
    [
        ("ma_cross", {"fast": 30, "slow": 20}),
        ("ema_cross", {"fast": 30, "slow": 20}),
        ("macd", {"short": 30, "long": 20}),
        ("triple_ma", {"short": 30, "mid": 20}),
        ("triple_ma", {"short": 5, "mid": 80, "long": 60}),
        ("rsi_reversal", {"oversold": 60, "overbought": 50}),
        ("cci", {"oversold": 50, "overbought": -50}),
        ("wr_reversal", {"oversold": -50, "overbought": -60}),
    ],
)
def test_param_constraints_enforced_despite_skip_bounds(name, params):
    """跨参数语义约束不受 skip_bounds 影响（寻优防倒挂组合，issue #39）。"""
    from easy_tdx.backtest.strategies import get_registry

    with pytest.raises(ValueError, match="必须小于"):
        get_registry().get(name).build(params, skip_bounds=True)


def test_skip_bounds_still_allows_out_of_range_values():
    """skip_bounds 仍应放行超范围取值（只是不放过语义倒挂）。"""
    from easy_tdx.backtest.strategies import get_registry

    inst = get_registry().get("ma_cross").build({"fast": 100, "slow": 200}, skip_bounds=True)
    assert inst.p["fast"] == 100
    assert inst.p["slow"] == 200


def test_strategy_rejects_unknown_name():
    """未知策略名应抛 KeyError。"""
    from easy_tdx.backtest.strategies import get_registry

    with pytest.raises(KeyError, match="未知策略"):
        get_registry().get("not_a_real_strategy")


def test_param_type_coercion():
    """字符串传入 int 参数应自动转换。"""
    from easy_tdx.backtest.strategies import registry

    p = registry.Param("n", int, default=10, min_value=1, max_value=100)
    assert p.validate("15") == 15
    assert p.validate(20) == 20


def test_param_bool_coercion():
    """bool 参数接受多种真值表示。"""
    from easy_tdx.backtest.strategies import registry

    p = registry.Param("flag", bool, default=False)
    assert p.validate("true") is True
    assert p.validate("0") is False
    assert p.validate(True) is True


def test_param_choices_validation():
    """字符串型 choices 限制取值集合。"""
    from easy_tdx.backtest.strategies import registry

    p = registry.Param("mode", str, default="a", choices=("a", "b", "c"))
    assert p.validate("a") == "a"
    with pytest.raises(ValueError, match="可选范围"):
        p.validate("z")


# ---------------------------------------------------------------------------
# 请求模型校验
# ---------------------------------------------------------------------------


def test_backtest_request_requires_data_source():
    """请求必须提供 ohlcv 或 symbol 之一。"""
    from easy_tdx.web.backtest_schemas import BacktestRequest

    with pytest.raises(ValueError, match="ohlcv|symbol"):
        BacktestRequest(strategy="ma_cross")


def test_backtest_request_symbol_pattern():
    """symbol 必须符合 市场:代码 格式。"""
    from easy_tdx.web.backtest_schemas import BacktestRequest

    with pytest.raises(ValueError):
        BacktestRequest(strategy="ma_cross", symbol="000001")  # 缺市场前缀
    with pytest.raises(ValueError):
        BacktestRequest(strategy="ma_cross", symbol="XX:000001")  # 非法市场


def test_backtest_request_cash_must_be_positive():
    """初始资金必须 > 0。"""
    from easy_tdx.web.backtest_schemas import BacktestRequest

    with pytest.raises(ValueError):
        BacktestRequest(strategy="ma_cross", symbol="SZ:000001", cash=0)


def test_backtest_request_execution_enum():
    """execution 必须是预定义模式之一。"""
    from easy_tdx.web.backtest_schemas import BacktestRequest

    with pytest.raises(ValueError):
        BacktestRequest(strategy="ma_cross", symbol="SZ:000001", execution="invalid_mode")


def test_backtest_request_defaults():
    """默认值应合理。"""
    from easy_tdx.web.backtest_schemas import BacktestRequest

    req = BacktestRequest(strategy="ma_cross", symbol="SZ:000001")
    assert req.cash == 1_000_000.0
    assert req.commission == 0.0003
    assert req.execution == "next_open"
    assert req.category == "DAY"
    assert req.count == 250


# ---------------------------------------------------------------------------
# 结果序列化
# ---------------------------------------------------------------------------


def test_serialize_result_cleans_numpy():
    """numpy scalar 应被转为 Python 原生类型。"""
    from easy_tdx.web.backtest_schemas import serialize_result

    fake = {
        "performance": {"total_return": np.float64(0.5), "sharpe": np.int32(3)},
        "equity_curve": [{"equity": np.float64(100.0)}],
        "trades": [],
        "positions": [],
        "config": {},
    }
    out = serialize_result(fake)
    assert isinstance(out["performance"]["total_return"], float)
    assert isinstance(out["performance"]["sharpe"], int)
    assert isinstance(out["equity_curve"][0]["equity"], float)


def test_serialize_result_cleans_nan_inf():
    """NaN/Inf 应被转为 None（JSON 不支持）。"""
    from easy_tdx.web.backtest_schemas import serialize_result

    fake = {
        "performance": {"sharpe": float("nan"), "sortino": float("inf")},
        "equity_curve": [],
        "trades": [],
        "positions": [],
        "config": {},
    }
    out = serialize_result(fake)
    assert out["performance"]["sharpe"] is None
    assert out["performance"]["sortino"] is None


def test_serialize_result_cleans_datetime():
    """datetime/timestamp 应被转为 ISO 字符串。"""
    from easy_tdx.web.backtest_schemas import serialize_result

    ts = pd.Timestamp("2024-01-15")
    fake = {
        "performance": {},
        "equity_curve": [{"date": ts}],
        "trades": [],
        "positions": [],
        "config": {"end_date": ts},
    }
    out = serialize_result(fake)
    assert isinstance(out["equity_curve"][0]["date"], str)
    assert "2024-01-15" in out["equity_curve"][0]["date"]


# ---------------------------------------------------------------------------
# 后台任务执行器
# ---------------------------------------------------------------------------


def test_task_runner_submit_and_poll():
    """提交任务后应能轮询到 done 状态与结果。"""
    from easy_tdx.web.task_runner import BacktestTaskRunner

    runner = BacktestTaskRunner(max_workers=2)

    def work() -> dict[str, object]:
        return {"ok": True, "value": 42}

    task_id = runner.submit(work, description="test")
    # 轮询直到完成
    for _ in range(100):
        state = runner.get(task_id)
        if state.status in ("done", "failed"):
            break
        time.sleep(0.05)

    assert state.status == "done"
    assert state.result == {"ok": True, "value": 42}
    assert state.error is None
    assert state.started_at is not None
    assert state.finished_at is not None
    runner.shutdown()


def test_task_runner_captures_failure():
    """任务内异常应记入 error，状态变 failed。"""
    from easy_tdx.web.task_runner import BacktestTaskRunner

    runner = BacktestTaskRunner(max_workers=1)

    def boom() -> dict[str, object]:
        raise RuntimeError("boom")

    task_id = runner.submit(boom)
    for _ in range(100):
        state = runner.get(task_id)
        if state.status in ("done", "failed"):
            break
        time.sleep(0.05)

    assert state.status == "failed"
    assert "RuntimeError" in (state.error or "")
    assert "boom" in (state.error or "")
    runner.shutdown()


def test_task_runner_lru_eviction():
    """超限淘汰只移终态任务（v1.32.6：pending/running 不淘汰，防幽灵任务）。

    确定性设计：max_workers=1 串行。先提交 5 个并等全部 done（提交瞬间的
    淘汰因全是 pending 而跳过——这正是新语义），随后提交第 6 个触发淘汰：
    此时 5 个全是终态，按 LRU 淘到只剩 max_results=3（t3/t4/t5）。
    """
    from easy_tdx.web.task_runner import BacktestTaskRunner

    runner = BacktestTaskRunner(max_workers=1, max_results=3)
    ids = [runner.submit(lambda: {"i": i}, description=f"t{i}") for i in range(5)]
    # 等 5 个任务全部完成
    for _ in range(200):
        states = [runner.peek(tid) for tid in ids]
        if all(s is not None and s.status in ("done", "failed") for s in states):
            break
        time.sleep(0.02)

    # 全部完成后提交第 6 个 → 淘汰最旧的 3 个 done（t0/t1/t2）
    ids.append(runner.submit(lambda: {"i": 5}, description="t5"))
    assert runner.peek(ids[5]) is not None
    assert runner.peek(ids[0]) is None, "最旧的 done 应被淘汰"
    assert runner.peek(ids[1]) is None
    assert runner.peek(ids[2]) is None
    for tid in ids[3:]:
        assert runner.peek(tid) is not None, "最近的任务不应被淘汰"

    runner.shutdown()


def test_task_runner_unknown_task_raises():
    """查询不存在的 task_id 应抛 KeyError。"""
    from easy_tdx.web.task_runner import BacktestTaskRunner

    runner = BacktestTaskRunner(max_workers=1)
    with pytest.raises(KeyError, match="未知任务"):
        runner.get("nonexistent")
    assert runner.peek("nonexistent") is None
    assert runner.status("nonexistent") is None
    runner.shutdown()


def test_task_runner_does_not_evict_running(monkeypatch):
    """LRU 淘汰应跳过 running 任务——这是审计修复的关键并发正确性回归。

    若回退此修复，正在执行的长任务会被新提交的任务淘汰，导致 worker 在
    move_to_end 时抛 KeyError、任务结果丢失。
    """
    import easy_tdx.web.task_runner as tr_mod

    runner = tr_mod.BacktestTaskRunner(max_workers=1, max_results=3)

    # 用 Event 钉住第一个任务使其保持 running
    release = __import__("threading").Event()
    started = __import__("threading").Event()

    def slow_task() -> dict[str, object]:
        started.set()
        # 无短超时：CI 慢环境（如 windows 3.10）下 5s 可能不够整个测试跑完，
        # 任务会因超时自动完成导致状态变 done，掩盖「running 被淘汰」的回归。
        # release.set()（测试末尾）是唯一释放点；若回归发生，断言会先失败。
        release.wait(timeout=30)
        return {"slow": True}

    running_id = runner.submit(slow_task, description="slow")
    # 等它确实进入 running
    assert started.wait(timeout=2), "慢任务未启动"

    # 提交足够多的新任务触发淘汰（max_results=3）
    other_ids = [runner.submit(lambda: {"i": i}, description=f"t{i}") for i in range(5)]
    for _ in range(200):
        if all(runner.peek(t) and runner.peek(t).status in ("done", "failed") for t in other_ids):
            break
        time.sleep(0.02)

    # 关键断言：running 任务绝不能被淘汰
    assert runner.peek(running_id) is not None, "running 任务被错误淘汰！"
    assert runner.peek(running_id).status == "running"

    release.set()
    for _ in range(100):
        if runner.peek(running_id) and runner.peek(running_id).status in ("done", "failed"):
            break
        time.sleep(0.02)
    assert runner.peek(running_id).status == "done"
    runner.shutdown()


def test_task_runner_shutdown_is_idempotent_and_rejects_submit():
    """shutdown 应幂等，关闭后再 submit 应报错。"""
    from easy_tdx.web.task_runner import BacktestTaskRunner

    runner = BacktestTaskRunner(max_workers=1)
    runner.shutdown()
    # 幂等：再次 shutdown 不抛
    runner.shutdown()
    # 关闭后 submit 应被拒
    with pytest.raises(RuntimeError, match="已关闭"):
        runner.submit(lambda: {})


def test_task_runner_worker_tolerates_eviction():
    """worker 即使在运行期间被（理论上的）淘汰也不应抛未捕获异常。

    直接验证 _run 的 move_to_end 容忍 KeyError：手动从 _tasks 移除条目后
    触发完成路径。
    """
    from easy_tdx.web.task_runner import BacktestTaskRunner

    runner = BacktestTaskRunner(max_workers=1, max_results=10)

    completed = __import__("threading").Event()

    def quick() -> dict[str, object]:
        return {"ok": True}

    task_id = runner.submit(quick)
    for _ in range(100):
        if runner.peek(task_id) and runner.peek(task_id).status == "done":
            break
        time.sleep(0.01)
    # 任务完成后手动移除（模拟并发淘汰），再提交新任务不应触发任何 worker 异常
    with runner._lock:
        runner._tasks.pop(task_id, None)
    # 新任务正常工作
    tid2 = runner.submit(quick)
    for _ in range(100):
        if runner.peek(tid2) and runner.peek(tid2).status == "done":
            break
        time.sleep(0.01)
    assert runner.peek(tid2).status == "done"
    completed.set()
    runner.shutdown()


# ---------------------------------------------------------------------------
# 审计修复回归：参数校验安全（NaN/Inf/OverflowError）+ ohlcv 上限
# ---------------------------------------------------------------------------


def test_param_rejects_float_nan():
    """float 参数的 NaN 必须被拦（NaN 绕过比较，原实现漏网）。"""
    from easy_tdx.backtest.strategies.registry import Param

    p = Param("p", float, default=2.0, min_value=0.5, max_value=4.0)
    with pytest.raises(ValueError, match="NaN|Inf|期望"):
        p.validate(float("nan"))


def test_param_rejects_float_inf():
    """float 参数的 Inf 必须被拦。"""
    from easy_tdx.backtest.strategies.registry import Param

    p = Param("p", float, default=2.0, min_value=0.5, max_value=4.0)
    with pytest.raises(ValueError):
        p.validate(float("inf"))


def test_param_rejects_int_from_inf_without_overflow():
    """int(inf) 应报 ValueError（→400）而非 OverflowError（→500）。"""
    from easy_tdx.backtest.strategies.registry import Param

    p = Param("n", int, default=10, min_value=1, max_value=100)
    with pytest.raises(ValueError):  # 不能是 OverflowError
        p.validate(float("inf"))


def test_param_rejects_int_from_nan():
    """int(nan) 应报 ValueError（→400）而非 ValueError 逃逸。"""
    from easy_tdx.backtest.strategies.registry import Param

    p = Param("n", int, default=10, min_value=1, max_value=100)
    with pytest.raises(ValueError):
        p.validate(float("nan"))


def test_param_normal_values_still_work():
    """安全加固不应误伤正常值。"""
    from easy_tdx.backtest.strategies.registry import Param

    assert Param("n", int, default=10, min_value=1, max_value=100).validate(50) == 50
    assert Param("p", float, default=2.0, min_value=0.5, max_value=4.0).validate(2.0) == 2.0
    # 字符串数字仍可转换
    assert Param("n", int, default=10, min_value=1, max_value=100).validate("15") == 15


def test_backtest_request_ohlcv_max_length():
    """ohlcv 内联数据必须有上限（防 DoS）。"""
    from easy_tdx.web.backtest_schemas import BacktestRequest

    bar = {
        "datetime": "2024-01-01",
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 1,
        "vol": 1,
        "amount": 1,
    }
    # 2000 条（上限）应接受
    req = BacktestRequest(strategy="ma_cross", ohlcv=[bar] * 2000)
    assert len(req.ohlcv) == 2000
    # 2001 条应拒绝
    with pytest.raises(ValueError):
        BacktestRequest(strategy="ma_cross", ohlcv=[bar] * 2001)


# ---------------------------------------------------------------------------
# Router 端到端（TestClient）
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """FastAPI TestClient（不触发真实行情连接——lifespan 连接失败不影响回测路由）。"""
    from fastapi.testclient import TestClient

    from easy_tdx.web import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def test_list_strategies_endpoint(client):
    """GET /backtest/strategies 返回策略列表。"""
    resp = client.get("/api/v1/backtest/strategies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 18
    names = [s["name"] for s in body["strategies"]]
    assert "ma_cross" in names
    # 每个策略的 schema 结构完整
    for s in body["strategies"]:
        assert "label" in s
        assert "params" in s
        assert isinstance(s["params"], list)


def test_sync_backtest_with_inline_data(client, sample_ohlcv):
    """POST /backtest/run 用内联数据同步回测应返回完整结果。"""
    resp = client.post(
        "/api/v1/backtest/run",
        json={
            "strategy": "ma_cross",
            "params": {"fast": 5, "slow": 20},
            "cash": 100000,
            "ohlcv": sample_ohlcv,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 结果结构完整
    assert "performance" in body
    assert "equity_curve" in body
    assert "trades" in body
    assert "positions" in body
    assert "config" in body
    # 绩效指标存在且为原生类型
    assert "total_return" in body["performance"]
    assert isinstance(body["performance"]["total_return"], int | float)
    # 净值曲线有数据
    assert len(body["equity_curve"]) > 0


def test_sync_backtest_rejects_missing_ohlcv(client):
    """同步回测不给 ohlcv 应返回 400。"""
    resp = client.post(
        "/api/v1/backtest/run",
        json={"strategy": "ma_cross", "symbol": "SZ:000001"},
    )
    assert resp.status_code == 400


def test_sync_backtest_rejects_bad_strategy(client, sample_ohlcv):
    """未知策略名应返回 400。"""
    resp = client.post(
        "/api/v1/backtest/run",
        json={"strategy": "nope", "ohlcv": sample_ohlcv},
    )
    assert resp.status_code == 400


def test_sync_backtest_rejects_bad_params(client, sample_ohlcv):
    """非法参数应返回 400。"""
    resp = client.post(
        "/api/v1/backtest/run",
        json={
            "strategy": "ma_cross",
            "params": {"fast": 999},
            "ohlcv": sample_ohlcv,
        },
    )
    assert resp.status_code == 400


def test_async_backtest_with_inline_data(client, sample_ohlcv):
    """POST /backtest/run/async 提交后台任务，轮询到 done。"""
    resp = client.post(
        "/api/v1/backtest/run/async",
        json={
            "strategy": "macd",
            "params": {"short": 12, "long": 26, "signal": 9},
            "ohlcv": sample_ohlcv,
        },
    )
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]

    # 轮询
    final = None
    for _ in range(200):
        poll = client.get(f"/api/v1/backtest/tasks/{task_id}")
        assert poll.status_code == 200
        final = poll.json()
        if final["status"] in ("done", "failed"):
            break
        time.sleep(0.05)

    assert final is not None
    assert final["status"] == "done", final
    assert final["result"] is not None
    assert "performance" in final["result"]


def test_task_poll_unknown_id(client):
    """轮询不存在的 task_id 应返回 400（ValueError → 全局 handler）。"""
    resp = client.get("/api/v1/backtest/tasks/nonexistent")
    assert resp.status_code == 400
    assert "未知任务" in resp.json()["detail"]


def test_async_backtest_failure_recorded(client, monkeypatch):
    """后台任务内的异常应被记录为 failed 状态（而非 500 到客户端）。

    通过 monkeypatch 让回测执行函数抛错，验证 task_runner 的异常捕获在
    端到端链路里也生效。单元层 test_task_runner_captures_failure 已覆盖纯逻辑。
    """
    import easy_tdx.web.routers.backtest as bt_router

    def boom(_df, _req):  # noqa: ANN001
        raise RuntimeError("simulated backtest failure")

    monkeypatch.setattr(bt_router, "_run_backtest", boom)

    two_bars = [
        {
            "datetime": "2024-01-01",
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.2,
            "vol": 1000.0,
            "amount": 10000.0,
        },
        {
            "datetime": "2024-01-02",
            "open": 10.2,
            "high": 10.6,
            "low": 10.0,
            "close": 10.4,
            "vol": 1200.0,
            "amount": 12000.0,
        },
    ]
    resp = client.post(
        "/api/v1/backtest/run/async",
        json={"strategy": "ma_cross", "ohlcv": two_bars},
    )
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]

    final = None
    for _ in range(200):
        poll = client.get(f"/api/v1/backtest/tasks/{task_id}")
        final = poll.json()
        if final["status"] in ("done", "failed"):
            break
        time.sleep(0.05)

    assert final["status"] == "failed"
    assert "simulated backtest failure" in (final["error"] or "")


# ---------------------------------------------------------------------------
# Phase 3: 组合回测路由
# ---------------------------------------------------------------------------


def test_portfolio_request_validates_stocks_format():
    """组合请求的 stocks 字段格式校验。"""
    from easy_tdx.web.backtest_schemas import PortfolioBacktestRequest

    req = PortfolioBacktestRequest(strategy="ma_cross", stocks=["SZ:000001", "SH:600519"])
    assert len(req.stocks) == 2
    with pytest.raises(ValueError):
        PortfolioBacktestRequest(strategy="ma_cross", stocks=["000001"])  # 缺市场
    with pytest.raises(ValueError):
        PortfolioBacktestRequest(strategy="ma_cross", stocks=["SZ:123"])  # 非6位
    with pytest.raises(ValueError):
        PortfolioBacktestRequest(strategy="ma_cross", stocks=[])  # 空
    with pytest.raises(ValueError):
        many = [f"SZ:00000{i}" for i in range(21)]
        PortfolioBacktestRequest(strategy="ma_cross", stocks=many)  # >20


def test_portfolio_request_defaults():
    """组合请求默认值。"""
    from easy_tdx.web.backtest_schemas import PortfolioBacktestRequest

    req = PortfolioBacktestRequest(strategy="ma_cross", stocks=["SZ:000001"])
    assert req.cash == 1_000_000.0
    assert req.category == "DAY"


def test_portfolio_backtest_endpoint(client, monkeypatch):
    """POST /backtest/portfolio/run/async 端到端（mock 行情取数）。"""
    import pandas as pd

    import easy_tdx.web.routers.backtest as bt_router
    from easy_tdx.backtest.portfolio_engine import StockData

    np.random.seed(3)

    async def fake_fetch(client_arg, stocks, category, start, end):  # noqa: ANN001
        result = []
        for sym in stocks:
            mkt, code = sym.split(":")
            n = 100
            close = 10 + np.cumsum(np.random.randn(n) * 0.3 + 0.05)
            df = pd.DataFrame(
                {
                    "datetime": pd.date_range("2024-01-01", periods=n, freq="B"),
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "vol": np.full(n, 5000.0),
                    "amount": close * 5000,
                }
            )
            result.append(StockData(code=code, market=mkt, df=df))
        return result

    monkeypatch.setattr(bt_router, "_fetch_portfolio_bars", fake_fetch)

    resp = client.post(
        "/api/v1/backtest/portfolio/run/async",
        json={
            "strategy": "ma_cross",
            "params": {"fast": 5, "slow": 20},
            "cash": 200000,
            "stocks": ["SZ:000001", "SH:600519"],
        },
    )
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]

    final = None
    for _ in range(200):
        poll = client.get(f"/api/v1/backtest/tasks/{task_id}")
        final = poll.json()
        if final["status"] in ("done", "failed"):
            break
        time.sleep(0.05)

    assert final["status"] == "done", final
    result = final["result"]
    assert "total_performance" in result
    assert "individual_results" in result
    assert "equity_allocation" in result
    assert "combined_equity" in result
    assert result["total_performance"]["total_stocks"] == 2
    assert len(result["individual_results"]) == 2
    assert len(result["combined_equity"]) > 0


def test_portfolio_backtest_bad_strategy(client, monkeypatch):
    """未知策略在后台任务内抛错 → failed。"""
    import pandas as pd

    import easy_tdx.web.routers.backtest as bt_router
    from easy_tdx.backtest.portfolio_engine import StockData

    async def fake_fetch(client_arg, stocks, category, start, end):  # noqa: ANN001
        n = 10
        df = pd.DataFrame(
            {
                "datetime": pd.date_range("2024-01-01", periods=n, freq="B"),
                "open": np.full(n, 10.0),
                "high": np.full(n, 10.5),
                "low": np.full(n, 9.5),
                "close": np.full(n, 10.2),
                "vol": np.full(n, 1000.0),
                "amount": np.full(n, 10000.0),
            }
        )
        return [StockData(code="000001", market="SZ", df=df)]

    monkeypatch.setattr(bt_router, "_fetch_portfolio_bars", fake_fetch)

    resp = client.post(
        "/api/v1/backtest/portfolio/run/async",
        json={"strategy": "nope", "stocks": ["SZ:000001"]},
    )
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]
    for _ in range(200):
        poll = client.get(f"/api/v1/backtest/tasks/{task_id}")
        final = poll.json()
        if final["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert final["status"] == "failed"


# ---------------------------------------------------------------------------
# Phase 4: 参数网格寻优路由
# ---------------------------------------------------------------------------


def test_optimize_request_validation():
    """寻优请求校验：param_grid 非空、至少 1 个数据源。"""
    from easy_tdx.web.backtest_schemas import OptimizeBacktestRequest

    one_bar = [
        {
            "datetime": "2024-01-01",
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "vol": 1,
            "amount": 1,
        }
    ]
    req = OptimizeBacktestRequest(
        strategy="ma_cross",
        param_grid={"fast": [5, 10], "slow": [20, 30]},
        ohlcv=one_bar,
    )
    assert len(req.param_grid) == 2
    with pytest.raises(ValueError):
        OptimizeBacktestRequest(strategy="ma_cross", param_grid={"fast": [5]})  # 缺数据源
    with pytest.raises(ValueError):
        OptimizeBacktestRequest(  # param_grid > 2 参数
            strategy="ma_cross",
            param_grid={"a": [1], "b": [2], "c": [3]},
            ohlcv=one_bar,
        )


def test_optimize_endpoint(client, sample_ohlcv):
    """POST /backtest/optimize/run/async 端到端（内联数据）。"""
    resp = client.post(
        "/api/v1/backtest/optimize/run/async",
        json={
            "strategy": "ma_cross",
            "param_grid": {"fast": [5, 10], "slow": [20, 30]},
            "cash": 100000,
            "ohlcv": sample_ohlcv,
        },
    )
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]

    final = None
    for _ in range(200):
        poll = client.get(f"/api/v1/backtest/tasks/{task_id}")
        final = poll.json()
        if final["status"] in ("done", "failed"):
            break
        time.sleep(0.05)

    assert final["status"] == "done", final
    result = final["result"]
    assert result["strategy"] == "ma_cross"
    assert result["param_names"] == ["fast", "slow"]
    assert len(result["results"]) == 4
    assert result["best"] is not None
    assert result["heatmap"] is not None
    assert len(result["heatmap"]["data"]) == 4


def test_optimize_single_param_no_heatmap(client, sample_ohlcv):
    """单参数寻优不应返回热力图。"""
    resp = client.post(
        "/api/v1/backtest/optimize/run/async",
        json={
            "strategy": "rsi_reversal",
            "param_grid": {"n": [7, 14, 21]},
            "ohlcv": sample_ohlcv,
        },
    )
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]
    for _ in range(200):
        poll = client.get(f"/api/v1/backtest/tasks/{task_id}")
        final = poll.json()
        if final["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert final["status"] == "done"
    assert final["result"]["heatmap"] is None


def test_optimize_all_endpoint(client, sample_ohlcv):
    """POST /backtest/optimize-all/run/async 端到端：逐策略预设网格寻优 + 全局排名。"""
    resp = client.post(
        "/api/v1/backtest/optimize-all/run/async",
        json={
            "cash": 1_000_000,
            "ohlcv": sample_ohlcv,
        },
    )
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]

    final = None
    for _ in range(400):
        poll = client.get(f"/api/v1/backtest/tasks/{task_id}")
        final = poll.json()
        if final["status"] in ("done", "failed"):
            break
        time.sleep(0.05)

    assert final["status"] == "done", final
    result = final["result"]
    # 排名按 total_return 降序、best 指向第一名、各策略最优点齐全
    assert "ranking" in result and len(result["ranking"]) > 0
    assert "best" in result and result["best"] is not None
    assert "per_strategy" in result and len(result["per_strategy"]) == len(result["ranking"])
    assert "total_grid_points" in result and result["total_grid_points"] > 0
    # ranking 降序校验
    returns = [r["total_return"] for r in result["ranking"]]
    assert returns == sorted(returns, reverse=True)
    # best == ranking[0]
    assert result["best"]["strategy"] == result["ranking"][0]["strategy"]
    # 合计网格点 == 各策略 grid_points 之和
    assert result["total_grid_points"] == sum(r["grid_points"] for r in result["ranking"])


def test_optimize_all_request_validation():
    """optimize-all 请求必须提供数据源。"""
    from easy_tdx.web.backtest_schemas import OptimizeAllBacktestRequest

    # 缺数据源
    with pytest.raises(ValueError):
        OptimizeAllBacktestRequest()
    # 合法
    req = OptimizeAllBacktestRequest(symbol="SZ:000001")
    assert req.cash == 1_000_000.0
    assert req.execution == "next_open"


# ---------------------------------------------------------------------------
# Phase 5: 任务列表端点（对比页用）
# ---------------------------------------------------------------------------


def test_list_tasks_endpoint(client, sample_ohlcv):
    """GET /backtest/tasks 返回最近任务摘要列表。"""
    for _ in range(2):
        client.post(
            "/api/v1/backtest/run/async",
            json={"strategy": "ma_cross", "ohlcv": sample_ohlcv},
        )
    import time as _time

    _time.sleep(0.5)

    resp = client.get("/api/v1/backtest/tasks?limit=20")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 2
    task = body["tasks"][0]
    assert "task_id" in task
    assert "status" in task
    assert "description" in task
    assert "result" not in task  # 摘要不含完整 result


def test_list_tasks_limit(client, sample_ohlcv):
    """limit 参数应限制返回数量。"""
    for _ in range(3):
        client.post(
            "/api/v1/backtest/run/async",
            json={"strategy": "ma_cross", "ohlcv": sample_ohlcv},
        )
    import time as _time

    _time.sleep(0.5)

    resp = client.get("/api/v1/backtest/tasks?limit=2")
    assert resp.json()["count"] <= 2


# ── 组合级 WF / 一条龙评估端点（v1.31）────────────────────────────────────────
def _wait_task(client, task_id: str, rounds: int = 400):
    import time as _time

    final = None
    for _ in range(rounds):
        poll = client.get(f"/api/v1/backtest/tasks/{task_id}")
        final = poll.json()
        if final["status"] in ("done", "failed"):
            break
        _time.sleep(0.05)
    return final


def test_portfolio_backtest_includes_grade_score_trades(client, monkeypatch):
    """组合回测响应附带 grade/score（对齐单标的）与组合层 trades。"""
    import pandas as pd

    import easy_tdx.web.routers.backtest as bt_router
    from easy_tdx.backtest.portfolio_engine import StockData

    async def fake_fetch(client_arg, stocks, category, start, end):  # noqa: ANN001
        result = []
        for sym in stocks:
            mkt, code = sym.split(":")
            n = 100
            close = 10 + np.cumsum(np.random.randn(n) * 0.3 + 0.05)
            df = pd.DataFrame(
                {
                    "datetime": pd.date_range("2024-01-01", periods=n, freq="B"),
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "vol": np.full(n, 5000.0),
                    "amount": close * 5000,
                }
            )
            result.append(StockData(code=code, market=mkt, df=df))
        return result

    monkeypatch.setattr(bt_router, "_fetch_portfolio_bars", fake_fetch)

    resp = client.post(
        "/api/v1/backtest/portfolio/run/async",
        json={"strategy": "ma_cross", "cash": 200000, "stocks": ["SZ:000001", "SH:600519"]},
    )
    assert resp.status_code == 202, resp.text
    final = _wait_task(client, resp.json()["task_id"])
    assert final["status"] == "done", final
    result = final["result"]
    # 评级 + 评分（与单标的响应同构）
    assert result["grade"]["grade"] in ("S", "A", "B", "C", "D")
    assert result["grade"]["scenario"] == "portfolio"
    assert 0 <= result["score"]["total"] <= 100
    # 完整绩效指标（含 SQN/连胜连亏）+ 组合层成交
    assert "sqn" in result["total_performance"]
    assert "max_consecutive_losses" in result["total_performance"]
    assert isinstance(result["trades"], list)


def test_portfolio_walkforward_endpoint(client, monkeypatch):
    """POST /backtest/portfolio/wf/run/async 端到端（mock 行情取数）。"""
    import pandas as pd

    import easy_tdx.web.routers.backtest as bt_router
    from easy_tdx.backtest.portfolio_engine import StockData

    async def fake_fetch(client_arg, stocks, category, start, end):  # noqa: ANN001
        result = []
        for sym in stocks:
            mkt, code = sym.split(":")
            n = 400
            close = 10 + np.cumsum(np.random.randn(n) * 0.3 + 0.02)
            df = pd.DataFrame(
                {
                    "datetime": pd.date_range("2023-01-02", periods=n, freq="B"),
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "vol": np.full(n, 5000.0),
                    "amount": close * 5000,
                }
            )
            result.append(StockData(code=code, market=mkt, df=df))
        return result

    monkeypatch.setattr(bt_router, "_fetch_portfolio_bars", fake_fetch)

    resp = client.post(
        "/api/v1/backtest/portfolio/wf/run/async?n_windows=3",
        json={"strategy": "ma_cross", "cash": 200000, "stocks": ["SZ:000001", "SH:600519"]},
    )
    assert resp.status_code == 202, resp.text
    final = _wait_task(client, resp.json()["task_id"])
    assert final["status"] == "done", final
    wf = final["result"]["walkforward"]
    assert wf["n_windows"] == 3
    assert len(wf["windows"]) == 3
    assert "consistency" in wf
    assert "sqn" in wf["windows"][0]["performance"]


def test_portfolio_evaluate_endpoint(client, monkeypatch):
    """POST /backtest/portfolio/evaluate/run/async 端到端（mock 行情取数）。"""
    import pandas as pd

    import easy_tdx.web.routers.backtest as bt_router
    from easy_tdx.backtest.portfolio_engine import StockData

    async def fake_fetch(client_arg, stocks, category, start, end):  # noqa: ANN001
        result = []
        for sym in stocks:
            mkt, code = sym.split(":")
            n = 400
            close = 10 + np.cumsum(np.random.randn(n) * 0.3 + 0.02)
            df = pd.DataFrame(
                {
                    "datetime": pd.date_range("2023-01-02", periods=n, freq="B"),
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "vol": np.full(n, 5000.0),
                    "amount": close * 5000,
                }
            )
            result.append(StockData(code=code, market=mkt, df=df))
        return result

    monkeypatch.setattr(bt_router, "_fetch_portfolio_bars", fake_fetch)

    resp = client.post(
        "/api/v1/backtest/portfolio/evaluate/run/async",
        json={"strategy": "ma_cross", "cash": 200000, "stocks": ["SZ:000001", "SH:600519"]},
    )
    assert resp.status_code == 202, resp.text
    final = _wait_task(client, resp.json()["task_id"])
    assert final["status"] == "done", final
    report = final["result"]
    for key in ("performance", "score", "grade", "walkforward", "fitness", "benchmark", "config"):
        assert key in report
    assert report["grade"]["scenario"] == "portfolio"
    assert report["fitness"]["total_checks"] == 8
    assert report["config"]["stocks"] == ["SZ000001", "SH600519"]


# ── 多策略组合级 WF / 一条龙评估端点（v1.31.1）────────────────────────────────
def _fake_multi_slots(n: int = 400):
    """构造 _fetch_multi_strategy_bars 的 mock 替身（两槽位合成行情）。"""
    import pandas as pd

    from easy_tdx.backtest.multi_strategy_engine import StrategySlot

    async def fake_fetch(client_arg, items):  # noqa: ANN001
        slots = []
        for item in items:
            mkt, code = item.symbol.split(":")
            close = 10 + np.cumsum(np.random.randn(n) * 0.3 + 0.02)
            df = pd.DataFrame(
                {
                    "datetime": pd.date_range("2023-01-02", periods=n, freq="B"),
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "vol": np.full(n, 5000.0),
                    "amount": close * 5000,
                }
            )
            slots.append(
                StrategySlot(label=item.strategy, symbol=item.symbol, strategy=None, df=df)
            )
        return slots

    return fake_fetch


def _multi_request() -> dict[str, Any]:
    from datetime import date as _date

    start = f"{_date.today().year - 3}-01-02"
    return {
        "items": [
            {
                "strategy": "ma_cross",
                "strategy_label": "双均线交叉",
                "params": {"fast": 5, "slow": 20},
                "symbol": "SH:601088",
                "category": "DAY",
                "start_date": start,
            },
            {
                "strategy": "macd",
                "strategy_label": "MACD 金叉",
                "params": {},
                "symbol": "SZ:000001",
                "category": "DAY",
                "start_date": start,
            },
        ],
        "cash": 200000,
    }


def test_multi_strategy_wf_endpoint(client, monkeypatch):
    """POST /backtest/multi-strategy/wf/run/async 端到端（mock 取数 + 真实引擎）。

    StrategySlot 由取数阶段绑定策略实例（_build 时替换 mock 的 None），
    这里用 router 内的真实 _fetch_multi_strategy_bars 不可行（需 client），
    故 fake_fetch 直接构造策略实例。
    """
    import pandas as pd

    import easy_tdx.web.routers.backtest as bt_router
    from easy_tdx.backtest.multi_strategy_engine import StrategySlot
    from easy_tdx.backtest.strategies import get_registry

    async def fake_fetch(client_arg, items):  # noqa: ANN001
        registry = get_registry()
        slots = []
        for item in items:
            entry = registry.get(item.strategy)
            strategy = entry.build(item.params)
            close = 10 + np.cumsum(np.random.randn(400) * 0.3 + 0.02)
            df = pd.DataFrame(
                {
                    "datetime": pd.date_range("2023-01-02", periods=400, freq="B"),
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "vol": np.full(400, 5000.0),
                    "amount": close * 5000,
                }
            )
            slots.append(
                StrategySlot(
                    label=item.strategy_label or item.strategy,
                    symbol=item.symbol,
                    strategy=strategy,
                    df=df,
                )
            )
        return slots

    monkeypatch.setattr(bt_router, "_fetch_multi_strategy_bars", fake_fetch)

    resp = client.post(
        "/api/v1/backtest/multi-strategy/wf/run/async?n_windows=3",
        json=_multi_request(),
    )
    assert resp.status_code == 202, resp.text
    final = _wait_task(client, resp.json()["task_id"])
    assert final["status"] == "done", final
    wf = final["result"]["walkforward"]
    assert wf["n_windows"] == 3
    assert len(wf["windows"]) == 3
    assert "consistency" in wf
    assert "sqn" in wf["windows"][0]["performance"]


def test_multi_strategy_evaluate_endpoint(client, monkeypatch):
    """POST /backtest/multi-strategy/evaluate/run/async 端到端。"""
    import pandas as pd

    import easy_tdx.web.routers.backtest as bt_router
    from easy_tdx.backtest.multi_strategy_engine import StrategySlot
    from easy_tdx.backtest.strategies import get_registry

    async def fake_fetch(client_arg, items):  # noqa: ANN001
        registry = get_registry()
        slots = []
        for item in items:
            entry = registry.get(item.strategy)
            strategy = entry.build(item.params)
            close = 10 + np.cumsum(np.random.randn(400) * 0.3 + 0.02)
            df = pd.DataFrame(
                {
                    "datetime": pd.date_range("2023-01-02", periods=400, freq="B"),
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "vol": np.full(400, 5000.0),
                    "amount": close * 5000,
                }
            )
            slots.append(
                StrategySlot(
                    label=item.strategy_label or item.strategy,
                    symbol=item.symbol,
                    strategy=strategy,
                    df=df,
                )
            )
        return slots

    monkeypatch.setattr(bt_router, "_fetch_multi_strategy_bars", fake_fetch)

    resp = client.post(
        "/api/v1/backtest/multi-strategy/evaluate/run/async",
        json=_multi_request(),
    )
    assert resp.status_code == 202, resp.text
    final = _wait_task(client, resp.json()["task_id"])
    assert final["status"] == "done", final
    report = final["result"]
    for key in ("performance", "score", "grade", "walkforward", "fitness", "benchmark", "config"):
        assert key in report
    assert report["grade"]["scenario"] == "portfolio"
    assert report["fitness"]["total_checks"] == 8
    assert report["config"]["slots"] == ["双均线交叉@SH:601088", "MACD 金叉@SZ:000001"]


# ── submit 响应真实状态（v1.32.6 修复：不再把 done/failed 谎报为 running）──────


class _InstantDoneRunner:
    """submit 即同步跑完的假 runner（模拟"拿到 future 前任务已完成"）。"""

    def __init__(self, status: str = "done"):
        self.status = status

    def submit(self, func, *, description=""):
        func()  # 同步执行完毕
        return "tid-done"

    def get(self, task_id):
        from easy_tdx.web.task_runner import TaskState

        return TaskState(
            task_id=task_id,
            status=self.status,  # type: ignore[arg-type]
            result={
                "performance": {},
                "equity_curve": [],
                "trades": [],
                "positions": [],
                "config": {},
            },
            finished_at=1.0,
            started_at=0.0,
            created_at=0.0,
        )


def test_task_submit_response_accepts_terminal_status():
    """TaskSubmitResponse.status 应接受 done/failed（旧 Literal 只许 pending/running）。"""
    from easy_tdx.web.backtest_schemas import TaskSubmitResponse

    assert TaskSubmitResponse(task_id="x", status="done").status == "done"
    assert TaskSubmitResponse(task_id="x", status="failed").status == "failed"
    assert TaskSubmitResponse(task_id="x", status="pending").status == "pending"


def test_async_submit_reports_real_done_status(monkeypatch):
    """极快任务已 done 时，202 响应应透传真实状态 "done"（旧码谎报 "running"）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from easy_tdx.web.errors import register_exception_handlers
    from easy_tdx.web.routers import backtest as backtest_mod

    monkeypatch.setattr(backtest_mod, "get_runner", lambda: _InstantDoneRunner("done"))

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(backtest_mod.router, prefix="/api/v1")
    app.state.tdx_client = object()

    with TestClient(app) as tc:
        resp = tc.post(
            "/api/v1/backtest/run/async",
            json={
                "strategy": "ma_cross",
                "params": {"fast": 3, "slow": 6},
                "ohlcv": [
                    {
                        "datetime": f"2024-01-{d:02d}",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.5,
                        "close": 10.0,
                        "vol": 1000.0,
                        "amount": 10000.0,
                    }
                    for d in range(1, 6)
                ],
            },
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "done"


def test_async_submit_reports_failed_status(monkeypatch):
    """任务同步失败时响应应报 "failed" 而非 "running"。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from easy_tdx.web.errors import register_exception_handlers
    from easy_tdx.web.routers import backtest as backtest_mod

    class _FailingRunner(_InstantDoneRunner):
        def submit(self, func, *, description=""):
            try:
                func()
            except Exception:
                pass
            return "tid-fail"

    monkeypatch.setattr(backtest_mod, "get_runner", lambda: _FailingRunner("failed"))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(backtest_mod.router, prefix="/api/v1")
    app.state.tdx_client = object()

    with TestClient(app) as tc:
        resp = tc.post(
            "/api/v1/backtest/run/async",
            json={
                "strategy": "no_such_strategy",
                "ohlcv": [
                    {
                        "datetime": "2024-01-01",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.5,
                        "close": 10.0,
                        "vol": 1000.0,
                        "amount": 10000.0,
                    },
                    {
                        "datetime": "2024-01-02",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.5,
                        "close": 10.0,
                        "vol": 1000.0,
                        "amount": 10000.0,
                    },
                ],
            },
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "failed"


def test_formula_submit_reports_real_status(monkeypatch):
    """formula 回测提交响应透传真实状态（旧码硬编码 "running"）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from easy_tdx.web.errors import register_exception_handlers
    from easy_tdx.web.routers import formula as formula_mod

    monkeypatch.setattr(formula_mod, "get_runner", lambda: _InstantDoneRunner("done"))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(formula_mod.router, prefix="/api/v1")
    app.state.tdx_client = object()

    ohlcv = [
        {
            "datetime": f"2024-01-{d:02d}",
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.0,
            "vol": 1000.0,
        }
        for d in range(1, 6)
    ]
    with TestClient(app) as tc:
        resp = tc.post(
            "/api/v1/formula/backtest/run/async",
            json={"text": "CROSS(C, MA(C, 3));", "ohlcv": ohlcv},
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "done"


# ── optimize 费率口径（stamp_tax / min_commission / auto_fees 透传）───────────


def test_optimize_request_accepts_fee_fields():
    """OptimizeBacktestRequest 应支持 stamp_tax/min_commission/auto_fees（镜像单标的）。"""
    from easy_tdx.web.backtest_schemas import OptimizeBacktestRequest

    req = OptimizeBacktestRequest(
        strategy="ma_cross",
        param_grid={"fast": [3]},
        symbol="SZ:000001",
        stamp_tax=0.0,
        min_commission=1.0,
        auto_fees=True,
    )
    assert req.stamp_tax == 0.0
    assert req.min_commission == 1.0
    assert req.auto_fees is True
    with pytest.raises(ValueError):
        OptimizeBacktestRequest(
            strategy="ma_cross",
            param_grid={"fast": [3]},
            symbol="SZ:000001",
            stamp_tax=0.5,  # > le=0.01
        )


def test_optimize_auto_fees_etf_matches_explicit_fee_backtest(sample_ohlcv):
    """ETF + auto_fees：寻优结果的买入持有基准与"显式 ETF 费率"口径一致。

    旧实现不透传 stamp_tax/min_commission（恒按股票默认 0.001/5.0），品种
    口径无法生效。用可转债（佣金 0.0002 / 最低佣金 1.0 / 免印花税）验证：
    寻优结果的买入持有基准与"显式可转债费率"同口径，且与旧股票默认口径
    不同（_BuyAndHold 不卖出，印花税不进收益，差异来自佣金/最低佣金）。
    """
    from easy_tdx.backtest.benchmark import run_buy_hold_benchmark
    from easy_tdx.web.backtest_schemas import OptimizeBacktestRequest
    from easy_tdx.web.routers.backtest import _run_optimize

    df = pd.DataFrame(sample_ohlcv)
    df["datetime"] = pd.to_datetime(df["datetime"])
    req = OptimizeBacktestRequest(
        strategy="ma_cross",
        param_grid={"fast": [3], "slow": [10]},
        cash=1_000_000.0,
        symbol="SH:110059",  # 可转债：佣金 0.0002 / 最低佣金 1.0 / 免印花税
        auto_fees=True,
    )
    out = _run_optimize(df, req)

    # 期望口径：可转债费率
    expected = run_buy_hold_benchmark(
        df,
        cash=req.cash,
        commission=0.0002,
        min_commission=1.0,
        slippage=req.slippage,
        execution=req.execution,
    )
    legacy = run_buy_hold_benchmark(
        df,
        cash=req.cash,
        commission=req.commission,  # 0.0003（股票默认）
        min_commission=5.0,
        slippage=req.slippage,
        execution=req.execution,
    )
    assert out["buy_hold"] is not None
    assert out["buy_hold"]["total_return"] == pytest.approx(expected["total_return"])
    # 若与旧股票默认口径相同则说明 auto_fees 没生效
    assert out["buy_hold"]["total_return"] != pytest.approx(legacy["total_return"])


def test_optimize_passes_resolved_fees_to_optimizer(sample_ohlcv, monkeypatch):
    """auto_fees 解析出的费率应透传给 ParamGridOptimizer（显式值优先）。"""
    import easy_tdx.backtest.optimizer as opt_mod
    from easy_tdx.web.backtest_schemas import OptimizeBacktestRequest
    from easy_tdx.web.routers.backtest import _run_optimize

    captured: dict = {}

    class SpyOptimizer(opt_mod.ParamGridOptimizer):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(opt_mod, "ParamGridOptimizer", SpyOptimizer)

    df = pd.DataFrame(sample_ohlcv)
    df["datetime"] = pd.to_datetime(df["datetime"])
    req = OptimizeBacktestRequest(
        strategy="ma_cross",
        param_grid={"fast": [3], "slow": [10]},
        symbol="SH:510300",
        auto_fees=True,
    )
    _run_optimize(df, req)
    # ETF 口径：印花税解析为 0
    assert captured.get("stamp_tax") == 0.0
    assert captured.get("min_commission") == 5.0

    # 显式非默认 stamp_tax 优先于品种默认
    captured.clear()
    req2 = OptimizeBacktestRequest(
        strategy="ma_cross",
        param_grid={"fast": [3], "slow": [10]},
        symbol="SH:510300",
        auto_fees=True,
        stamp_tax=0.005,
    )
    _run_optimize(df, req2)
    assert captured.get("stamp_tax") == 0.005
