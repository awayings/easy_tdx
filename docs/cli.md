# CLI 命令总览

`easy-tdx` 全部命令速查表。按域的详细用法与示例见分域文档：

| 域 | 文档 |
|------|------|
| 行情 / 分时 / 板块 / 资金 / 中金所 / 扩展市场 | [cli-market.md](./cli-market.md) |
| 公告 / 财务 / F10 | [cli-finance.md](./cli-finance.md) |
| 技术指标 / 通达信公式 | [cli-indicators.md](./cli-indicators.md) |
| 缠论分析 | [chanlun.md](./chanlun.md) |
| 回测 / 寻优 / 组合 / run-all | [cli-backtest.md](./cli-backtest.md) |
| 选股扫描 / 强势股排名 | [cli-screen.md](./cli-screen.md) |
| K 线仓库 / 离线数据 | [data-local.md](./data-local.md) |
| Web 服务（serve） | [web-api.md](./web-api.md) |

## 命令汇总

| 命令 | 说明 |
|------|------|
| `ping` | 服务器延迟测速 |
| `version` | 版本号 |
| `kline` | K 线（日/周/月/分钟，支持复权） |
| `quote` | 实时报价（单只/批量） |
| `quote-list` | 市场分类排序报价（A/SH/SZ/KCB/CYB） |
| `tick` | 分时图（单日/多日/历史） |
| `transaction` | 逐笔成交 |
| `board-list` | 板块列表（行业/概念/风格） |
| `board-members` | 板块成分股报价 |
| `board-summary` | 板块汇总（成交额、主力净流入、涨跌家数） |
| `board-ranking` | 板块涨跌幅排行榜（行业/概念排行） |
| `board-change-ranking` | 板块 N 日涨跌幅排行（支持指定截止日期） |
| `belong-board` | 个股所属板块 |
| `capital-flow` | 资金流向 |
| `auction` | 集合竞价 |
| `unusual` | 市场异动 |
| `market-stat` | 全市场涨跌统计 |
| `server-info` | 服务器交易时段 |
| `symbol-info` | 个股特征快照 |
| `indicator` | 技术指标计算（34 个：MACD/KDJ/RSI/BOLL/DMI/ATR...） |
| `indicator-list` | 列出可用技术指标 |
| `chanlun` | 缠论分析（笔/中枢/线段/买卖点/背驰，支持多级别联立） |
| `indicator ZHUOYAO` | 捉妖大师信号（多周期 ROC 共振），详解见 [indicator-zhuoyao.md](./indicator-zhuoyao.md) |
| `indicator BIAS_SIGNAL` | 30 日乖离率信号，详解见 [indicator-bias-signal.md](./indicator-bias-signal.md) |
| `backtest` | 回测引擎（加载策略文件，输出绩效报告） |
| `portfolio` | 多标的组合回测（共享资金池，均等分配，汇总绩效） |
| `factor list` | 列出所有内置因子 |
| `factor analyze` | 因子分析（IC/分层/衰减） |
| `pfactor backtest` | 组合因子选股回测 |
| `run-all` | 批量运行所有策略并排名（绩效排名 + 综合评分 + 可选图表） |
| `optimize` | 参数网格寻优（单策略网格搜索，或 `--all` 一键寻优所有内置策略并排名） |
| `strategies` | 列出内置策略注册表（名称/中文标签/参数默认值/预设寻优网格） |
| `formula compute` | 通达信公式计算（命名布尔输出即买卖信号） |
| `formula screen` | 公式批量选股（信号在最后一根 = 1 的标的） |
| `formula backtest` | 公式回测（买/卖列自动挑选，输出绩效 + 评级 + 评分） |
| `warehouse sync` | 本地 K 线仓库增量同步（DuckDB，需 `easy-tdx[warehouse]`） |
| `warehouse query` | 仓库查询（默认忽略未收盘的临时 bar） |
| `warehouse stats` | 仓库统计（各标的行数/数据范围） |
| `warehouse check` | 仓库健康自检（缺口/除权跳变/新鲜度） |
| `ccpm` | 中金所成交持仓排名（官网每日发布，按日落盘缓存） |
| `announcement` | 公告检索（巨潮资讯网，独立数据源，支持下载 PDF） |
| `screen scan` | 策略选股扫描（纯离线，全市场信号扫描） |
| `screen rank` | 扫描结果回测排名（按夏普/回撤等指标排序） |
| `screen strength` | 强势股排名（5/20/60 日涨幅加权，steady/breakout/balanced 三预设） |
| `serve` | 启动 Web API 服务器（REST + WebSocket，需 `easy-tdx[web]`） |
| `f10` | 财报三表（新浪：利润表/资产负债表/现金流量表） |
| `finance-info` | 最新财务快照（通达信协议，30+ 项单期指标） |
| `company-info` | F10 公司信息（无板块名列目录 / 有板块名读正文） |
| `fund-flow` | 历史资金流向（CLI 暂未实现，Web API `/fund-flow/history` 可用） |
| `ex kline` | 扩展市场 K 线 |
| `ex quote` | 扩展市场报价 |
| `ex quote-list` | 扩展市场商品列表 |
| `ex tick` | 扩展市场分时 |
| `ex markets` | 列出可用扩展市场 |
| `offline home` | 检测通达信安装目录 |
| `offline daily` | A 股日线（本地 .day 文件） |
| `offline sync-daily` | 从服务端同步单只股票日线到本地 .day 文件 |
| `offline sync-all` | 一键同步沪深全市场日线（扫描本地 .day 文件） |
| `offline min` | 分钟线（本地 .5/.lc1/.lc5 文件） |
| `offline ex-files` | 列出扩展市场可用文件 |
| `offline ex-daily` | 扩展市场日线（期货/港股/外盘） |
| `offline gbbq` | 股本变迁数据 |
| `offline financial` | 历史财务数据 |
| `offline blocks` | 自定义板块数据 |
