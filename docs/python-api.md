# Python API 使用指南

## 连接管理

所有客户端支持 `from_best_host()` 自动选最低延迟服务器：

```python
from easy_tdx import MacClient

with MacClient.from_best_host() as c:
    df = c.get_stock_kline(...)
```

| 客户端 | 端口 | 覆盖范围 |
|--------|------|----------|
| `MacClient` / `AsyncMacClient` | 7709 | A 股行情（MAC 协议，推荐） |
| `MacExClient` / `AsyncMacExClient` | 7727 | 港股/美股/期货（MAC 协议） |
| `UnifiedTdxClient` / `AsyncUnifiedTdxClient` | 自动 | A 股 + 扩展市场统一入口 |
| `TdxClient` / `AsyncTdxClient` | 7709 | A 股行情（标准协议） |

## MAC 协议（推荐）

### 报价

```python
from easy_tdx import MacClient, Market, Category, SortType, SortOrder

with MacClient.from_best_host() as c:
    # 批量报价（最多 80 只/次）
    df = c.get_stock_quotes([(Market.SH, "600519"), (Market.SZ, "000858")])

    # 市场分类排序报价
    df = c.get_stock_quotes_list(
        Category.A, count=20,
        sort_type=SortType.CHANGE_PCT,
        sort_order=SortOrder.DESC,
    )
```

返回列：`market, code, name` + 动态字段（`pre_close, open, high, low, close, vol, amount, turnover, vol_ratio` 等）。

### K 线（支持复权）

```python
from easy_tdx import MacClient, Market, Period, Adjust

with MacClient.from_best_host() as c:
    # 日K前复权
    df = c.get_stock_kline(Market.SH, "600519", Period.DAILY, count=10, adjust=Adjust.QFQ)
    # 5分钟线
    df = c.get_stock_kline(Market.SZ, "000001", Period.MIN_5, count=100)
```

返回列：`datetime, open, close, high, low, vol, amount`。

### 技术指标

自动获取 200+ 条历史数据预热 EMA，返回最后 `count` 条带指标的结果：

```python
from easy_tdx import MacClient, Market, Period, Adjust
from easy_tdx.indicator import compute_indicators, list_indicators

with MacClient.from_best_host() as c:
    # 便捷方法：获取 K 线 + 计算指标一步完成（默认前复权）
    df = c.get_stock_kline_with_indicators(
        Market.SH, "600519",
        indicators=["MACD", "KDJ", "RSI", "BOLL"],
        count=30,
    )
    # df 包含: datetime, open, close, high, low, vol, amount
    #         + MACD_DIF, MACD_DEA, MACD_HIST, KDJ_K, KDJ_D, KDJ_J, RSI,
    #           BOLL_UPPER, BOLL_MID, BOLL_LOWER

    # 自定义指标参数
    df = c.get_stock_kline_with_indicators(
        Market.SH, "600519",
        indicators=["MACD"],
        params={"MACD": {"SHORT": 10, "LONG": 22}},
    )

    # 独立使用：对已有 DataFrame 计算指标
    raw = c.get_stock_kline(Market.SH, "600519", Period.DAILY, count=200, adjust=Adjust.QFQ)
    result = compute_indicators(raw, ["ATR", "CCI", "WR"], tail=30)

    # 查看所有可用指标
    for info in list_indicators():
        print(info["name"], info["description"], info["outputs"])
```

支持 34 个技术指标：

