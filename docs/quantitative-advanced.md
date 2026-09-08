# 量化进阶：执行仿真与归因

滑点建模、执行仿真（TWAP/VWAP/限价单）、归因分析与完整工作流。因子/组合基础见 [quantitative-guide.md](./quantitative-guide.md)。

## 1. 高级回测

### 1.1 滑点模型

4 种可插拔滑点模型，替代原有固定滑点：

```python
from easy_tdx.backtest import BacktestEngine
from easy_tdx.backtest.slippage import (
    FixedSlippage,
    PercentSlippage,
    SquareRootSlippage,
    VolumeSlippage,
)

# 1. 固定每股滑点（与旧行为一致）
model1 = FixedSlippage(per_share=0.01)

# 2. 按金额百分比
model2 = PercentSlippage(rate=0.001)

# 3. 方根市场冲击模型（Almgren-Chriss 简化版）
#    impact = sigma * sqrt(participation_rate) * price * size * coeff
#    A 股量化主流：参与率 >5% 时冲击显著
model3 = SquareRootSlippage(impact_coeff=0.1)

# 4. 成交量比例滑点
model4 = VolumeSlippage(base_bps=10.0)

# 在 BacktestEngine 中使用
engine = BacktestEngine(
    MyStrategy,
    cash=1_000_000,
    slippage_model=SquareRootSlippage(impact_coeff=0.1),
)
result = engine.run(df)
```

**模型选择建议**：

| 场景 | 推荐模型 | 参数 |
|------|---------|------|
| 快速原型 | `FixedSlippage` | `per_share=0.01` |
| 中频策略 | `PercentSlippage` | `rate=0.001` |
| 大额订单 | `SquareRootSlippage` | `impact_coeff=0.1` |
| 低流动性股票 | `VolumeSlippage` | `base_bps=10.0` |

### 1.2 执行仿真

4 种执行模型，将单笔信号拆分为多笔子交易：

```python
from easy_tdx.backtest.execution import (
    ImmediateExecution,
    TWAPExecution,
    VWAPExecution,
    LimitExecution,
)

# 1. 即时成交（默认，与旧行为一致）
exec1 = ImmediateExecution()

# 2. TWAP：时间加权平均价格，N 根 K 线均匀拆单
exec2 = TWAPExecution(n_bars=5)

# 3. VWAP：成交量加权平均价格，按历史量分布拆单
exec3 = VWAPExecution(n_bars=5, volume_lookback=20)

# 4. 限价单：目标价挂单，TTL 内未触发则放弃
exec4 = LimitExecution(ttl_bars=5)

# 在 BacktestEngine 中使用
engine = BacktestEngine(
    MyStrategy,
    cash=1_000_000,
    execution_model=TWAPExecution(n_bars=3),
    slippage_model=SquareRootSlippage(),
)
result = engine.run(df)
```

**执行模型选择**：

| 场景 | 推荐模型 | 参数 |
|------|---------|------|
| 小额/快速验证 | `ImmediateExecution` | 默认 |
| 大额建仓/平仓 | `TWAPExecution` | `n_bars=3~5` |
| 追踪 VWAP 基准 | `VWAPExecution` | `n_bars=5` |
| 精确入场价位 | `LimitExecution` | `ttl_bars=5` |

**TWAP vs VWAP 示例**：

```python
# TWAP: 300 股拆成 3 笔 100 股，在 bar 1/2/3 以 close 执行
engine = BacktestEngine(
    MyStrategy, cash=100_000,
    execution_model=TWAPExecution(n_bars=3),
)

# VWAP: 按成交量分布拆 300 股 — 成交量大的 bar 分配更多
engine = BacktestEngine(
    MyStrategy, cash=100_000,
    execution_model=VWAPExecution(n_bars=3, volume_lookback=20),
)

# 限价单：在 50 元挂买入，5 根 K 线内 low <= 50 才成交
class LimitBuyStrategy(Strategy):
    def init(self): pass
    def next(self):
        if self._bar_index == 0:
            self.buy(size=100, price=50.0)  # 指定限价

engine = BacktestEngine(
    LimitBuyStrategy, cash=100_000,
    execution_model=LimitExecution(ttl_bars=5),
)
```

### 1.3 归因分析

从回测结果生成归因报告：

```python
from easy_tdx.backtest import BacktestEngine
from easy_tdx.backtest.attribution import AttributionAnalyzer

# 运行回测
engine = BacktestEngine(MyStrategy, cash=1_000_000)
result = engine.run(df)

# --- 成本归因 ---
analyzer = AttributionAnalyzer(result.trades, result.equity_curve)
cost_report = analyzer.cost_attribution()
print(f"总收益: {cost_report.total_return:.2%}")
print(f"总交易成本: {cost_report.total_trade_cost:.0f} 元")
print(f"  佣金: {cost_report.commission_cost:.0f}")
print(f"  滑点: {cost_report.slippage_cost:.0f}")
print(f"  印花税: {cost_report.stamp_tax_cost:.0f}")

# --- Brinson 归因（需要基准）---
import numpy as np
import pandas as pd
# 构造基准曲线（如沪深300）
benchmark = pd.DataFrame({
    "datetime": result.equity_curve["datetime"],
    "total": np.linspace(100000, 108000, len(result.equity_curve)),
})
analyzer = AttributionAnalyzer(result.trades, result.equity_curve, benchmark=benchmark)
brinson_report = analyzer.brinson_attribution()
print(f"配置贡献: {brinson_report.allocation_return:.2%}")
print(f"选股贡献: {brinson_report.selection_return:.2%}")
print(f"交叉效应: {brinson_report.interaction_return:.2%}")

# --- 因子归因（需要因子数据）---
exposures = pd.DataFrame({"momentum": [0.5, 0.3, 0.2], "quality": [0.1, -0.1, 0.0]})
returns = pd.DataFrame({"momentum": [0.05, 0.03, 0.02], "quality": [0.01, -0.02, 0.0]})
analyzer = AttributionAnalyzer(
    result.trades, result.equity_curve,
    factor_exposures=exposures, factor_returns=returns,
)
factor_report = analyzer.factor_attribution()
for name, ret in factor_report.factor_returns.items():
    print(f"  {name}: {ret:.4f}")
print(f"特质收益: {factor_report.specific_return:.4f}")

# --- 完整报告（自动选择最佳归因模式）---
full_report = analyzer.full_report()
```

