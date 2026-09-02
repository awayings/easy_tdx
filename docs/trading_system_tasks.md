# 交易系统任务清单（handoff 文档）

> 新 context 开工指引：先读本文件，再按需读 `docs/trading_system_requirements.md`
> （需求架构 + easy_tdx/tushare 数据能力验证）与 `CLAUDE.md`（环境/踩坑/既有设施）。
> 每个任务自带目标、数据来源、实现要点、验收标准与坑，可独立领取。

## 当前状态（2026-09-01 已落地）

- ✅ 需求报告与数据能力验证：`docs/trading_system_requirements.md`（含 ex 协议实测 §3.4）
- ✅ **特征库 `easy_tdx.features`**：ex 协议 48 系列（汇率/商品主连/上海黄金/中美国债全期限/
  Shibor/R007/OMO/MLF/SLF/准备金率/EFFR/SOFR/PMI/CPI/M2/国际指数），全历史已回填
  （中债 10Y 回溯 2006 年），launchd 23:00 每日增量同步（`~/.easy_tdx/features/`）。
  读取 API：`snapshot()` / `feature_series(id)` / `merge_features(ohlcv_df, ids)`。
- ✅ ex 协议连通性结论：期货/汇率/黄金/宏观全可用；美元指数无品种（见 P1-T3）。

## 当前状态（2026-09-02 本 session 新增）

**核心闭环已打通："念动想买入" → `easy-tdx plan` 一条命令出交易计划**
（市场整体情绪 + 单股技术点位 + 公共舆情 → 风险度 → 止损/止盈事件条件与点位 + 仓位）。

- ✅ **P0-T4 量价模型**：`src/easy_tdx/ta/`（`support_resistance.py`）——
  三角近似筹码分布（筹码峰/获利盘）、前高低点+筹码峰+均线+布林带+20/60 日高低点
  聚合的支撑/阻力位（±0.5% 现价过滤、1.5% 容差聚类）、技术点位状态（趋势/RSI/
  MACD/ATR/量比/突破）。协议无筹码分布，纯本地近似；指标复用 MyTT。
- ✅ **P0-T1/P0-T2 市场情绪**：`src/easy_tdx/sentiment/` + `scripts/market_sentiment.py`——
  market-stat 宽度 + 两市成交额（上证+399107 指数日线 amount）+ MAC 板块资金榜 +
  特征快照 → 日频快照 JSON 落 `~/.easy_tdx/cache/sentiment_*.json` + 0-30 市场风险分
  （涨跌家数/涨停跌停/成交额极值/指数 vs MA20）+ 可选钉钉日报。
- ✅ **公共舆情**（P2-T5 公开可抓源替代）：`src/easy_tdx/public_sentiment.py`——
  东财人气榜（排名/变化）+ 个股新闻搜索（近 3 日条数/利好利空词表情绪分），
  全部免费公开接口、无登录态，失败优雅降级（离线时舆情风险计 0 分）。
- ✅ **P2-T3 交易计划生成器（规则版）**：`src/easy_tdx/planner/` + `easy-tdx plan`
  CLI + `scripts/trading_plan.py` 批量模式（screen signals → 每股计划）。输出：
  风险评分 0-100（波动/技术/舆情/市场四分量可解释）、操作判定（buy/wait/avoid
  三重门：风险 75+ 观望、55+ 等待、下降趋势勿接飞刀、RR1<1.2 等待）、
  止损（支撑破位 ×0.985 与 ATR×2.2 取先触发者 + 事件条件）、止盈多档
  （阻力减半仓→第二阻力清仓 + 移动止盈）、仓位（基础×风险×市场×波动系数，
  单标的上限 20%）、盈亏比。**LLM 编排（P2-T3 原版）留待后续**——当前为
  确定性规则版，信号可解释、可回测。
- ✅ **P1-T5 决策日志**：`src/easy_tdx/journal/`——`~/.easy_tdx/decisions.db`
  （计划全量 JSON + 特征快照 JSON 落库，为 P2-T1 事后回归打底）；
  `easy-tdx plan --save` / `scripts/log_decision.py` / 批量模式 `--save` 三个写入口。
- ✅ 测试：`tests/unit/test_{support_resistance,planner,journal,market_sentiment,
  public_sentiment}.py`（44 项，纯计算注入，无网络）。

**未做（按需后续）**：P0-T5 全市场 .day 扩展；P1-T1~T3 tushare（>200 积分接口
一律不接入，免费 120 分档仅作交叉验证备选）；P1-T4 大海牛群情绪（微信群非公共）；
P2-T1 事后回归管道（需决策日志积累 N 日数据）；P2-T2/T4 图谱与宏观规则；
P2-T5 爬虫；P2-T6 低频源。