| 指标 | 输入 | 输出列 |
|------|------|--------|
| MACD | close | MACD_DIF, MACD_DEA, MACD_HIST |
| KDJ | close, high, low | KDJ_K, KDJ_D, KDJ_J |
| RSI | close | RSI |
| BOLL | close | BOLL_UPPER, BOLL_MID, BOLL_LOWER |
| DMI | close, high, low | DMI_PDI, DMI_MDI, DMI_ADX, DMI_ADXR |
| ATR | close, high, low | ATR |
| WR | close, high, low | WR1, WR2 |
| CCI | close, high, low | CCI |
| BIAS | close | BIAS1, BIAS2, BIAS3 |
| OBV | close, vol | OBV |
| VR | close, vol | VR |
| EMV | high, low, vol | EMV, EMV_MA |
| MFI | close, high, low, vol | MFI |
| BRAR | open, close, high, low | AR, BR |
| ASI | open, close, high, low | ASI, ASI_MA |
| TRIX | close | TRIX, TRIX_MA |
| DPO | close | DPO, DPO_MA |
| MTM | close | MTM, MTM_MA |
| ROC | close | ROC, ROC_MA |
| EXPMA | close | EXPMA_12, EXPMA_50 |
| BBI | close | BBI |
| PSY | close | PSY, PSY_MA |
| DFMA | close | DFMA_DIF, DFMA_DMA |
| CR | close, high, low | CR |
| KTN | close, high, low | KTN_UPPER, KTN_MID, KTN_LOWER |
| XSII | close, high, low | XSII_TD1, XSII_TD2, XSII_TD3, XSII_TD4 |
| MASS | high, low | MASS, MASS_MA |
| TAQ | high, low | TAQ_UP, TAQ_MID, TAQ_DOWN |
| ZHUOYAO | close | ZY_LONG, ZY_MID, ZY_SHORT, ZY_TREND |
| BIAS_SIGNAL | close | BS_X, BS_SMA, BS_LMA |
| SAR | high, low | SAR（抛物线转向/动态止损位） |
| VWAP | close, high, low, vol | VWAP（N日滚动成交量加权均价） |
| AROON | high, low | AROON_UP, AROON_DOWN, AROON_OSC |
| FK | close | FK（EMA(2) 突破斜率外推 EMA(42)） |

### 分时

```python
with MacClient.from_best_host() as c:
    df = c.get_tick_chart(Market.SH, "600519")          # 单日分时
    df = c.get_tick_charts(Market.SH, "600519", days=3)  # 多日分时（最多5天）
    df = c.get_chart_sampling(Market.SH, "600519")       # 240点缩略采样
```

### 逐笔成交

```python
with MacClient.from_best_host() as c:
    df = c.get_transactions(Market.SH, "600519", count=100)
    df = c.get_transactions(Market.SH, "600519", count=100, date=20250115)
```

### 板块

```python
from easy_tdx import BoardSortColumn, BoardType

with MacClient.from_best_host() as c:
    df = c.get_board_list(BoardType.GN)                       # 概念板块
    df = c.get_board_list(sort_column=BoardSortColumn.SPEED)  # 按涨速排序取涨速%
    df = c.get_board_members("881001", sort_type=SortType.CHANGE_PCT)
    df = c.get_belong_board(Market.SZ, "000001")              # 个股所属板块

    # 板块汇总：成交额、主力净流入、涨跌家数
    summary = c.get_board_summary("881001")
    # summary = {
    #     "member_count": 82,
    #     "amount": 5823456000.0,        # 板块总成交额（元）
    #     "vol": 412356789,              # 板块总成交量（股）
    #     "main_net_amount": -123456.0,  # 当日主力净流入
    #     "main_net_3d": -567890.0,      # 近3日主力净流入
    #     "main_net_5d": -234567.0,      # 近5日主力净流入
    #     "up_count": 45,
    #     "down_count": 37,
    #     "members": DataFrame(...),     # 成分股明细
    # }

    # 板块涨跌幅排行榜
    df = c.get_board_ranking(BoardType.HY, top_n=10, sort_by="change_pct")
    df = c.get_board_ranking(BoardType.GN, top_n=20, sort_by="main_net_amount")
    # 返回列：code, name, change_pct, amount, vol, main_net_amount, up_count, down_count, member_count

    # 板块 N 日涨跌幅排行（支持指定截止日期，默认全部）
    df = c.get_board_change_ranking(BoardType.HY, days=20)
    df = c.get_board_change_ranking(BoardType.GN, target_date=20250530, days=10, top_n=15)
    # 返回列：code, name, close_end, close_start, change_pct
```