**归因模式优先级**：因子归因 > Brinson 归因 > 成本归因。`full_report()` 自动选择数据最完整的模式。

---

## 2. CLI 命令

```bash
# 列出所有内置因子
easy-tdx factor list --table

# 因子分析（需要数据，输出示例代码）
easy-tdx factor analyze momentum_20d

# 组合因子回测（需要数据，输出示例代码）
easy-tdx pfactor backtest momentum_20d --n-stocks 10 --optimizer factor_weighted
```

CLI 命令输出 Python API 示例代码，方便复制使用。完整的因子计算和组合回测建议通过 Python API 完成。

---

## 3. 完整工作流示例

从数据获取到组合回测再到归因分析的完整管道：

```python
"""
easy-tdx 量化研究完整工作流示例。

依赖: pip install easy-tdx
"""

from easy_tdx import TdxClient, Market, KlineCategory
from easy_tdx.factor import FactorEngine, FactorAnalyzer, preprocess
from easy_tdx.portfolio import RebalanceEngine, FactorWeightedOptimizer
from easy_tdx.backtest import BacktestEngine
from easy_tdx.backtest.slippage import SquareRootSlippage
from easy_tdx.backtest.execution import TWAPExecution
from easy_tdx.backtest.attribution import AttributionAnalyzer

# ── 1. 数据获取 ──────────────────────────────────────
client = TdxClient()
stock_pool = ["000001", "000858", "600519", "600036", "601318",
              "000333", "002415", "601012", "600276", "000568"]

data = {}
for code in stock_pool:
    market = Market.SH if code.startswith("6") else Market.SZ
    data[code] = client.get_security_bars(
        market, code, KlineCategory.DAY, 0, 500
    )
print(f"获取 {len(data)} 只股票数据")

# ── 2. 因子计算 ──────────────────────────────────────
engine = FactorEngine()
factor_data = engine.compute_cross_section(
    data, ["momentum_20d", "volatility_20d", "rsi_14"]
)
print(f"截面因子数据: {len(factor_data)} 行")

# ── 3. 因子预处理 ─────────────────────────────────────
clean = preprocess(
    factor_data,
    factor_names=["momentum_20d", "volatility_20d", "rsi_14"],
    steps=["winsorize", "zscore", "fill_missing"],
)

# ── 4. 因子分析 ──────────────────────────────────────
forward_returns = engine.compute_forward_returns(data, period=5)

for factor_name in ["momentum_20d", "volatility_20d", "rsi_14"]:
    analyzer = FactorAnalyzer(clean, forward_returns)
    report = analyzer.full_report(factor_name)
    print(f"\n── {factor_name} ──")
    print(f"  IC均值: {report.mean_ic:.4f}  ICIR: {report.icir:.4f}")
    print(f"  多头年化: {report.long_only_annual:.2%}")
    print(f"  多空夏普: {report.long_short_sharpe:.4f}")

# ── 5. 组合回测 ──────────────────────────────────────
rebalancer = RebalanceEngine(
    optimizer=FactorWeightedOptimizer(),
    factor_name="momentum_20d",
    n_stocks=5,
    rebalance_freq="M",
    cash=1_000_000,
)
result = rebalancer.run(data, start_date=20230101, end_date=20240101)
print(f"\n── 组合回测 ──")
print(f"  总收益: {result.performance['total_return']:.2%}")
print(f"  年化: {result.performance['annual_return']:.2%}")
print(f"  最大回撤: {result.performance['max_drawdown']:.2%}")
print(f"  夏普: {result.performance['sharpe']:.4f}")

# ── 6. 高级单策略回测（滑点 + 执行仿真）──────
from easy_tdx.backtest import Strategy

class MomentumStrategy(Strategy):
    def init(self):
        pass
    def next(self):
        if self._bar_index < 20:
            return
        ret = (self.data.close[0] - self.data.close[-20]) / self.data.close[-20]
        if ret > 0.05 and self.position["size"] == 0:
            self.buy(size=0)
        elif ret < -0.03 and self.position["size"] > 0:
            self.sell(size=0)

bt_engine = BacktestEngine(
    MomentumStrategy,
    cash=500_000,
    slippage_model=SquareRootSlippage(impact_coeff=0.1),
    execution_model=TWAPExecution(n_bars=3),
)
# 选一只股票做回测
bt_result = bt_engine.run(data["600519"])
print(f"\n── 高级回测（600519）──")
print(f"  总收益: {bt_result.performance['total_return']:.2%}")
print(f"  夏普: {bt_result.performance['sharpe']:.4f}")

# ── 7. 归因分析 ──────────────────────────────────────
att_analyzer = AttributionAnalyzer(bt_result.trades, bt_result.equity_curve)
cost_report = att_analyzer.cost_attribution()
print(f"\n── 成本归因 ──")
print(f"  总交易成本: {cost_report.total_trade_cost:.0f} 元")
print(f"    佣金: {cost_report.commission_cost:.0f}")
print(f"    滑点: {cost_report.slippage_cost:.0f}")
print(f"    印花税: {cost_report.stamp_tax_cost:.0f}")

print("\n完成。")
client.close()
```

---

