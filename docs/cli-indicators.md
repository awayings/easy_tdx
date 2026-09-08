# CLI 参考 — 技术指标与公式

## 技术指标

```bash
easy-tdx indicator-list --table                       # 列出所有可用指标
easy-tdx indicator MACD -m SH -c 600519 --table       # MACD
easy-tdx indicator KDJ -m SZ -c 000001 --table        # KDJ
easy-tdx indicator RSI -m SH -c 600519 --table        # RSI
easy-tdx indicator BOLL -m SH -c 600519 --table       # BOLL 布林带
easy-tdx indicator DMI -m SH -c 600519 --table        # DMI 动向指标
easy-tdx indicator ATR -m SH -c 600519 --table        # ATR 真实波幅
easy-tdx indicator WR -m SH -c 600519 --table         # WR 威廉指标
easy-tdx indicator CCI -m SH -c 600519 --table        # CCI 顺势指标
easy-tdx indicator BIAS -m SZ -c 000001 --table       # BIAS 乖离率
easy-tdx indicator BIAS_SIGNAL -m SH -c 600519 --table # 30日乖离率信号
easy-tdx indicator OBV -m SZ -c 000001 --table        # OBV 能量潮

# 多指标同时计算
easy-tdx indicator MACD,KDJ,RSI,BOLL -m SH -c 600519 --count 10 --table

# 自定义参数
easy-tdx indicator MACD -m SH -c 600519 --params SHORT=10,LONG=22

# 分钟线指标
easy-tdx indicator MACD -m SH -c 600519 --period 5MIN --count 50

# 仅输出指标值（不含 OHLCV）
easy-tdx indicator RSI -m SZ -c 000001 --no-ohlcv
```

## 通达信公式（v1.27）

粘贴通达信公式即可计算 / 选股 / 回测——命名布尔输出自动成为买卖信号（名字含「买/卖」或 BUY/SELL 优先），30+ 白名单函数（MA/EMA/SMA/HHV/LLV/REF/CROSS/LONGCROSS/MACD/KDJ/RSI/BOLL/ATR…）向量化求值，无未来数据：

```bash
# 公式计算（最后一根各列值 + 最近信号明细）
easy-tdx formula compute SH 600519 --formula "金叉: CROSS(MA(C,5), MA(C,20));"

# 批量选股：信号在最后一根 = 1 的标的（--symbols 支持逗号分隔或 @文件）
easy-tdx formula screen --symbols SH:600519,SZ:000001 --formula "金叉: CROSS(MA(C,5), MA(C,20));"

# 公式回测：买/卖列自动挑选，信号下一根开盘成交，输出绩效 + 评级 + 评分
easy-tdx formula backtest SH 600519 --file my_formula.txt
```

## 捉妖大师（重点）

捉妖大师是多周期涨幅共振指标，通过 20/60/120 日涨幅及指数平滑判断短中长线趋势是否同向，用于筛选趋势刚启动的强势股。

```bash
easy-tdx indicator ZHUOYAO -m SH -c 600519 --count 30 --table

# 自定义周期参数
easy-tdx indicator ZHUOYAO -m SZ -c 000001 --params N1=90,N2=45,N3=15

# 结合其他指标一起看
easy-tdx indicator ZHUOYAO,MACD,KDJ -m SH -c 600519 --count 20 --table
```

输出列说明：

| 列名 | 含义 |
|------|------|
| `ZY_LONG` | 长线 — 120 日涨幅的 10 日指数平滑 |
| `ZY_MID` | 中线 — 60 日涨幅(%) |
| `ZY_SHORT` | 短线 — 20 日涨幅(%) |
| `ZY_TREND` | 趋势 — 中线的 10 日指数平滑 |

**核心信号：** 四线全部 > 0 且短线 > 中线 > 长线 = 短中长趋势完全一致向上，是强势股特征。详见 [捉妖大师指标详解](./indicator-zhuoyao.md)。

## 30日乖离率信号（重点）

30日乖离率信号指标，在标准乖离率（BIAS）基础上叠加短/长信号线，通过三者位置关系判断趋势方向和转折点。源自通达信经典指标。

```bash
easy-tdx indicator BIAS_SIGNAL -m SH -c 600519 --count 60 --table

# 自定义周期参数
easy-tdx indicator BIAS_SIGNAL -m SZ -c 000001 --params P=5,M=20

# 结合其他指标一起看
easy-tdx indicator BIAS_SIGNAL,MACD,KDJ -m SH -c 600519 --count 30 --table
```

输出列说明：

| 列名 | 含义 |
|------|------|
| `BS_X` | M日乖离率 — 当前价格偏离30日均线的百分比 |
| `BS_SMA` | 短周期信号线 — 乖离率的 P 日均线，过滤短期噪音 |
| `BS_LMA` | 长周期信号线 — 乖离率的 M 日均线，捕捉中期趋势方向 |

**核心信号：** X > S_SMA 且 X_LMA 上升 = 多头确认（通达信红色）；S_SMA > X 或 X_LMA 下降 = 空头预警（通达信绿色）。多空判断非对称设计——多头需两个条件同时满足，空头只需其一，偏向保守预警。详见 [30日乖离率信号指标详解](./indicator-bias-signal.md)。

```python
# Python API 用法
from easy_tdx import MacClient, Market

with MacClient.from_best_host() as c:
    df = c.get_stock_kline_with_indicators(
        Market.SH, "600519",
        indicators=["BIAS_SIGNAL"],
        count=60,
    )
    # df 包含: datetime, open, close, high, low, vol, amount
    #         + BS_X, BS_SMA, BS_LMA
```

支持 34 个指标：MACD, KDJ, RSI, BOLL, DMI, ATR, WR, CCI, BIAS, BIAS_SIGNAL, OBV, VR, EMV, MFI, BRAR, ASI, TRIX, DPO, MTM, ROC, EXPMA, BBI, PSY, DFMA, CR, KTN, XSII, MASS, TAQ, ZHUOYAO, SAR, VWAP, AROON, FK。

```python
# Python API 用法
from easy_tdx import MacClient, Market

with MacClient.from_best_host() as c:
    df = c.get_stock_kline_with_indicators(
        Market.SH, "600519",
        indicators=["ZHUOYAO"],
        count=30,
    )
    # df 包含: datetime, open, close, high, low, vol, amount
    #         + ZY_LONG, ZY_MID, ZY_SHORT, ZY_TREND
```

支持 34 个指标：MACD, KDJ, RSI, BOLL, DMI, ATR, WR, CCI, BIAS, BIAS_SIGNAL, OBV, VR, EMV, MFI, BRAR, ASI, TRIX, DPO, MTM, ROC, EXPMA, BBI, PSY, DFMA, CR, KTN, XSII, MASS, TAQ, ZHUOYAO, SAR, VWAP, AROON, FK。