### 资金流向

```python
with MacClient.from_best_host() as c:
    df = c.get_capital_flow(Market.SH, "600519")
```

返回列：`date, main_in, main_out, main_net, small_in/out/net, mid_in/out/net, large_in/out/net`。

### 监控

```python
with MacClient.from_best_host() as c:
    df = c.get_auction(Market.SH, "600519")     # 集合竞价
    df = c.get_unusual(Market.SH)               # 市场异动
    df = c.get_symbol_info(Market.SZ, "000001") # 个股特征快照
    df = c.get_server_info()                     # 服务器交易时段
```

`get_unusual` 返回列含 `unusual_type`（类型码）与 `desc`（中文描述），共 19 种类型
（主力买卖/加速拉升/急速拉升/盘中强弱/竞价异动/涨跌停/大单盘口等）。类型码→名称
可用顶层常量映射：

```python
from easy_tdx import UNUSUAL_TYPE_NAMES

df["type_name"] = df["unusual_type"].map(UNUSUAL_TYPE_NAMES)
```

## 扩展市场

```python
from datetime import date

from easy_tdx import MacExClient, ExMarket, Period

with MacExClient.from_best_host() as c:
    count = c.goods_count(ExMarket.HK_MAIN_BOARD)
    df = c.goods_list(ExMarket.HK_MAIN_BOARD, start=0, count=50)
    df = c.goods_kline(ExMarket.US_STOCK, "AAPL", Period.DAILY, count=10)
    df = c.goods_quotes([(ExMarket.HK_MAIN_BOARD, "00700")])
    df = c.goods_tick_chart(ExMarket.HK_MAIN_BOARD, "00700")
    df = c.goods_transaction(ExMarket.HK_MAIN_BOARD, "00700", count=100)
    df = c.goods_transaction_all(ExMarket.HK_MAIN_BOARD, "00700", date(2026, 7, 3))  # 港股当日全部逐笔
```

> **逐笔成交排序**：通达信协议为**倒序**——`start=0` 指向最新一笔（收盘方向），`count=2000` 默认只取最近 2000 笔。港股单日成交常达数万笔（如 02715 约 1.3 万笔/日），若需当日全部成交，用 `goods_transaction_all`（仅港股股票类市场，自动按 1800/页翻页取全天，安全上限 9 万条；返回协议原生倒序，需正序展示自行 `df.iloc[::-1]`）。

## 统一客户端

```python
from easy_tdx import UnifiedTdxClient, ExMarket, Market, Period

with UnifiedTdxClient() as client:
    # A 股 -- 自动路由到 MacClient
    df = client.get_stock_kline(Market.SH, "600519", Period.DAILY, count=5)
    df = client.get_stock_quotes([(Market.SH, "600519")])
    df = client.get_board_list()

    # 扩展市场 -- 自动路由到 MacExClient
    df = client.goods_kline(ExMarket.HK_MAIN_BOARD, "00700", Period.DAILY, count=5)
```

## 标准协议

```python
from easy_tdx import TdxClient, Market, KlineCategory

with TdxClient.from_best_host() as c:
    count = c.get_security_count(Market.SH)
    stocks = c.get_security_list(Market.SH, start=0)
    quotes = c.get_security_quotes([(Market.SH, "600000"), (Market.SZ, "000001")])
    bars = c.get_security_bars(Market.SZ, "002176", KlineCategory.DAY, 0, 100)
    minute = c.get_minute_time_data(Market.SH, "600000")
    trades = c.get_transaction_data(Market.SH, "600000", 0, 20)
    flow = c.get_fund_flow(Market.SH, "600519")
    blocks = c.get_block_info("block_gn.dat")
    xdxr = c.get_xdxr_info(Market.SH, "600519")
    stat = c.get_market_stat()
```