## 全局约定（每个任务都适用，详见 CLAUDE.md）

- **数据口径**：vol 单位（协议=股、.day raw=股、reader ×0.01 输出手）；资金流
  TDX tick 重算版与东财 L2 口径不可混用；K 线 bar 时间取 start；净值口径 T-1/T-2。
- **TDX 防封红线**：报价 80 只/批、K 线 700 根/页（--start 翻页）、tick 5 天、
  文件下载 60KB/响应、并发 ≤4-8。定时任务错峰：22:30 track / 22:45 vipdoc / 23:00 features。
- **代码风格**：ruff（line-length 100）、click CLI、库代码进 `src/easy_tdx/`、
  运营脚本进 `scripts/`（`sys.path.insert(0, .../src)`）、`get_* → DataFrame` 客户端风格
  （参照 `src/easy_tdx/sina/client.py`）。测试：`uv run --with pytest python -m pytest ...`。
- **launchd 模式**：plist = [`.venv/bin/python`, 脚本绝对路径(参数)]，日志
  `~/.easy_tdx/cache/*.log`（out/err 同文件），见 CLAUDE.md 管理命令。

---

## P0：市场情绪面板（1-2 周，纯 easy_tdx，零新数据源）

### P0-T1 市场情绪日频快照脚本【M】

- **目标**：收盘后产出 a.0 情绪快照 JSON/CSV 落 `~/.easy_tdx/cache/`，可选钉钉日报。
- **数据来源**（全部 easy_tdx 原生）：
  - `market-stat`（880005/880001/880006）：全市场涨跌家数、涨停/跌停数
  - `quote-list --sort CHANGE_PCT`（分类 A/B/KCB/CYB/BJ/ETF/LOF/HGT/SGT）：涨跌榜
  - `features.snapshot()`：汇率/商品/国债/流动性/宏观/国际指数最新值
  - 两市成交额：指数日线 amount（深市全市场口径用 399107.SZ，勿用 399001.SZ）
- **实现要点**：新脚本 `scripts/market_sentiment.py`；阈值判断（成交量 >2 万亿等）先
  硬编码常量；快照结构 `{date, breadth{...}, board_fund_flow{...}, features{...}}`。
- **验收**：收盘后跑一次，输出 JSON 完整、数值与通达信软件一致（抽查 2-3 项）。
- **坑**：`market-stat` 的 880xxx 部分主机不提供（fallback 用 quote-list 汇总）；
  成交额单位换算（手×100×价）。

### P0-T2 板块资金流日报【S】

- **目标**：b 模块——行业/概念板块资金流向 + 涨跌幅排行日报。
- **数据来源**：MAC `board-list`（HY 行业/GN 概念/FG 风格/DQ 地区）、`board-summary`
  （含板块资金流入流出）、`board-ranking`（涨跌幅排行）、`board-change-ranking`（N 日）。
- **实现要点**：可与 P0-T1 合并为同一脚本；输出板块资金榜 Top N（净流入/净流出各 10）。
- **验收**：输出与通达信板块界面一致（抽查 2 个板块）。
- **坑**：880xxx 板块指数 K 线部分主机不可用；板块资金流只有 MAC 协议有。

### P0-T3 钉钉通知公共模块【S】✅ 已完成（2026-09-01）

- **目标**：把 webhook 发送从 `scripts/track_161129_premium.py`（:108-146）抽成公共
  `src/easy_tdx/notify.py`（或 `webhook.py`），P0-T1/T2 日报直接复用。
- **要点**：保持现有语义——env `CUSTOM_WEBHOOK_URLS` 优先，其次
  `~/.easy_tdx/custom_webhook_urls` 文件；消息含"预警"关键词；无配置时降级
  `osascript display notification`。改完跑 `track_161129_premium.py` 冒烟验证不回归。
- **验收**：旧脚本行为不变 + 新模块被新日报脚本引用。
- **状态**：已抽成 `src/easy_tdx/notify.py`（`webhook_urls()`/`notify()`，配置目录遵循
  `EASY_TDX_CONFIG_DIR` 懒读取），track 脚本已改用并冒烟通过（2026-09-01）；
  首个复用方 `scripts/track_intraday.py`（盘中价格跟踪）已上线，测试
  `tests/unit/test_notify.py`。

### P0-T4 c.2 量价模型：筹码/支撑阻力【M】

- **目标**：日线成交量加权成本分布（筹码密集区）、前高低点支撑/阻力、量价突破，
  输出支撑/阻力位与"爆破筹码区"信号。
