# 回测完整示例集

本文件汇集回测引擎的完整可运行示例与注意事项，配合 [backtest_usage.md](./backtest_usage.md)（手册）与 [cli-backtest.md](./cli-backtest.md)（CLI 参考）使用。

## 完整示例

### 示例 1：双均线交叉策略

```python
"""双均线交叉策略：MA5 上穿 MA20 买入，下穿卖出。"""
import pandas as pd
from easy_tdx.backtest import BacktestEngine, Strategy, crossover
from easy_tdx import MyTT


class DualMACross(Strategy):
    def init(self):
        self.ma5 = self.I(MyTT.MA, self.data.close, 5)
        self.ma20 = self.I(MyTT.MA, self.data.close, 20)
        self.golden = crossover(self.ma5, self.ma20)
        self.death = crossover(self.ma20, self.ma5)

    def next(self):
        if self.golden[self._bar_index] and self.position["size"] == 0:
            self.buy(size=0)
        elif self.death[self._bar_index] and self.position["size"] > 0:
            self.sell(size=0)


# 构造模拟数据（实际使用 TdxClient 获取）
dates = pd.date_range("2024-01-01", periods=200, freq="D")
import numpy as np
rng = np.random.default_rng(42)
close = 10.0 + np.cumsum(rng.normal(0, 0.2, 200))

df = pd.DataFrame({
    "datetime": dates,
    "open": close + rng.uniform(-0.1, 0.1, 200),
    "close": close,
    "high": close + rng.uniform(0, 0.3, 200),
    "low": close - rng.uniform(0, 0.3, 200),
    "vol": rng.integers(10000, 100000, 200),
})

engine = BacktestEngine(DualMACross, cash=100000, commission=0.0003)
result = engine.run(df)

result.summary()
print(f"\n年化收益: {result.performance['annual_return']:.2%}")
print(f"夏普比率: {result.performance['sharpe']:.2f}")
```

### 示例 2：MACD 策略 + 预计算指标

```python
"""MACD 策略：DIF 上穿 DEA 买入，下穿卖出。"""
from easy_tdx.backtest import BacktestEngine, Strategy, crossover
from easy_tdx import MyTT


class MACDStrategy(Strategy):
    def init(self):
        dif, dea, macd_hist = self.I(MyTT.MACD, self.data.close)
        self.dif = dif
        self.dea = dea
        self.golden = crossover(dif, dea)
        self.death = crossover(dea, dif)

    def next(self):
        if self.golden[self._bar_index] and self.position["size"] == 0:
            self.buy(size=0)
        elif self.death[self._bar_index] and self.position["size"] > 0:
            self.sell(size=0)


engine = BacktestEngine(MACDStrategy, cash=100000)
result = engine.run(df)  # df 包含 OHLCV 数据
```

### 示例 3：布林带突破 + 滑点模拟

```python
"""布林带策略：跌破下轨买入，突破上轨卖出，模拟滑点。"""
from easy_tdx.backtest import BacktestEngine, Strategy
from easy_tdx import MyTT


class BollingerBreakout(Strategy):
    def init(self):
        upper, mid, lower = self.I(MyTT.BOLL, self.data.close, 20)
        self.upper = upper
        self.lower = lower

    def next(self):
        cur = self.data.close[0]
        if cur <= self.lower[self._bar_index] and self.position["size"] == 0:
            self.buy(size=0)
        elif cur >= self.upper[self._bar_index] and self.position["size"] > 0:
            self.sell(size=0)


# 模拟滑点和保守成交价
engine = BacktestEngine(
    BollingerBreakout,
    cash=100000,
    slippage=0.02,          # 每股 2 分钱滑点
    execution="worst",      # 保守成交价
    reject_policy="skip",   # 资金不足直接跳过
)
result = engine.run(df)
```

### 示例 4：从文件运行 CLI

```python
# save as rsi_strategy.py
from easy_tdx.backtest import Strategy
from easy_tdx import MyTT


class RSIStrategy(Strategy):
    """RSI 超卖超买策略。"""
    def init(self):
        self.rsi = self.I(MyTT.RSI, self.data.close, 14)

    def next(self):
        cur_rsi = self.rsi[self._bar_index]
        if cur_rsi < 30 and self.position["size"] == 0:
            self.buy(size=0)
        elif cur_rsi > 70 and self.position["size"] > 0:
            self.sell(size=0)
```

```bash
easy-tdx backtest SZ 000001 \
    --strategy-file rsi_strategy.py \
    --cash 200000 \
    --execution next_open \
    --count 1000 \
    --adjust QFQ \
    --table
```

---

## 注意事项

1. **DataFrame 格式要求**：必须包含 `datetime`, `open`, `close`, `high`, `low` 列。`vol`/`amount` 为可选但推荐。
2. **成交时机**：默认 `next_open` 模式下，信号产生后需等待下一根 K 线才能成交。如果信号在最后一根 K 线产生，则无法成交。
3. **整手交易**：A 股按 100 股整手交易。全仓模式会自动向下取整到 100 的倍数。
4. **做空限制**：v1 不支持做空，卖出数量不能超过当前持仓。
5. **未来函数警告**：使用 `this_close` 模式时，结果中的 `config.future_leak_warning` 会标记为 `True`。
6. **多笔同 bar 交易**：引擎支持同一根 K 线上产生多笔交易（如分批建仓），按顺序依次撮合。
