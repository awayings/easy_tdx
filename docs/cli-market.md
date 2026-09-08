# CLI 参考 — 行情数据

## 输出格式

`easy-tdx` 默认输出 JSON（一行一条记录），`--table` 切换表格，`--output csv` 输出 CSV。

## 基础

```bash
easy-tdx ping                    # 服务器测速
easy-tdx version                 # 版本号
```

## 行情

```bash
# K 线
easy-tdx kline SZ 000001 --count 30 --table
easy-tdx kline SH 600519 --period 5MIN --adjust QFQ

# 实时报价
easy-tdx quote "SZ 000001,SH 600519" --table

# 市场分类报价（按涨幅排序）
easy-tdx quote-list A --count 20 --table
easy-tdx quote-list KCB --sort TOTAL_AMOUNT --order ASC
easy-tdx quote-list CYB --count 50
```

## 分时 / 成交

```bash
easy-tdx tick SZ 000001 --table
easy-tdx tick SH 600519 --days 5
easy-tdx tick SZ 000001 --date 20250115

easy-tdx transaction SZ 000001 --count 100 --table
easy-tdx transaction SH 600519 --date 20250115
```

## 板块

```bash
easy-tdx board-list --type GN --table
easy-tdx board-list --type HY --count 200
easy-tdx board-members 881001 --table
easy-tdx belong-board SZ 000001 --table
easy-tdx board-summary 881001 --table          # 板块汇总（成交额/主力净流入/涨跌家数）
easy-tdx board-summary 881001 --members --table # 含成分股明细
easy-tdx board-ranking --type HY --top 10 --table   # 行业板块排行
easy-tdx board-ranking --type GN --sort-by amount    # 概念板块按成交额排行

# 板块 N 日涨跌幅排行（默认全部，支持指定日期）
easy-tdx board-change-ranking --table                      # 行业 20 日涨跌幅排行
easy-tdx board-change-ranking --type GN --days 10 --table  # 概念 10 日涨跌幅排行
easy-tdx board-change-ranking --type HY --date 20250530 --days 20 --table
easy-tdx board-change-ranking --type HY --top 10 --asc     # 行业跌幅前10
```

## 资金 / 监控

```bash
easy-tdx capital-flow SH 600519 --table
easy-tdx auction SZ 000001 --table
easy-tdx unusual SH --count 100 --table
easy-tdx market-stat --table
easy-tdx server-info --table
easy-tdx symbol-info SZ 000001 --table
```

## 中金所成交持仓排名（v1.29.1）

```bash
easy-tdx ccpm IF --table                       # 最近有数据的交易日（缺省自动回溯）
easy-tdx ccpm IF --date 2026-09-02             # 指定交易日
easy-tdx ccpm all --date 2026-08-28 --table    # 全部 8 个品种一次抓取
easy-tdx ccpm TL --refresh                     # 忽略本地缓存，强制重新抓取
```

> 独立数据源（中金所官网），无需连接 TDX 行情服务器。品种：IF 沪深300 / IH 上证50 / IC 中证500 / IM 中证1000 股指期货，TS/TF/T/TL 为 2/5/10/30 年期国债期货。
> 每个交易日收盘后约 16:15 发布，含该品种**全部合约 × 三类排名（成交量 / 持买单量·多单 / 持卖单量·空单）× 各前 20 名会员**；
> 数据发布后不可变，按日缓存到 `~/.easy_tdx/cache/ccpm/`，历史二次查询零网络。
> JSON 输出为英文列名（vol/long_pos/short_pos 等，机器友好），`--table` 自动切换中文表头。
> WebUI 对应「期货持仓排名」页（含新手科普），API 为 `GET /api/v1/ccpm/rank`。

## 扩展市场（港股/美股/期货）

```bash
easy-tdx ex markets                                       # 列出可用市场
easy-tdx ex kline HK_MAIN_BOARD 00700 --count 30 --table  # 港股 K 线
easy-tdx ex kline US_STOCK AAPL --table                    # 美股 K 线
easy-tdx ex quote US_STOCK TSLA --table                    # 美股报价
easy-tdx ex quote-list HK_MAIN_BOARD --table               # 港股商品列表
easy-tdx ex tick HK_MAIN_BOARD 00700 --table               # 港股分时
```