> **资金流口径注意**（Issue #55）：`get_fund_flow` / `get_history_fund_flow` 按 0x0fb5 逐笔接口的"单笔成交额"分档，而该接口返回的记录是交易所真实逐笔**聚合**后的（实测 000001.SZ 单日约 17:1），分档看的也不是挂单额。结果：高价股小单档可不足成交额 1%、主力档常占 95%+，`main_net_inflow` 实质更接近"当日主动买卖总失衡"。东财/同花顺的"主力净流入"基于 L2 逐笔委托、按挂单额分档——两套口径**不可比**（实证同规则选股信号重合度仅约 14%），勿混用于同一张表或同一个因子。

`AsyncTdxClient` 提供对应的 `async def` 方法，接口一一对应。

## SecurityQuote 字段说明

`get_security_quotes()` 返回的 DataFrame 包含以下特殊字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `trading_status` | int | 交易状态标志。`0x8020`(32800) = 停牌，其余值表示正常交易或集合竞价 |
| `open_amount` | float | 集合竞价成交金额（元）。仅个股有效，指数该字段无意义 |
| `server_time` | str | 服务器时间，格式 `HH:MM:SS.mmm` |
| `unknown_2` | int | 指数: 集合竞价成交金额/100；个股: 舍入残差≈0 |
| `unknown_3` | int | 个股: 集合竞价成交金额/100；指数: 负值/无意义 |
| `unknown_5-8` | int | 保留字段，恒为 0 |

检测停牌：

```python
df = c.get_security_quotes([(Market.SH, "600000")])
is_suspended = df.iloc[0]["trading_status"] == 0x8020
```


## 公告检索（巨潮资讯网）

独立数据源（巨潮资讯网 cninfo），无需连接 TDX 行情服务器即可检索公司公告。
标准库 urllib 实现，零额外依赖。

```python
from easy_tdx.cninfo import CninfoClient

client = CninfoClient()

# 检索公告（默认 30 条，最新在前）
df = client.get_announcements("688017")
# → DataFrame[title, type, date, url, code, org_id, announcement_id, announcement_time, pdf_url]

# 翻页 + 自定义数量
df = client.get_announcements("601088", count=10, page=2)

# 返回示例（url 含 4 参数可直点打开，pdf_url 为 PDF 直链）：
#   title                       type     date        url                                              pdf_url
# 0 关于召开2025年年度股东大会... 股东大会 2025-06-14 .../detail?stockCode=688017&announcementId=... http://static.cninfo.com.cn/.../xxx.PDF
# 1 2024年年度报告              PDF      2025-03-28 .../detail?stockCode=688017&announcementId=... http://static.cninfo.com.cn/.../yyy.PDF
```

> - ``type`` 优先取 cninfo 的 ``announcementTypeName``；该字段对很多公告为 null
>   （数据源限制），此时回退到 ``adjunctType``（如 "PDF"），再为空给空字符串。
> - ``url`` 必须含 4 参数（``stockCode``/``announcementId``/``orgId``/``announcementTime``）
>   才能打开，少参数会 404。
> - orgId 解析沿用 #19 修复：动态拉取官方映射表，查不到回退硬编码规则，
>   保证 601xxx 等非标 orgId 段也能正常查询。

### 下载公告 PDF

```python
# 下载最新一条公告的 PDF 到当前目录
df = client.get_announcements("601088", count=5)
path = client.download_pdf(df.iloc[0])  # 接受 Announcement 或 DataFrame 的一行
print(path)  # /abs/path/20260605_1225351400.PDF

# 批量下载
for _, row in df.iterrows():
    try:
        path = client.download_pdf(row, dest_dir="./pdfs")
    except Exception as e:
        print(f"跳过（无附件或失败）: {e}")
```

