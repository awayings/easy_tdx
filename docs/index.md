# easy-tdx 文档索引

本索引是文档的目录目录：**新增或删除文档必须同步更新本文件与 README 文档导航**（见 [development.md](./development.md) 的文档规范）。每份文档 ≤500 行，超限拆分；教程（how-to）与参考（速查）分开成文。

## 开始

- [README](../README.md) — 项目门面：功能总览、30 秒上手、安装、文档导航（ReadTheDocs 落地页即其包含）

## CLI 参考

- [cli.md](./cli.md) — 全部命令速查表 + 按域导航
- [cli-market.md](./cli-market.md) — 行情数据：报价 / K线 / 分时 / 板块 / 资金 / 中金所 / 扩展市场
- [cli-finance.md](./cli-finance.md) — 财务与公告：巨潮公告检索 / 财报三表 / 通达信 F10
- [cli-indicators.md](./cli-indicators.md) — 技术指标与公式：34 指标 / 通达信公式 / 捉妖 / 乖离率
- [cli-backtest.md](./cli-backtest.md) — 回测与寻优：backtest / optimize / portfolio / run-all（含参数表）
- [cli-screen.md](./cli-screen.md) — 选股扫描：全市场信号扫描 / 强势股排名
- [chanlun.md](./chanlun.md) — 缠论分析（CLI + Python，含完整输出示例）

## 本地数据

- [data-local.md](./data-local.md) — K 线仓库（DuckDB）与 vipdoc 离线读写（CLI + Python 合一操作手册）

## Web

- [web-api.md](./web-api.md) — REST + WebSocket + SSE 端点与示例、帧规范
- [web-ui.md](./web-ui.md) — 行情终端与回测工作台零代码操作手册
- [packaging.md](./packaging.md) — Windows EXE 打包（PyInstaller）与分发

## Python API

- [python-api.md](./python-api.md) — 教程：连接管理 / MAC 协议 / 统一客户端 / 离线 / 缠论 / 公告 / 财报 / 实时轮询
- [api_reference.md](./api_reference.md) — 参考：TdxClient 标准协议方法速查 + MAC 客户端方法表
- [field_mapping.md](./field_mapping.md) — 参考：数据模型字段 ↔ 中文含义 ↔ 类型 + 枚举（标准 + MAC）

## 回测与量化

- [backtest_usage.md](./backtest_usage.md) — 回测引擎使用手册（策略 / 配置 / 结果）
- [backtest-examples.md](./backtest-examples.md) — 完整示例集 + 注意事项
- [quantitative-guide.md](./quantitative-guide.md) — 因子引擎与组合管理（基础）
- [quantitative-advanced.md](./quantitative-advanced.md) — 滑点模型 / 执行仿真 / 归因分析 / 完整工作流

## 指标深度

- [indicator-zhuoyao.md](./indicator-zhuoyao.md) — 捉妖大师（ZHUOYAO）多周期 ROC 共振详解
- [indicator-bias-signal.md](./indicator-bias-signal.md) — 30 日乖离率信号（BIAS_SIGNAL）详解

## 开发者

- [architecture.md](./architecture.md) — 七层架构图 / 源码树 / 分层要点
- [development.md](./development.md) — 环境初始化 / 测试 / CI / 发布流程 / 文档规范

## 协议与逆向

- [protocol-reverse-engineering.md](./protocol-reverse-engineering.md) — 通达信二进制协议逆向过程
- [protocol-unknown-fields.md](./protocol-unknown-fields.md) — 未知字段研究日志（活文档）

## 历史归档（已实施 / 规划完成，供追溯）

- [board-overview-design.md](./board-overview-design.md) — 行业/概念总览页设计稿（已上线）
- [hotspot-rolling-design.md](./hotspot-rolling-design.md) — 市场热点滚动页设计稿（已上线）
- [market-insights-roadmap.md](./market-insights-roadmap.md) — 盘面洞察功能路线图（已收尾）
- [upgrade-plan-2026H2.md](./upgrade-plan-2026H2.md) — 2026 H2 升级计划（全部阶段完成）
- [superpowers/](superpowers/) — 回测/因子/组合引擎的计划与设计存档（v1.11–v1.15 时代，非现行文档）

## HTML 资产（仅 GitHub 可交互，ReadTheDocs 不发布）

- [architecture.html](./architecture.html) — 38 模块交互式架构图（可缩放平移、导出 PNG）
- [回测系统完全上手手册.html](./回测系统完全上手手册.html) — 零基础图文回测手册（13 章）

---

```{toctree}
:maxdepth: 1
:caption: 文档目录

readme
cli
cli-market
cli-finance
cli-indicators
cli-backtest
cli-screen
chanlun
data-local
web-api
web-ui
python-api
api_reference
field_mapping
backtest_usage
backtest-examples
quantitative-guide
quantitative-advanced
indicator-zhuoyao
indicator-bias-signal
architecture
development
packaging
protocol-reverse-engineering
protocol-unknown-fields
board-overview-design
hotspot-rolling-design
market-insights-roadmap
upgrade-plan-2026H2
superpowers/plans/2026-06-09-backtest-engine
superpowers/plans/2026-06-12-v1.11.0-factor-engine
superpowers/plans/2026-06-12-v1.12.0-factor-analysis
superpowers/plans/2026-06-12-v1.13.0-portfolio
superpowers/plans/2026-06-12-v1.14.0-slippage-execution
superpowers/plans/2026-06-12-v1.15.0-attribution
superpowers/specs/2026-06-09-backtest-engine-design
superpowers/specs/2026-06-12-advanced-backtest-design
superpowers/specs/2026-06-12-quantitative-factor-engine-design
```