- **要点**：先做纯函数库 `src/easy_tdx/ta/chip.py`（或 scripts 内模块）：输入日线 df
  （open/high/low/close/vol），输出筹码成本分布序列与支撑/阻力位列表；再包装成策略
  `strategies/support_resistance.py`（Strategy 子类，参照 strategies/ 现有 17 个文件）
  用 `BacktestEngine` 回测验证；筹码分布协议不支持，**只能本地近似**（~100 行）。
- **验收**：对 62 只股票池回测出绩效（19 项指标），支撑位与肉眼可见的前高低点一致。
- **坑**：`benchmark=` 引擎参数是死代码（手动算基准）；回测成本模型默认 A 股口径
  （ETF 需 `stamp_tax=0.0`）。

### P0-T5 全市场 .day 扩展 + 全市场策略扫描【L】

- **目标**：c.4 从 62 只扩展到全市场。
- **要点**：`bootstrap_vipdoc.py` 分批拉全量 .day（**防封**：单股一文件、并发 ≤4、
  分批错峰，预计 5000+ 文件分多晚）；改 `~/.easy_tdx/vipdoc_universe.txt` 或直接
  `screen scan --universe all`；随后 `screen scan` 全市场跑各策略。
- **验收**：`screen scan --universe all` 输出信号数合理；`screen rank` 排名正常。
- **坑**：screen 纯离线读 `.day`；62→全市场增量大，勿一晚拉完。

---

## P1：外部源 + 本机设施（2-4 周）

### P1-T1 tushare 接入层（免费 120 分档先行）【M】

- **目标**：`src/easy_tdx/tushare/client.py`（`get_* → DataFrame` 风格，仿 sina/cninfo）。
- **首期接口**（免费/120 分，无需充值）：`us_tycr`（美债收益率，与特征库
  `bond_8_AT*` 交叉验证）、`cn_cpi/cn_pmi/cn_gdp`（与 `macro_3_PMI` 等交叉验证）、
  `trade_cal`、`fx_daily`、`hk_daily`。
- **要点**：token 用 `ts.set_token()`，token 存 `~/.easy_tdx/tushare_token`（不进仓库）；
  频率限制 120 分 = 50 次/分（够用）。
- **验收**：`us_tycr` 与特征库 `bond_8_ATY` 数值一致（抽 3 个交易日）。
- **坑**：`trade_cal`/`shibor`/`fx_daily` 积分标注有官方/第三方冲突，以实测为准；
  数据延迟：`daily` 15-17 点入库，21:00 后取当日最稳。

### P1-T2 两融余额接入（需 2000 分档，200 元/年）【S】

- **目标**：a.0 两融余额日频落 `~/.easy_tdx/cache/margin.csv`（或并入特征库体系）。
- **要点**：`margin`（汇总+分交易所：rzye/rqye/rzrqye）+ `margin_detail`（个股明细）；
  9:05 前更新完，可在 23:00 features 同步后并入快照。
- **验收**：与交易所官网公布值一致。
- **坑**：2000 分档 200 次/分、10 万次/天——日频绰绰有余；积分一年有效。

### P1-T3 美元指数 DXY【M】

- **目标**：a.2 的美元指数。ex 协议**无此品种**（已实测确认）。
- **方案 A**（推荐先做）：合成 DXY——用特征库已有 EURUSD/USDJPY（再补 GBPUSD/
  USDCHF/USDCAD 入 manifest）按 ICE 权重加权，回测与真实 DXY 相关性（>0.99 才可用）。
- **方案 B**：tushare `fx_obasic`（2000 分）查 `FX_BASKET=USDOLLAR`，`fx_daily` 取日线。
- **验收**：合成序列与公开 DXY 数据相关性达标，或 B 方案直连成功。

### P1-T4 大海牛群情绪特征【M】

- **目标**：c.3 的微信群情绪源。复用本机 skills：`dahainiu`（群话题热度/发言量 TOP）、
  `wechat-reader`（微信聊天库检索）。
- **要点**：每日跑 dahainiu 分析 → 话题热度 → 映射情绪分特征（如"话题热度/负面词频"）
  落 `~/.easy_tdx/features/`（新家族 `sentiment.csv` 或独立 JSON）；特征按日期可被
  `merge_features` 使用。
- **验收**：日频情绪分序列可生成；与行情日期的对齐无未来函数（当天 22:00 后取当天）。
- **坑**：微信群数据只在本地（chatlog 库），换机需迁移。

### P1-T5 f 决策日志库 decisions.db【M】

