"""通达信公式解析器测试（tokenizer / AST / 白名单求值 / 信号归类 / 安全性）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from easy_tdx.formula import FormulaError, compile_formula


def _df(n: int = 60, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = 10.0 * np.cumprod(1.0 + 0.002 + rng.normal(0, 0.015, n))
    return pd.DataFrame(
        {
            "datetime": dates,
            "open": close * 0.999,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "vol": rng.uniform(1e6, 5e6, n),
            "amount": close * rng.uniform(1e6, 5e6, n),
        }
    )


# ── 编译与语法 ───────────────────────────────────────────────────────────────


def test_compile_and_outputs():
    formula = compile_formula(
        """
        N := 9;
        RSV := (C - LLV(L, N)) / (HHV(H, N) - LLV(L, N)) * 100;
        K := SMA(RSV, 3, 1);
        金叉: CROSS(K, 20);
        强度: K;
        """
    )
    res = formula.compute(_df())
    assert "金叉" in res.signals
    assert "强度" in res.values
    frame = res.to_frame()
    assert list(frame.columns) == ["金叉", "强度"]
    assert len(frame) == 60


def test_syntax_error_has_position():
    with pytest.raises(FormulaError):
        compile_formula("A := ;")
    with pytest.raises(FormulaError):
        compile_formula("A := UNKNOWN_FUNC(C)")
    with pytest.raises(FormulaError):
        compile_formula("A := B + ")  # 引用未定义变量且语法断裂


def test_unknown_variable_rejected():
    with pytest.raises(FormulaError, match="未知变量"):
        compile_formula("A: X1;").compute(_df())


def test_unknown_function_rejected():
    with pytest.raises(FormulaError, match="白名单"):
        compile_formula("A: EVAL(C);").compute(_df())


def test_empty_formula_rejected():
    with pytest.raises(FormulaError, match="为空"):
        compile_formula("{只有注释}")


def test_no_python_eval_injection():
    """公式层不走 Python eval：危险标识符按未知变量/函数拒绝。"""
    with pytest.raises(FormulaError):
        compile_formula("__import__('os'): 1;").compute(_df())
    with pytest.raises(FormulaError):
        compile_formula("A: OPEN(C);").compute(_df())


# ── 语义正确性 ───────────────────────────────────────────────────────────────


def test_series_aliases():
    """C/O/H/L/V/AMOUNT 别名与底层列一致。"""
    df = _df(50)
    res = compile_formula("高价: H; 低价: L; 收盘: CLOSE; 量: VOL; 额: AMOUNT;").compute(df)
    assert np.allclose(res.columns["高价"], df["high"])
    assert np.allclose(res.columns["收盘"], df["close"])
    assert np.allclose(res.columns["量"], df["vol"])


def test_ma_matches_mytt():
    from easy_tdx.MyTT import MA

    df = _df(50)
    res = compile_formula("均线: MA(C, 5);").compute(df)
    assert np.allclose(res.columns["均线"], MA(df["close"].to_numpy(), 5), equal_nan=True)


def test_cross_semantics():
    """CROSS(A,B)：A 上穿 B 的那一根为 1，其余 0。"""
    df = _df(50)
    res = compile_formula(
        """
        快: MA(C, 3);
        慢: MA(C, 10);
        金叉: CROSS(快, 慢);
        """
    ).compute(df)
    golden = res.columns["金叉"]
    assert set(np.unique(golden[np.isfinite(golden)])).issubset({0.0, 1.0})
    assert golden.sum() >= 0  # 结构完整（趋势数据至少存在或为 0）
    # CROSS 手工复算对拍
    from easy_tdx.MyTT import CROSS, MA

    fast = MA(df["close"].to_numpy(), 3)
    slow = MA(df["close"].to_numpy(), 10)
    assert np.allclose(golden, CROSS(fast, slow), equal_nan=True)


def test_safe_division_zero_denominator_nan():
    """除零 → NaN（不炸、不 inf）。"""
    df = _df(30)
    res = compile_formula("比值: C / (C - C);").compute(df)  # 分母全 0
    assert np.isnan(res.columns["比值"]).all()


def test_logic_operators():
    df = _df(40)
    res = compile_formula(
        """
        条件1: C > MA(C, 5);
        条件2: C > MA(C, 20);
        同时: 条件1 AND 条件2;
        任一: 条件1 OR 条件2;
        取反: NOT(条件1);
        """
    ).compute(df)
    c1 = res.columns["条件1"] > 0.5
    c2 = res.columns["条件2"] > 0.5
    assert np.allclose(res.columns["同时"] > 0.5, c1 & c2)
    assert np.allclose(res.columns["任一"] > 0.5, c1 | c2)
    assert np.allclose(res.columns["取反"] > 0.5, ~c1)


def test_comparison_and_unary():
    df = _df(30)
    res = compile_formula("跌幅: -(C - REF(C, 1)) / REF(C, 1) * 100; 平: C == C;").compute(df)
    assert (res.columns["平"] == 1.0).all()
    assert "跌幅" in res.values


def test_warmup_nan_not_signal():
    """预热期 NaN 不产生信号（比较含 NaN → 0）。"""
    df = _df(30)
    res = compile_formula("信号: CROSS(MA(C, 20), MA(C, 25));").compute(df)
    sig = res.columns["信号"]
    assert np.nanmax(np.nan_to_num(sig[:25])) <= 1.0
    assert np.isnan(sig).sum() == 0  # 布尔输出不含 NaN


def test_output_classification_boolean_vs_numeric():
    """比较/逻辑输出 → 信号；数值输出 → 数值列；0/1 值域数值也归信号。"""
    df = _df(40)
    res = compile_formula(
        """
        布尔输出: C > REF(C, 1);
        数值输出: MA(C, 5) - MA(C, 20);
        """
    ).compute(df)
    assert res.signals == ["布尔输出"]
    assert res.values == ["数值输出"]


def test_last_row_for_screening():
    df = _df(30)
    res = compile_formula("买入: CROSS(MA(C, 3), MA(C, 10)); 值: MA(C, 5);").compute(df)
    last = res.last_row()
    assert set(last) == {"买入", "值"}
    assert last["买入"] in (0.0, 1.0)


def test_chinese_identifier_and_comment():
    df = _df(30)
    formula = compile_formula(
        """
        {这是注释：N 周期}
        周期 := 5;
        均线: MA(C, 周期);
        """
    )
    res = formula.compute(df)
    assert res.columns["均线"][0] != res.columns["均线"][-1]


def test_compiled_formula_reusable_across_frames():
    f = compile_formula("值: MA(C, 5);")
    r1 = f.compute(_df(30, seed=1))
    r2 = f.compute(_df(40, seed=2))
    assert len(r1.columns["值"]) == 30
    assert len(r2.columns["值"]) == 40


def test_compiled_formula_is_dataclass_safe():
    """CompiledFormula 可 pickle（进程池/后台任务传输）。"""
    import pickle

    f = compile_formula("值: MA(C, 5);")
    f2 = pickle.loads(pickle.dumps(f))
    assert np.allclose(
        f.compute(_df(20)).columns["值"], f2.compute(_df(20)).columns["值"], equal_nan=True
    )


# ── 回归：归类收严 / FILTER 副作用 / 递归上限 / REF 负移位（审查修复） ────────


def test_ratio_output_classified_as_value_not_signal():
    """0~1 区间的价格比率是数值列，不是信号列（旧码按 [0,1] 值域兜底误判）。"""
    res = compile_formula("比率: C / HHV(C, 20);").compute(_df(40))
    assert res.signals == []
    assert res.values == ["比率"]


def test_normalized_oscillator_classified_as_value():
    """归一化振荡器（RSI/100）是数值列，不是信号列。"""
    res = compile_formula("强度: RSI(C, 14) / 100;").compute(_df(40))
    assert res.signals == []
    assert res.values == ["强度"]


def test_binary_zero_one_values_still_signal():
    """真正的 0/1 两值输出仍兜底归信号列。"""
    res = compile_formula("X: IF(C > MA(C, 5), 1, 0);").compute(_df(40))
    assert res.signals == ["X"]


def test_filter_does_not_pollute_series():
    """FILTER(C, N) 不改写输入序列：同公式后续 MA(C, 2) 与未过滤一致。"""
    df = _df(30)
    res = compile_formula("A: FILTER(C, 2); B: MA(C, 2);").compute(df)
    close = pd.to_numeric(df["close"]).to_numpy(dtype=float)
    expected = pd.Series(close).rolling(2).mean().to_numpy()
    np.testing.assert_allclose(res.columns["B"], expected, equal_nan=True)


def test_deep_paren_nesting_formula_error():
    """超深括号嵌套抛 FormulaError（嵌套过深），而非 RecursionError 逃逸。"""
    text = "X: " + "(" * 5000 + "C" + ")" * 5000 + ";"
    with pytest.raises(FormulaError, match="嵌套过深"):
        compile_formula(text)


def test_deep_unary_chain_formula_error():
    """超长一元运算符链同样受深度上限保护。"""
    with pytest.raises(FormulaError, match="嵌套过深"):
        compile_formula("X: " + "!" * 5000 + "C;")


def test_moderate_nesting_still_compiles():
    """常规嵌套深度不受上限影响。"""
    res = compile_formula("X: -(-(-(C + 1) * 2) + 3);").compute(_df(10))
    assert res.columns["X"].shape == (10,)


def test_ref_negative_shift_banned():
    """REF 负移位（未来函数）显式 FormulaError，不再依赖 float 类型巧合。"""
    with pytest.raises(FormulaError, match="负移位"):
        compile_formula("X: REF(C, -1);").compute(_df(30))


def test_ref_negative_via_expression_banned():
    """负移位经表达式算出（如 0-1）同样被禁。"""
    with pytest.raises(FormulaError, match="负移位"):
        compile_formula("X: REF(C, 0 - 1);").compute(_df(30))


def test_ref_positive_still_works():
    res = compile_formula("X: REF(C, 1);").compute(_df(30))
    assert np.isnan(res.columns["X"][0])
    assert res.columns["X"][1] == pytest.approx(float(pd.to_numeric(_df(30)["close"]).iloc[0]))


def test_mytt_internal_negative_ref_unaffected():
    """MyTT 库内直调（ICHIMOKU 迟行带）不经公式白名单，负移位仍可用。"""
    from easy_tdx.MyTT import REF

    close = np.arange(5, dtype=float)
    out = REF(close, -1)
    assert out[0] == pytest.approx(1.0)
