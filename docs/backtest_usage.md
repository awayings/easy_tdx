# easy-tdx 回测引擎使用手册

`easy_tdx.backtest` 是一个纯计算层的向量化策略回测引擎，零网络依赖，可完全离线运行。

## 目录

- [快速开始](#快速开始)
- [编写策略](#编写策略)
  - [策略生命周期](#策略生命周期)
  - [访问行情数据](#访问行情数据)
  - [注册技术指标](#注册技术指标)
  - [金叉检测](#金叉检测)
  - [生成交易信号](#生成交易信号)
- [引擎配置](#引擎配置)
  - [成交价规则](#成交价规则)
  - [仓位模式](#仓位模式)
  - [费用模型](#费用模型)
  - [订单拒绝策略](#订单拒绝策略)
- [获取回测结果](#获取回测结果)
  - [绩效指标一览](#绩效指标一览)
  - [资金曲线](#资金曲线)
  - [交易记录](#交易记录)
  - [序列化输出](#序列化输出)
- [CLI 命令行](#cli-命令行) → 见 [cli-backtest.md](./cli-backtest.md)
- [进阶用法](#进阶用法)
  - [预计算指标列](#预计算指标列)
  - [缠论结果注入](#缠论结果注入)
  - [自定义策略文件](#自定义策略文件)
- [完整示例](#完整示例) → 见 [backtest-examples.md](./backtest-examples.md)

---

## 快速开始

```python
import pandas as pd
from easy_tdx.backtest import BacktestEngine, Strategy, crossover
from easy_tdx import MyTT


# 1. 定义策略
class DualMAStrategy(Strategy):
    def init(self):
        self.ma5 = self.I(MyTT.MA, self.data.close, 5)
        self.ma20 = self.I(MyTT.MA, self.data.close, 20)
        self.cross = crossover(self.ma5, self.ma20)

    def next(self):
        if self.cross[self._bar_index]:
            self.buy(size=0)       # 全仓买入
        elif self.position["size"] > 0:
            self.sell(size=0)      # 全部卖出


# 2. 准备数据（DataFrame 必须包含 datetime, open, close, high, low 列）
#    通过 TdxClient 获取真实数据：
#    from easy_tdx import TdxClient
#    client = TdxClient()
#    df = client.get_stock_kline("SZ", "000001", period="DAILY", count=500)

# 3. 运行回测
engine = BacktestEngine(DualMAStrategy, cash=100000)
result = engine.run(df)

# 4. 查看结果
print(f"总收益率: {result.performance['total_return']:.2%}")
print(f"夏普比率: {result.performance['sharpe']:.2f}")
print(f"最大回撤: {result.performance['max_drawdown']:.2%}")
print(f"交易次数: {result.performance['total_trades']}")
```

---

## 编写策略

### 策略生命周期

继承 `Strategy` 基类，实现两个方法：

```python
class MyStrategy(Strategy):
    def init(self):
        """回测开始前调用一次。注册指标、初始化内部状态。"""
        pass

    def next(self):
        """每根 K 线调用一次。根据当前行情生成交易信号。"""
        pass
```

引擎内部执行顺序：

```
_bind_data(df) → _call_init() → 逐 bar 调用 _set_bar_index(i) + _call_next()
```

### 访问行情数据

通过 `self.data` 代理访问 K 线数据，支持相对索引：

```python
def next(self):
    # 当前 bar（索引 0）
    price = self.data.close[0]

    # 前一根 bar（索引 -1）
    prev_price = self.data.close[-1]

    # 前两根 bar（索引 -2）
    prev2 = self.data.close[-2]
```

> **提示**：回溯索引（如 `[-1]`、`[-2]`）在回测首根 bar 数据不足时返回
> `NaN` 而非报错。如需完全跳过指标预热期，设置 `warmup_bars`（见下文）。

**标准列**：`open`, `close`, `high`, `low`, `vol`, `amount`

```python
self.data.open[0]     # 开盘价
self.data.close[0]    # 收盘价
self.data.high[0]     # 最高价
self.data.low[0]      # 最低价
self.data.vol[0]      # 成交量
self.data.amount[0]   # 成交额
```

**自定义列**：如果 DataFrame 包含额外列（如 `MACD_DIF`），通过属性名直接访问：

```python
self.data.MACD_DIF[0]     # 自动通过 __getattr__ 查找
```

**获取完整数组**：`.raw` 属性返回 numpy 数组，可传入指标函数：

```python
close_array = self.data.close.raw    # numpy ndarray
```

### 注册技术指标

使用 `self.I()` 在 `init()` 中注册指标。`_SeriesAccessor` 参数会自动解包为 numpy 数组：

```python
from easy_tdx import MyTT

def init(self):
    # 均线
    self.ma5 = self.I(MyTT.MA, self.data.close, 5)
    self.ma20 = self.I(MyTT.MA, self.data.close, 20)

    # MACD
    self.dif, self.dea, self.macd = self.I(MyTT.MACD, self.data.close)

    # 布林带
    self.upper, self.mid, self.lower = self.I(MyTT.BOLL, self.data.close, 20)

def next(self):
    # 用索引访问指标值
    if self.ma5[self._bar_index] > self.ma20[self._bar_index]:
        self.buy(size=0)
```

### 金叉检测

`crossover(a, b)` 检测序列 a 从下方穿越 b（金叉）：

```python
from easy_tdx.backtest import crossover

def init(self):
    self.ma5 = self.I(MyTT.MA, self.data.close, 5)
    self.ma20 = self.I(MyTT.MA, self.data.close, 20)
    self.golden = crossover(self.ma5, self.ma20)   # 金叉：ma5 上穿 ma20
    self.death = crossover(self.ma20, self.ma5)    # 死叉：ma20 上穿 ma5

def next(self):
    if self.golden[self._bar_index]:
        self.buy(size=0)
    if self.death[self._bar_index]:
        self.sell(size=0)
```

### 生成交易信号

在 `next()` 中调用 `self.buy()` 或 `self.sell()`：

```python
self.buy(size=100)                            # 买入 100 股
self.buy(size=0)                              # 全仓买入（引擎自动计算股数）
self.buy(size=100, price=10.5)                # 限价买入
self.buy(size=100, stop_loss=9.0, take_profit=12.0)  # 带止损止盈

self.sell(size=100)                           # 卖出 100 股
self.sell(size=0)                             # 全部卖出
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `size` | float | 0 | 交易数量，0 = 全仓/清仓 |
| `price` | float \| None | None | 限价，None = 市价单 |
| `stop_loss` | float \| None | None | 止损价。当根 bar 的 low 触及止损价时触发平仓信号 |
| `take_profit` | float \| None | None | 止盈价。当根 bar 的 high 触及止盈价时触发平仓信号 |

> **止损/止盈成交时点**：SL/TP 信号触发后，**延迟到下一根 bar 开盘成交**（与普通
> 策略信号一致），而非在信号当根以触发价成交。若下一根跳空，取对持仓者更不利的实际
> 开盘价（卖出取 `min(下一根开盘, 触发价)`）。这避免了"假设能在止损价精确成交"的
> 前视偏差，回测结果更贴近真实滑点与跳空场景。

**查看当前持仓**：

```python
def next(self):
    pos = self.position          # {"size": 100.0}
    if pos["size"] > 0:
        # 当前持有多头
        pass
```

---

## 引擎配置

```python
engine = BacktestEngine(
    strategy=MyStrategy,
    cash=100000.0,              # 初始资金
    commission=0.0003,          # 佣金率（万三）
    min_commission=5.0,         # 最低佣金（元）
    stamp_tax=0.001,            # 印花税率（千一，仅卖出）
    slippage=0.0,               # 滑点（每股）
    execution="next_open",      # 成交价规则
    position_mode="full",       # 仓位模式
    reject_policy="reduce",     # 拒绝策略
    warmup_bars=0,              # 指标预热 bar 数（前 N 根不调用 next()，不产生信号）
)
```

### 指标预热（warmup）

技术指标（如 MA20、MACD）在前若干根 bar 的值是 `NaN` 或不稳定的。为避免
预热期产生错误信号或访问越界：

- **回溯访问容错**：`self.data.close[-1]` / `[-2]` 等负向索引在首根 bar
  （数据不足）时返回 `NaN` 而非抛 `IndexError`，策略无需手动加 `bar_index > 0`
  守卫。
- **warmup_bars 参数**：设置后引擎在前 `warmup_bars` 根不调用 `next()`、不
  产生信号（资金曲线照常推进）。例如用 MA20 策略时可设 `warmup_bars=20`：

```python
engine = BacktestEngine(MyStrategy, cash=100000, warmup_bars=20)
```

### 成交价规则

| 模式 | 成交价 | 说明 |
|------|--------|------|
| `next_open` | 下一根 K 线开盘价 | **默认**，最贴近实盘 |
| `next_close` | 下一根 K 线收盘价 | 日内策略常用 |
| `this_close` | 当前 K 线收盘价 | ⚠️ 存在**未来函数**风险，引擎会标记 `future_leak_warning` |
| `worst` | 买入取高价 / 卖出取低价 | 保守估计滑点 |
| `best` | 买入取低价 / 卖出取高价 | 乐观估计 |

信号在 bar N 产生时，`next_*` 模式在 bar N+1 成交，`this_close` 在 bar N 成交。

### 仓位模式

| 模式 | `buy(size=X)` 行为 |
|------|---------------------|
| `full` | `size=0` 时全仓，按 100 股整手计算；`size>0` 时买入指定股数 |
| `fixed` | 严格按 `size` 买入指定股数 |
| `percent` | `size` 表示总资产的百分比（如 0.5 = 50%），按 100 股整手计算 |

### 费用模型

引擎模拟 A 股费用结构：

- **佣金**：`max(成交金额 × commission, min_commission)`，双向收取
- **印花税**：`成交金额 × stamp_tax`，仅卖出时收取
- **滑点**：`成交股数 × slippage`，双向收取

```python
# 免佣回测
engine = BacktestEngine(MyStrategy, commission=0.0, min_commission=0.0, stamp_tax=0.0)

# 模拟实际佣金
engine = BacktestEngine(MyStrategy, commission=0.00025, min_commission=5.0, stamp_tax=0.001)
```

### 订单拒绝策略

当资金不足（买入）或持仓不足（卖出）时：

| 策略 | 行为 |
|------|------|
| `reduce` | 减少到可执行的股数，生成实际成交 |
| `skip` | 拒绝整个订单，标记 `rejected=True` |

---

## 获取回测结果

```python
result = engine.run(df)
```

`result` 是 `BacktestResult` 对象，包含：

### 绩效指标一览

```python
perf = result.performance   # dict[str, float]
```

| 指标 | Key | 说明 |
|------|-----|------|
| 总收益率 | `total_return` | (期末权益 / 期初资金) - 1 |
| 年化收益率 | `annual_return` | 按年化复利计算 |
| 最大回撤 | `max_drawdown` | 峰值到谷底的最大跌幅比例 |
| 最大回撤持续 | `max_dd_duration` | 最大回撤持续的 bar 数 |
| 夏普比率 | `sharpe` | (日超额收益均值 / 日标准差) × √252 |
| 索提诺比率 | `sortino` | 分母只用负收益标准差 |
| 卡玛比率 | `calmar` | 年化收益 / 最大回撤 |
| 年化波动率 | `volatility` | 日收益率标准差 × √252 |
| 总交易次数 | `total_trades` | 卖出次数（完整闭环） |
| 盈利次数 | `win_trades` | PnL > 0 的卖出 |
| 亏损次数 | `lose_trades` | PnL ≤ 0 的卖出 |
| 被拒绝次数 | `rejected_trades` | 资金/持仓不足被拒绝的总次数 |
| 胜率 | `win_rate` | 盈利次数 / 总交易次数 |
| 盈亏比 | `profit_factor` | 总盈利 / |总亏损| |
| 平均盈利 | `avg_win` | 盈利交易的平均 PnL |
| 平均亏损 | `avg_loss` | 亏损交易的平均 PnL |
| 最大盈利 | `max_win` | 单笔最大盈利 |
| 最大亏损 | `max_loss` | 单笔最大亏损 |
| 平均持仓天数 | `avg_holding_days` | 固定值 5.0（待改进） |

### 资金曲线

```python
equity = result.equity_curve   # pd.DataFrame
# 列：datetime, cash, position_value, total, drawdown, drawdown_pct
```

| 列 | 说明 |
|----|------|
| `datetime` | 时间 |
| `cash` | 可用现金 |
| `position_value` | 持仓市值 |
| `total` | 总权益 = cash + position_value |
| `drawdown` | 回撤金额 = 峰值 - 当前总权益 |
| `drawdown_pct` | 回撤比例 |

### 交易记录

```python
trades = result.trades   # pd.DataFrame
# 列：datetime, direction, size, price, commission, pnl, rejected
```

### 持仓快照

```python
positions = result.positions   # pd.DataFrame
# 列：datetime, size, avg_price, market_value, unrealized_pnl
```

### 序列化输出

```python
# JSON 字符串
json_str = result.to_json()

# Python 字典（DataFrame 转为 records 列表）
data = result.to_dict()

# 打印概要到标准输出
result.summary()
```

### 配置快照

```python
config = result.config
# {"cash": 100000, "commission": 0.0003, "execution": "next_open",
#  "position_mode": "full", "reject_policy": "reduce",
#  "future_leak_warning": False}
```

---


## CLI 命令行

CLI 用法见 [cli-backtest.md](./cli-backtest.md)（回测/寻优/组合/run-all 命令与参数）。

## 进阶用法

### 预计算指标列

策略可使用 DataFrame 中预先计算的指标列。通过 `self.data.列名` 访问：

```python
from easy_tdx.backtest import BacktestEngine, Strategy
from easy_tdx.indicator import compute_indicators

class BollingerStrategy(Strategy):
    def init(self):
        # BOLL_UPPER 已在 DataFrame 中预计算
        self.upper = self.data.BOLL_UPPER
        self.lower = self.data.BOLL_LOWER

    def next(self):
        if self.data.close[0] < self.lower[0]:
            self.buy(size=0)     # 跌破下轨买入
        elif self.data.close[0] > self.upper[0]:
            self.sell(size=0)    # 突破上轨卖出

# 预计算指标
df = compute_indicators(df, ["BOLL"])
engine = BacktestEngine(BollingerStrategy)
result = engine.run(df)
```

### 缠论结果注入

v1 提供手动注入接口，策略通过 `self.chanlun` 访问：

```python
from easy_tdx.backtest import BacktestEngine, Strategy
from easy_tdx.chanlun import ChanlunAnalyser

class ChanlunStrategy(Strategy):
    def init(self):
        pass

    def next(self):
        cl = self.chanlun
        if cl is None:
            return
        # 使用缠论买卖点
        # mmd_list = cl.get("mmd", [])
        # ...

# 获取缠论结果
analyser = ChanlunAnalyser("SZ000001", "DAILY")
cl_result = analyser.process_klines(df)

# 注入引擎
engine = BacktestEngine(ChanlunStrategy)
result = engine.run(df, chanlun_result=cl_result.to_dict())
```

### 自定义策略文件

CLI 的 `--strategy-file` 加载 Python 文件，文件中必须包含一个 `Strategy` 子类：

```python
# my_strategy.py
from easy_tdx.backtest import Strategy, crossover
from easy_tdx import MyTT


class MyStrategy(Strategy):
    """双均线策略。"""
    def init(self):
        self.ma5 = self.I(MyTT.MA, self.data.close, 5)
        self.ma20 = self.I(MyTT.MA, self.data.close, 20)
        self.cross = crossover(self.ma5, self.ma20)

    def next(self):
        if self.cross[self._bar_index]:
            self.buy(size=0)
        elif self.position["size"] > 0:
            self.sell(size=0)
```

使用：

```bash
easy-tdx backtest SZ 000001 --strategy-file my_strategy.py --table
```

---