- **目标**：每笔交易计划落库（标的/理由/止损止盈/可信度/**当时特征快照 JSON**）。
- **要点**：SQLite 仿 `web/strategy_store.py`（`~/.easy_tdx/decisions.db`）；schema：
  id/date/symbol/plan_json(理由+止损+止盈)/snapshot_json(特征快照)/created_at。
  提供 `src/easy_tdx/journal/` 读写模块 + `scripts/log_decision.py`。
- **验收**：写入读取往返无损；快照 JSON 与 `features.snapshot()` 输出一致。
- **坑**：快照必须落库（事后回归的唯一数据基础）。

---

## P2：LLM 决策层与复盘闭环（持续迭代）

### P2-T1 事后回归管道【L】

- **目标**：f 模块核心——决策 N 日后收益 → 胜率/盈亏比统计 → 可信度反哺。
- **要点**：launchd/脚本定时（如每周）扫描 decisions.db，取当日特征快照中的标的价格
  vs N 日后实际价格（用 .day/特征库），统计按策略/理由标签分组的胜率、盈亏比、
  平均收益；输出回归报告 JSON。参照现成闭环：`screen rank`（信号→全历史回测）。
- **验收**：手工写入 3 条历史决策，回归结果与手算一致。
- **坑**：无 walk-forward 设施，全部自建；`performance["total_trades"]`=平仓轮次
  与 `len(result.trades)` 成交笔数勿混。

### P2-T2 知识图谱（板块上下游）【L】

- **目标**：b 模块高级功能——板块/个股上下游关联标签。
- **要点**：LLM 离线抽取（Claude API）板块产业链上下游关系 → 本地 JSON/SQLite，
  人工审核入库；下游查询：给定板块返回上下游板块代码（对接 board-list HY/GN 代码）。
- **验收**：10 个核心板块的关系图人工抽查通过。

### P2-T3 LLM 交易计划编排器【L】

- **目标**：e 模块——宏观指引→板块轮动→标的候选→微观出入点的计划生成。
- **要点**：`scripts/trading_plan.py`：输入 = `features.snapshot()` + 市场情绪快照
  （P0-T1）+ 板块资金榜（P0-T2）+ 策略信号（screen scan 输出）+ 决策日志回归统计
  （P2-T1）；LLM 输出模板化：理由/可信度(引用回归统计)/止损点+条件/止盈点+条件/
  买入时机；结果落 decisions.db（P1-T5）+ 钉钉推送。
- **验收**：每天收盘后自动产出结构化计划，字段齐全可入库。
- **坑**：LLM 只做编排与叙事，信号必须来自可回测的规则（见 P2-T4）。

### P2-T4 c.1 宏观策略规则化【L】

- **目标**：卢麒元（中美地缘/通胀传导）、留得超（中必赢）模型 → **规则+信号**（可回测），
  板块切换提示（如科技切石油化工）。
- **要点**：模型变量映射到特征库已有系列（国债利差、汇率、商品、流动性、PMI）；
  规则写成 Strategy/信号脚本走回测验证后再进 LLM 上下文；LLM 不做黑箱判断。
- **验收**：板块切换规则在历史区间回测出有意义的分组收益差异。

### P2-T5 舆情爬虫（微信指数/抖音）【L，最后做】

- **目标**：c.3 公开平台情绪——共识形成/发酵/爆破期判断。
- **要点**：**先做合规评估**；微信指数无公开 API（需 token/逆向，风险高）；抖音
  评论区爬虫需账号与风控处理。优先做微信群（已有 P1-T4）+ 公开可抓源（东方财富
  股吧、雪球热帖）作为替代。
- **验收**：任一来源的日频关键词热度序列落地。

### P2-T6 低频外部源补全（可选，按需）【S-M】

- 日本国债（a.1）：英为财情爬取或"美债+中美利差"代理（ex 无日债）。
- 美联储资产负债表（a.3）：FRED API（免费；EFFR/FFR/SOFR 已由特征库覆盖）。
- 房产（a.1）：统计局/第三方月度数据，手工录入 CSV（优先级最低）。

---

## 建议执行顺序

```
P0-T1/T2/T3（情绪面板+日报，第一周可见成果）
→ P0-T4（量价模型）与 P0-T5（全市场扫描）并行
→ P1-T5 + P2-T1（决策日志+回归，闭环骨架）
→ P1-T1（tushare 免费档交叉验证）→ P1-T2/T3（两融/美元指数，按需充值）
→ P1-T4（大海牛情绪）
→ P2-T2/T4/T3（图谱→宏观规则→LLM 编排）
→ P2-T5/T6（舆情/低频源，最后）
```

每个阶段都有独立可用产出：情绪面板日报 → 量价信号+决策闭环 → 完整 LLM 交易计划。
