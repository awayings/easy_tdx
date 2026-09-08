# 量化因子与组合管理 — 使用指南

> 本文档覆盖 easy-tdx v1.11.1 新增的量化计算能力：因子研究、因子分析、组合管理、高级回测（滑点建模/执行仿真/归因分析）。

---

## 目录

- [1. 因子引擎](#1-因子引擎)
  - [1.1 内置因子一览](#11-内置因子一览)
  - [1.2 单股多因子计算](#12-单股多因子计算)
  - [1.3 截面因子计算](#13-截面因子计算)
  - [1.4 远期收益计算](#14-远期收益计算)
  - [1.5 自定义因子](#15-自定义因子)
- [2. 因子预处理](#2-因子预处理)
- [3. 因子分析](#3-因子分析)
- [4. 组合管理](#4-组合管理)
  - [4.1 权重优化器](#41-权重优化器)
  - [4.2 风险模型](#42-风险模型)
  - [4.3 再平衡引擎](#43-再平衡引擎)
- [高级回测（滑点/执行仿真/归因）、CLI 与完整工作流](#高级回测滑点执行仿真归因cli-与完整工作流) → 见 [quantitative-advanced.md](./quantitative-advanced.md)

---

## 1. 因子引擎

因子引擎（`FactorEngine`）支持单股和多股截面两种计算模式，内置 19 个因子。

### 1.1 内置因子一览

| 类别 | 因子名 | 说明 |
|------|--------|------|
| **动量** | `momentum_20d` | 20 日收益率 |
| | `momentum_60d` | 60 日收益率 |
| | `reversal_5d` | 5 日反转（负收益） |
| **波动率** | `volatility_20d` | 20 日年化波动率 |
| | `atr_14d` | 14 日平均真实波幅 |
| | `turnover_rate` | 换手率（需 vol 列） |
| **质量** | `sharpe_20d` | 20 日夏普比率 |
| | `max_drawdown_20d` | 20 日最大回撤 |
| | `win_rate_20d` | 20 日上涨天数占比 |
| **成交量** | `obv_trend` | OBV 趋势斜率 |
| | `vol_surge` | 成交量突增倍数 |
| | `amount_ma_ratio` | 成交额 / MA5 比值 |
| **技术** | `macd_hist_signal` | MACD 柱状信号 |
| | `rsi_14` | 14 日 RSI |
| | `boll_position` | 布林带位置（0~1） |
| **缠论** | `chanlun_bi_dir` | 当前笔方向（+1/-1） |
| | `chanlun_mmd` | 最近买卖点（+2/+1/-1/-2） |
| **价值** | `pe_ratio` | 市盈率（占位，返回 NaN） |
| | `pb_ratio` | 市净率（占位，返回 NaN） |

### 1.2 单股多因子计算

```python
from easy_tdx import TdxClient
from easy_tdx.factor import FactorEngine

client = TdxClient()
df = client.get_security_bars(Market.SH, "600519", KlineCategory.DAY, 0, 300)

engine = FactorEngine()

# 计算多个因子
result = engine.compute_single(df, ["momentum_20d", "volatility_20d", "rsi_14"])
print(result.tail())

# 计算所有内置因子
result = engine.compute_single(df)  # 不传因子名 = 全部
print(result.columns.tolist())
```

输出 DataFrame 在原始列基础上追加因子列（以因子名命名，前缀 `NaN` 行因窗口不足为 `NaN`）。

### 1.3 截面因子计算

```python
from easy_tdx import TdxClient
from easy_tdx.factor import FactorEngine

client = TdxClient()

# 准备多只股票数据
stock_pool = ["000001", "000858", "600519", "600036", "601318"]
data = {}
for code in stock_pool:
    market = Market.SH if code.startswith("6") else Market.SZ
    data[code] = client.get_security_bars(market, code, KlineCategory.DAY, 0, 300)

engine = FactorEngine()

# 截面计算：返回 long format（date, code, factor_name...）
factor_data = engine.compute_cross_section(
    data,
    ["momentum_20d", "volatility_20d", "rsi_14"],
)
print(factor_data.head(10))

# 指定日期：只计算某一天的截面
factor_data = engine.compute_cross_section(
    data, ["momentum_20d"], date=20240601,
)
```

### 1.4 远期收益计算

```python
# 计算未来 5 日收益率（用于因子分析）
forward_returns = engine.compute_forward_returns(data, period=5)
print(forward_returns.head())
```

### 1.5 自定义因子

继承 `Factor` 基类，用 `@register_factor` 注册即可自动发现：

```python
from easy_tdx.factor import Factor, register_factor

@register_factor
class MyMomentum(Factor):
    name = "my_momentum"
    description = "自定义动量因子"
    window = 20

    def compute(self, df):
        return df["close"].pct_change(self.window)
```

注册后直接用名字引用：

```python
result = engine.compute_single(df, ["my_momentum"])
```

---

## 2. 因子预处理

6 个纯函数，组合成管道：

```python
from easy_tdx.factor import preprocess

# 单因子预处理管道
clean = preprocess(
    factor_data,
    factor_names=["momentum_20d"],
    steps=["winsorize", "zscore", "fill_missing"],
)
```

| 函数 | 说明 |
|------|------|
| `winsorize(df, factor_names, n_sigma=3)` | MAD 去极值 |
| `zscore(df, factor_names)` | 截面标准化 |
| `rank_normalize(df, factor_names)` | 排名归一化 |
| `fill_missing(df, factor_names)` | 填充缺失值 |
| `orthogonalize(df, factor_names, by="market_cap")` | 正交化（去除市值暴露） |
| `preprocess(df, factor_names, steps)` | 组合管道 |

所有函数自动检测截面数据（有 `date` 列时按日期分组处理）。

---

## 3. 因子分析

```python
from easy_tdx.factor import FactorEngine, FactorAnalyzer, preprocess

# 1. 计算截面因子
factor_data = engine.compute_cross_section(data, ["momentum_20d", "rsi_14"])

# 2. 预处理
clean = preprocess(factor_data, ["momentum_20d", "rsi_14"])

# 3. 计算远期收益
forward_returns = engine.compute_forward_returns(data, period=5)

# 4. 分析
analyzer = FactorAnalyzer(clean, forward_returns)

# IC 分析（Spearman 秩相关）
ic_series = analyzer.compute_ic("momentum_20d")
print(f"均值 IC: {ic_series.mean():.4f}, ICIR: {ic_series.mean()/ic_series.std():.4f}")

# 分层收益（5 组）
quantile_returns = analyzer.compute_quantile_returns("momentum_20d", n_groups=5)
print(quantile_returns.head())

# 因子衰减（IC 自相关）
decay = analyzer.compute_decay("momentum_20d", max_lag=10)
print(decay)

# 完整报告
report = analyzer.full_report("momentum_20d")
print(f"IC均值={report.mean_ic:.4f} ICIR={report.icir:.4f}")
print(f"多头年化={report.long_only_annual:.2%} 空头年化={report.short_only_annual:.2%}")
print(f"多空夏普={report.long_short_sharpe:.4f} 换手率={report.turnover:.4f}")
```

---

## 4. 组合管理

### 4.1 权重优化器

4 种内置优化器：

```python
from easy_tdx.portfolio import (
    EqualWeightOptimizer,
    FactorWeightedOptimizer,
    RiskParityOptimizer,
    MeanVarianceOptimizer,
)
import pandas as pd

# 因子分数表（来自 FactorEngine）
scores_df = pd.DataFrame({
    "code": ["000001", "600519", "601318", "000858", "600036"],
    "score": [0.8, 0.6, 0.5, 0.3, 0.1],
})

# 1. 等权：选前 N 只，等权分配
opt1 = EqualWeightOptimizer()
weights1 = opt1.optimize(scores_df, n_stocks=3)
# {'000001': 0.333, '600519': 0.333, '601318': 0.333}

# 2. 因子加权：分数越高权重越大
opt2 = FactorWeightedOptimizer()
weights2 = opt2.optimize(scores_df, n_stocks=3)
# {'000001': 0.42, '600519': 0.32, '601318': 0.26}

# 3. 风险平价：按波动率倒数加权
returns_df = pd.DataFrame(...)  # 收益率矩阵
opt3 = RiskParityOptimizer(returns_df)
weights3 = opt3.optimize(scores_df, n_stocks=3)

# 4. 均值方差：scipy SLSQP 优化（无 scipy 退化为等权）
opt4 = MeanVarianceOptimizer(returns_df)
weights4 = opt4.optimize(scores_df, n_stocks=3)
```

### 4.2 风险模型

```python
from easy_tdx.portfolio import RiskModel
import pandas as pd

risk = RiskModel()

# 估计协方差矩阵（Ledoit-Wolf 收缩）
returns = pd.DataFrame(...)  # N 只股票 × T 天收益率
cov = risk.estimate_covariance(returns, method="shrinkage", window=60)

# 组合风险分解
weights = {"000001": 0.3, "600519": 0.4, "601318": 0.3}
metrics = risk.portfolio_risk(weights, cov)
print(f"年化波动率: {metrics['total_volatility']:.2%}")
print(f"最大风险贡献: {metrics['max_risk_contribution']:.2%}")
print(f"持仓数: {metrics['n_positions']}")
```

### 4.3 再平衡引擎

```python
from easy_tdx.portfolio import RebalanceEngine, FactorWeightedOptimizer
from easy_tdx import TdxClient

client = TdxClient()
stock_pool = ["000001", "000858", "600519", "600036", "601318"]
data = {c: client.get_security_bars(..., c, ...) for c in stock_pool}

# 创建引擎
engine = RebalanceEngine(
    optimizer=FactorWeightedOptimizer(),
    factor_name="momentum_20d",   # 用哪个因子选股
    n_stocks=3,                   # 持仓数量
    rebalance_freq="M",           # 调仓频率: W/M/Q
    commission=0.0003,            # 佣金率
    slippage=0.001,               # 滑点率
    cash=1_000_000,               # 初始资金
)

# 运行回测
result = engine.run(data, start_date=20230101, end_date=20240101)

# 结果
print(f"总收益: {result.performance['total_return']:.2%}")
print(f"年化: {result.performance['annual_return']:.2%}")
print(f"最大回撤: {result.performance['max_drawdown']:.2%}")
print(f"夏普: {result.performance['sharpe']:.4f}")
print(f"调仓次数: {len(result.rebalance_dates)}")
print(f"交易笔数: {len(result.trades)}")

# 权益曲线
print(result.equity_curve.head())

# 持仓历史
for state in result.states[-5:]:
    print(f"  {state.date}: 持仓{state.positions_count}只 净值{state.total_value:.0f}")
```

---


## 向后兼容

所有新功能通过可选参数启用，**现有代码零改动**：

| 现有调用 | 行为 |
|---------|------|
| `BacktestEngine(strategy, slippage=0.01)` | 与旧版完全一致 |
| `BacktestEngine(strategy)` | 无滑点，与旧版一致 |
| `OrderSimulator(df, slippage=0.01)` | 与旧版完全一致 |
| `BacktestEngine(strategy, slippage_model=...)` | 使用新滑点模型 |
| `BacktestEngine(strategy, execution_model=...)` | 使用新执行引擎 |

新增模块（`factor/`, `portfolio/`, `backtest/slippage.py`, `backtest/execution.py`, `backtest/attribution.py`）为独立新增，不修改任何现有接口。