## 财报三表（新浪财经）

独立数据源（新浪财经），无需连接 TDX 行情服务器即可获取利润表/资产负债表/现金流量表。
标准库 urllib 实现，零额外依赖。

```python
from easy_tdx.sina import SinaClient

client = SinaClient()

# 利润表（默认 8 期，最新在前）
df = client.get_financial_report("600519", report_type="lrb")
# → DataFrame，每行一期，列 = [报告期, 营业总收入, 营业总收入_同比, ...]

# 资产负债表 / 现金流量表（report_type 也接受中文别名：利润表/资产负债表/现金流量表）
df = client.get_financial_report("600519", report_type="fzb", num=4)
df = client.get_financial_report("600519", report_type="llb", num=4)

# 返回示例（item_value 已转 float，可直接数值计算）：
#         报告期      营业总收入  营业总收入_同比       营业收入  营业收入_同比
# 0  2026-03-31  54702912385.23        0.06336  53909252220.51        0.06538
# 1  2025-12-31 174000000000.00        0.10000            NaN            NaN
```

> - ``item_value`` 是字符串（新浪原始格式），本实现转 float；空/非数值转 None
> - 有同比的科目附加 ``{科目}_同比`` 列（float 比例，如 0.06336 = +6.3%）
> - 大类标题行（如 ``流动资产``，原 ``item_value=""``）保留为 None，反映报表结构

## 实时行情轮询（RealtimeDataFeed）

> ⚠️ 通达信协议**没有服务端推送**，只有请求/响应。本模块的「实时」是 **轮询五档快照近似**
> （默认约 3 秒延迟），适合盘中信号提醒、轻量监控；**不适合高频 / 逐笔撮合**。

`EventBus` 自身是纯发布/订阅管道，不会产生数据。要让 `RealtimeStrategy` 跑起来，
需要配合 `RealtimeDataFeed`：它自动完成 `get_stock_quotes → MarketEvent → bus.publish`。

```python
import asyncio
from easy_tdx.mac.client import AsyncMacClient
from easy_tdx.realtime import (
    EventBus,
    RealtimeStrategy,
    MarketEvent,
    RealtimeDataFeed,
)


class MyStrategy(RealtimeStrategy):
    def on_tick(self, event: MarketEvent) -> None:
        print(f"{event.market}{event.code} price={event.price} vol={event.volume}")


async def main():
    bus = EventBus()
    strategy = MyStrategy()
    bus.subscribe("SZ000001", strategy.on_tick)   # 注意：key 必须带市场前缀

    feed = RealtimeDataFeed(
        bus=bus,
        symbols=[(0, "000001"), (1, "600519")],   # [(Market.SZ, code), ...]，单批 ≤80 只
        interval=3.0,     # 轮询间隔（秒），下限 0.1
        dedup=True,       # (price, volume) 未变的标的跳过发布
        # sessions=(),    # 传空 tuple 表示全天轮询；默认仅 9:15-11:30 / 13:00-15:00
    )
    async with AsyncMacClient.from_best_host() as client:
        await feed.run_async(client)   # Ctrl+C 或 feed.stop() 退出


asyncio.run(main())
```

同步客户端（`MacClient`）用 `run_sync`，feed 会把阻塞调用丢到线程池，不卡事件循环：

```python
from easy_tdx.mac.client import MacClient

with MacClient.from_best_host() as client:
    feed.run_sync(client)
```

> **关键坑（issue #34）**：
> - 订阅 key 必须与 `publish` 内部拼的 `f"{market}{code}"` 一致，即 `"SZ000001"`
>   而不是 `"000001"`；否则事件分发匹配不到。
> - 传给 `subscribe` 的必须是**实例的绑定方法** `strategy.on_tick`，
>   不是未绑定的类方法 `MyStrategy.on_tick`。
> - 整个流程跑在 `asyncio.run()` 里，否则协程不会被调度。

