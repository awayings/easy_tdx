# easy_tdx API 参考文档

> 本文档为**方法速查参考**；上手教程见 [python-api.md](./python-api.md)，数据模型与枚举字段见 [field_mapping.md](./field_mapping.md)，Web 服务端点见 [web-api.md](./web-api.md)。
>
> 文档不写死版本号，对应版本以 [CHANGELOG.md](../CHANGELOG.md) 与 pyproject.toml 为准。

## 目录

- [客户端](#客户端)
  - [TdxClient（同步）](#tdxclient同步)
  - [AsyncTdxClient（异步）](#asynctdxclient异步)
- [连接与服务器选择](#连接与服务器选择)
- [市场信息](#市场信息)
- [K 线数据](#k-线数据)
- [分时数据](#分时数据)
- [逐笔成交](#逐笔成交)
- [财务与公司信息](#财务与公司信息)
- [板块信息](#板块信息)
- [资金流向](#资金流向)
- [文件下载](#文件下载)
- [市场统计](#市场统计)
- [异常](#异常)
- [涨跌停价计算](#涨跌停价计算)
- [全局常量](#全局常量)
- [完整 API 列表（MAC 协议客户端）](#完整-api-列表mac-协议客户端)

---

## 客户端

### TdxClient（同步）

```python
TdxClient(host, port=7709, timeout=15.0, auto_reconnect=True)
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| host | `str` | `KNOWN_HOSTS[0]` | 服务器 IP 地址 |
| port | `int` | `7709` | 服务器端口 |
| timeout | `float` | `15.0` | 连接/读写超时（秒） |
| auto_reconnect | `bool` | `True` | 断线自动重连 |

支持上下文管理器：`with TdxClient(...) as c:`

#### 工厂方法

```python
TdxClient.from_best_host(hosts=KNOWN_HOSTS, port=7709, timeout=15.0,
                          ping_timeout=5.0, auto_reconnect=True)
```

测量 `hosts` 中所有服务器延迟，选择最低延迟的建立连接。若全部不可达，回退到 `hosts[0]`。

### AsyncTdxClient（异步）

```python
AsyncTdxClient(host, port=7709, timeout=15.0, auto_reconnect=True, heartbeat_interval=60.0)
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| heartbeat_interval | `float` | `60.0` | 心跳间隔（秒），≤0 禁用 |

所有方法均为 `async def`，使用 `await` 调用。支持异步上下文管理器：`async with AsyncTdxClient(...) as c:`

> **注意**：单个 AsyncTdxClient 仅维护一条 TCP 连接，并发调用在连接内串行执行。

---

## 连接与服务器选择

### ping_all

```python
TdxClient.ping_all(hosts=KNOWN_HOSTS, port=7709, timeout=5.0) -> list[tuple[str, float]]
```

测量多台服务器延迟，返回按延迟升序排列的 `(host, seconds)` 列表。

**示例**：
```python
results = TdxClient.ping_all()
for host, latency in results:
    print(f"{host}: {latency * 1000:.1f} ms")
```

### connect / close

```python
c.connect()  # 建立连接
c.close()    # 关闭连接
```

建议使用上下文管理器自动管理。

---

## 市场信息

### get_security_count

```python
c.get_security_count(market: Market) -> int
```

获取指定市场的证券总数。

| 参数 | 类型 | 说明 |
|------|------|------|
| market | `Market` | 市场代码（SZ/SH/BJ） |

### get_security_list

```python
c.get_security_list(market: Market, start: int) -> list[SecurityInfo]
```

获取证券列表（每页约 1000 条）。

| 参数 | 类型 | 说明 |
|------|------|------|
| market | `Market` | 市场代码 |
| start | `int` | 分页偏移量（0, 1000, 2000, ...） |

### get_security_list_all

```python
c.get_security_list_all() -> list[SecurityInfo]
```

获取沪深 A 股完整列表，自动挂载行业信息（通达信行业 + 申万行业）。

**注意**：
- 内部会拉取 `tdxhy.cfg` 并遍历全部证券，耗时较长
- `Market.BJ` 因服务器端问题暂不纳入

**A股过滤规则**：
- 沪市：60xxxx（主板）、68xxxx（科创板）
- 深市：00xxxx（主板）、30xxxx（创业板）

### get_security_quotes

```python
c.get_security_quotes(stocks: list[tuple[Market, str]]) -> list[SecurityQuote]
```

批量获取实时五档行情，**最多 80 只/次**。

| 参数 | 类型 | 说明 |
|------|------|------|
| stocks | `list[tuple[Market, str]]` | (市场, 代码) 列表 |

---

## K 线数据

### get_security_bars

```python
c.get_security_bars(market: Market, code: str, category: KlineCategory,
                     start: int, count: int = 800, *, bar_time: str = "start") -> pd.DataFrame
```

获取个股 K 线数据。

| 参数 | 类型 | 说明 |
|------|------|------|
| market | `Market` | 市场代码 |
| code | `str` | 证券代码（如 "600000"） |
| category | `KlineCategory` | K 线周期 |
| start | `int` | 分页偏移（0 为最新） |
| count | `int` | 请求数量（最多 800） |
| bar_time | `str` | 时间戳语义，见下方说明 |

**bar_time（分钟级周期时间戳对齐）**：通达信协议默认用 bar **开始时间**打时间戳
（5min 线上午最后一根标 11:25、下午第一根标 13:00；午休 11:30–13:00 无 bar）。
传 `bar_time="end"` 切换为 bar **右端点**（= 开始 + 周期时长，标 11:30/13:05），
对齐 Tushare / 同花顺 / 聚宽约定。仅对分钟级周期（MIN_1/5/15/30/60）生效，
日线及以上不受影响。默认 `"start"` 保持完全向后兼容。

### get_index_bars

```python
c.get_index_bars(market: Market, code: str, category: KlineCategory,
                  start: int, count: int = 800, *, bar_time: str = "start") -> pd.DataFrame
```

获取指数 K 线数据。参数（含 `bar_time`）同 `get_security_bars`。

**常用指数**：
| 指数 | market | code |
|------|--------|------|
| 上证指数 | SH | 000001 |
| 深证成指 | SZ | 399001 |
| 创业板指 | SZ | 399006 |
| 沪深300 | SH | 000300 |

---

## 分时数据

### get_minute_time_data

```python
c.get_minute_time_data(market: Market, code: str) -> list[MinuteBar]
```

获取今日分时数据（240 条）。内部优先尝试历史接口，失败后回退到实时接口。

### get_history_minute_time_data

```python
c.get_history_minute_time_data(market: Market, code: str, date: int) -> list[MinuteBar]
```

获取历史某日分时数据。

| 参数 | 类型 | 说明 |
|------|------|------|
| date | `int` | YYYYMMDD 格式（如 20250110） |

---

## 逐笔成交

### get_transaction_data

```python
c.get_transaction_data(market: Market, code: str,
                        start: int, count: int = 800) -> list[TransactionRecord]
```

获取当日逐笔成交。

### get_history_transaction_data

```python
c.get_history_transaction_data(market: Market, code: str, date: int,
                                start: int, count: int = 800) -> list[TransactionRecord]
```

获取历史逐笔成交。

| 参数 | 类型 | 说明 |
|------|------|------|
| date | `int` | YYYYMMDD 格式 |
| start | `int` | 分页偏移 |
| count | `int` | 请求数量（最多 800） |

---

## 财务与公司信息

### get_xdxr_info

```python
c.get_xdxr_info(market: Market, code: str) -> list[XdxrRecord]
```

获取除权除息历史记录。返回值按时间排序，包含分红、送股、配股、股本变动等。

### get_finance_info

```python
c.get_finance_info(market: Market, code: str) -> FinanceInfo
```

获取最新财务数据，包含股本结构、资产负债、利润指标等。

### get_company_info_category

```python
c.get_company_info_category(market: Market, code: str) -> list[CompanyInfoCategory]
```

获取公司信息文件目录，返回可用的文件名、起始偏移和长度。

### get_company_info_content

```python
c.get_company_info_content(market: Market, code: str, filename: str,
                            offset: int, length: int) -> str
```

读取公司信息文本内容。需先通过 `get_company_info_category` 获取文件名和长度。

---

## 板块信息

### get_block_info

```python
c.get_block_info(filename: str) -> list[TdxBlock]
```

获取并解析板块文件。

**常用文件名**：
| 文件名 | 说明 |
|--------|------|
| `block_zs.dat` | 行业/指数板块 |
| `block_gn.dat` | 概念板块 |
| `block_fg.dat` | 风格板块 |

---

## 资金流向

### get_fund_flow

```python
c.get_fund_flow(market: Market, code: str) -> pd.DataFrame
```

获取个股当日资金流向（基于 L1 逐笔数据统计）。返回含 `main_net_inflow`（主力净流入）列。
口径限制见下方 `get_history_fund_flow` 的"口径注意"（两个接口同，Issue #55）。

**资金分级**：
| 级别 | 单笔成交额 |
|------|-----------|
| 超大单 | > 100 万 |
| 大单 | 20 ~ 100 万 |
| 中单 | 4 ~ 20 万 |
| 小单 | ≤ 4 万 |

### get_history_fund_flow

```python
c.get_history_fund_flow(market: Market, code: str,
                         start: int, count: int) -> pd.DataFrame
```

获取历史日线资金流向序列，由"日K线取日期 + 逐笔成交重算"实现（标准服务器
无资金流专用指令，Issue #52）。当日 bar 盘中取当日实时逐笔。返回列含
`main_net_inflow`（主力净流入，单位元）。

**口径注意**（Issue #55，两个接口同）：分档基于 0x0fb5 逐笔接口返回的"单笔
成交额"，而该接口的记录是交易所真实逐笔**聚合**后的（实测 000001.SZ 单日
约 17:1），分档看的也不是挂单额。高价股单笔普遍被聚合推过 100 万/20 万阈值，
小单档可不足成交额 1%、主力档常占 95%+——`main_net_inflow` 实质更接近
"当日主动买卖总失衡"（另有约 2–4% 方向未定的成交被排除）。东财/同花顺的
"主力净流入"基于 L2 逐笔委托、按挂单额分档、四档净额严格归零——两套口径
**不可比**（实证同规则选股信号重合度仅约 14%），勿混用于同一张表或同一个因子。

---

## 文件下载

### get_report_file

```python
c.get_report_file(filename: str) -> bytes
```

从服务器拉取大文件（分块传输）。

**常用文件**：
| 文件名 | 说明 |
|--------|------|
| `base_info.zip` | 基础信息包 |
| `tdxhy.cfg` | 行业映射配置 |

---

## 市场统计

### get_market_stat

```python
c.get_market_stat() -> MarketStat
```

获取 A 股全市场涨跌统计（基于 880005 行情统计代码）。

**注意**：`suspended_count` 是 `total - up - down - neutral` 的残差估算值。

---

## 异常

所有异常继承自 `TdxError`。

| 异常 | 说明 |
|------|------|
| `TdxError` | 基础异常 |
| `TdxConnectionError` | 连接错误（断线、超时等） |
| `TdxDecodeError` | 数据解析错误 |
| `TdxCommandError` | 命令执行错误 |

---

## 涨跌停价计算

### get_price_limits

```python
c.get_price_limits(market: Market, code: str, name: str,
                    pre_close: float) -> tuple[float | None, float | None]
```

按交易规则计算涨跌停价。返回 `(涨停价, 跌停价)`，不适用时对应位置为 `None`。

内部逻辑：
- 自动检测上市初期不设涨跌幅限制的窗口期
- 通过日 K 线条数估算已上市交易天数
- 调用 `compute_price_limits()` 执行规则计算

### compute_price_limits（独立函数）

```python
from easy_tdx.codec.price_rules import compute_price_limits

compute_price_limits(market, code, name, pre_close, listed_days=None)
    -> tuple[float | None, float | None]
```

涨跌幅规则：
| 类型 | 涨跌幅 |
|------|--------|
| 主板（60/00） | ±10% |
| 科创板（68） | ±20% |
| 创业板（30） | ±20% |
| ST 股 | ±5% |
| 上市首 N 日 | 不设限制 |

---

## 全局常量

| 常量 | 类型 | 说明 |
|------|------|------|
| `KNOWN_HOSTS` | `list[str]` | A 股行情服务器列表 |
| `KNOWN_EX_HOSTS` | `list[str]` | 扩展行情服务器列表 |
| `XDXR_CATEGORY_NAMES` | `dict[int, str]` | 除权除息事件类型映射 |

## 完整 API 列表（MAC 协议客户端）

### MacClient / AsyncMacClient

| 方法 | 说明 |
|------|------|
| `get_stock_quotes(stocks, fields)` | 批量实时报价 |
| `get_stock_quotes_list(category, ...)` | 市场分类排序报价 |
| `get_stock_kline(market, code, period, ...)` | K 线（支持复权） |
| `get_stock_kline_with_indicators(market, code, indicators, ...)` | K 线 + 技术指标 |
| `get_tick_chart(market, code, date)` | 单日分时图 |
| `get_tick_charts(market, code, days)` | 多日分时图 |
| `get_chart_sampling(market, code)` | 分时缩略采样 |
| `get_transactions(market, code, ...)` | 逐笔成交 |
| `get_symbol_info(market, code)` | 个股特征快照 |
| `get_board_list(board_type, ..., sort_column)` | 板块列表（sort_value 列=排序键指标值） |
| `get_board_members(board_symbol, ...)` | 板块成分股报价 |
| `get_board_summary(board_symbol, ...)` | 板块汇总（成交额、主力净流入、涨跌家数） |
| `get_board_ranking(board_type, top_n, sort_by, ...)` | 板块涨跌幅排行榜（行业/概念排行） |
| `get_board_change_ranking(board_type, target_date, days, ...)` | 板块 N 日涨跌幅排行 |
| `get_belong_board(market, code)` | 个股所属板块 |
| `get_capital_flow(market, code)` | 资金流向 |
| `get_auction(market, code)` | 集合竞价 |
| `get_unusual(market, ...)` | 市场异动 |
| `get_server_info()` | 服务器交易时段 |
| `get_kline_offset(offset, count)` | K 线偏移信息 |
| `get_goods_list(market, ...)` | 扩展市场商品列表 |

### MacExClient / AsyncMacExClient

| 方法 | 说明 |
|------|------|
| `goods_count(market)` | 商品总数 |
| `goods_list(market, start, count)` | 商品列表 |
| `goods_quotes(stocks, fields)` | 批量报价 |
| `goods_quotes_list(market, ...)` | 市场分类报价列表 |
| `goods_kline(market, code, period, ...)` | K 线（支持复权） |
| `goods_tick_chart(market, code, ...)` | 分时图 |
| `goods_chart_sampling(market, code)` | 分时缩略采样 |
| `goods_transaction(market, code, ...)` | 逐笔成交 |

