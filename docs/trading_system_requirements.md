# 交易系统架构归纳与 easy_tdx 落地可行性报告

> 2026-09-01，基于 easy_tdx 代码库实测探查（CLI/协议/策略/回测/外部数据三路探查）+
> tushare Pro 官方文档核查 + **ex 协议连通性实测（见 §3.4，全部关键品种已验证）**编写。
> 结论先行：**架构成立，数据面 easy_tdx 原生覆盖大幅高于预期（约 85%+）**——
> ex 协议实测通过汇率/上海黄金/外盘期货（COMEX/NYMEX/CBOT）/国内期货/港股/美股/
> 中国国债全期限/美国国债/联邦基金利率/R007/MLF/公开市场操作利率，tushare 收缩为
> **两融 + 美元指数 + 交叉验证**三个用途；仅剩日债、美联储资产负债表、房产、舆情需外部。
> 分三阶段落地，P0 立即可开工。

---

## 1. 架构归纳（五层）

```
┌─────────────────────────────────────────────────────────────────┐
│ L4 记录与复盘层  ← f)                                          │
│   决策日志 SQLite（标的/理由/止损止盈/可信度/数据快照）            │
│   事后回归任务：信号 → N日后收益 → 可信度统计 → 反哺 L3           │
├─────────────────────────────────────────────────────────────────┤
│ L3 决策层       ← e)  LLM 编排器（外部 Claude API）             │
│   宏观指引(a) → 板块轮动(b) → 标的候选(c) → 微观出入点(c.2)       │
│   输出交易计划：理由 / 可信度(历史回归) / 止损点+条件 /           │
│   止盈点+条件 / 买入时机；风控约束（仓位上限、回撤熔断）            │
├─────────────────────────────────────────────────────────────────┤
│ L2 策略与信号层 ← c)                                            │
│   c.1 宏观策略(板块切换规则化)  c.2 量价模型(支撑/筹码/爆破区)      │
│   c.3 舆情情绪指标(共识周期)    c.4 经典指标(MACD/RSI/KDJ…)       │
│   screen scan / rank / strength + 回测 + 网格寻优               │
├─────────────────────────────────────────────────────────────────┤
│ L1 特征与指标层：统一特征库（日频快照，parquet/csv）               │
│   市场情绪面 | 板块资金 | 宏观跨市场 | 舆情特征 | 量价指标          │
├─────────────────────────────────────────────────────────────────┤
│ L0 数据层                                                       │
│   TDX classic + MAC + ex（easy_tdx 原生）                       │
│   tushare（2000 分档：两融/资金流/期货/指数；免费档：美债/宏观）    │
│   官网/替代源：中债官网、央行、FRED、yfinance、英为财情            │
│   舆情源：微信本地库（dahainiu/wechat-reader skills）+ 爬虫        │
└─────────────────────────────────────────────────────────────────┘
```

数据流：L0 定时采集（launchd 错峰）→ L1 特征库 → L2 扫描/回测 → L3 LLM 计划 →
钉钉推送 → L4 记录 → 定期回归更新 L3 的可信度参数。

### 对原方案的完善建议

1. **a.0 增加"市场宽度"骨架**：涨跌家数、涨停/跌停数、两市成交额——这是情绪面的
   基础指标，easy_tdx 原生支持（`market-stat` + `quote-list`），且"成交量 >2万亿"这类
   阈值判断直接建立在其上。
2. **风控并入 L3**：交易计划必须带硬条件（单标的仓位上限、总回撤熔断），否则
   f 模块的回归没有可比口径。
3. **f 模块 = 决策日志 + 定期回归**：`e)` 中"可信度（根据历史数据回归）"的唯一量化
   来源就是 f。每笔计划落库（含当时特征快照），N 日后自动回算收益，形成
   "可信度 = 历史胜率/盈亏比"的统计闭环，再反哺 LLM prompt。
4. **数据治理提前约定**：时间戳口径（K线 `--bar-time start/end`）、成交量单位
   （协议=股、`.day` raw=股、reader 输出手、tushare `index_daily` amount 单位=千元）、
   净值口径（T-1/T-2，见 CLAUDE.md）——跨源混用是这类系统最大的隐性 bug 源。
5. **舆情分两源**：微信群（本机已有 skills，结构化、合规）与公开平台（微信指数/
   抖音，需爬虫，**合规风险先行评估**）。后者建议最后做。

---

## 2. 数据需求清单

| 模块 | 数据项 | 粒度 | 频率 | 优先级 |
|---|---|---|---|---|
| a.0 | 两市成交额、涨跌家数、涨停/跌停数 | 全市场 | 盘后 | P0（easy_tdx 原生） |
| a.0 | 主力资金流入流出（个股/板块） | 个股/板块 | 盘后 | P0（easy_tdx MAC 原生） |
| a.0 | 两融余额 | 全市场/个股 | 盘后 | P0（tushare `margin` 2000分） |
| a.0 | 历史年/月/季线及突破点 | 个股/指数 | 月末 | P0 |
| a.0 | 国家队 ETF 流入流出 | ETF | 盘后 | P1 |
| a.1 | 中/美/日国债利率 | 日 | 盘后 | P1（美=tushare 免费；中/日=官网） |
| a.1 | 黄金/石油/金属价格+成交量 | 日 | 盘后 | P1（easy_tdx ex + tushare `fut_daily`） |
| a.1 | 房产综合成交量/价格 | 月 | 月末 | P2（统计局/第三方） |
| a.2 | 美元指数、美日、美中汇率 | 日 | 盘后 | P1（easy_tdx ex + tushare `fx_*`） |
| a.3 | 美联储/央行/HK 流动性指标 | 周/月 | 定时 | P2（Shibor/HIBOR=tushare；DR007/MLF=央行官网；美联储=FRED） |
| a.b | 大海牛群情绪（话题热度/发言量） | 日 | 盘后 | P1（本机 skills） |
| a.c | 公开平台评论区情绪（反身性） | 日 | 盘后 | P2（爬虫） |
| b | 行业板块资金流/涨跌幅排行 | 板块 | 盘后 | P0（easy_tdx MAC 原生；东财口径 tushare 6000分 作备选） |
| b | 板块指数日线 | 板块 | 盘后 | P0 |
| b | 知识图谱上下游关联标签 | 板块/个股 | 静态+更新 | P2（LLM 自建） |
| c.2 | 筹码密集区/支撑阻力/爆破区 | 个股 | 盘后 | P1（本地计算） |
| c.3 | 微信指数/抖音评论关键词热度 | 关键词 | 盘后 | P2（爬虫） |
| c.4 | MACD/RSI/KDJ/BOLL… 指标信号 | 全市场 | 盘后 | P0（原生） |
| e | LLM 上下文 = 以上全部特征 | — | 盘后一次 | P2 |
| f | 每笔决策日志（计划+特征快照） | 每决策 | 实时 | P1 |
| f | 决策事后回归统计 | 每决策 | N日后 | P2 |

---

## 3. 数据支持验证

### 3.1 总览

| 需求 | 状态 | 落点 |
|---|---|---|
| 成交量/涨跌幅/K线全周期（日/周/月/季/年/分钟） | ✅ 原生 | `kline --period DAILY/WEEKLY/MONTHLY/SEASON/YEAR/1MIN…60MIN`，协议 800 根/页自动翻页；离线 `.day` 全历史 |
| 全市场涨跌统计/涨跌榜 | ✅ 原生 | `market-stat`（880005/880001/880006）；`quote-list --sort CHANGE_PCT`（A/B/KCB/CYB/BJ/ETF/LOF/HGT/SGT 分类）；tushare `limit_list_d`（5000分，涨停炸板连板）作增强备选 |
| 个股主力资金流 | ✅ 原生 | MAC `capital-flow`（0x1218 主力/散户净流入）；classic `get_fund_flow` 由 tick 重算（见坑）；tushare `moneyflow`（2000分）作交叉验证 |
| 板块（行业/概念/风格/地区）+ 板块资金流+排行 | ✅ 原生 | MAC `board-list/members/belong/summary/ranking/change-ranking`；board-summary 含板块资金流入流出；东财口径备选 tushare `moneyflow_ind_dc`（6000分） |
| 两融余额 | ⚠️ tushare | `margin`（汇总+分交易所）**2000 分**；`margin_detail` 个股明细 2000 分 |
| 期货/外盘/商品/黄金 | ✅ **实测通过** | easy_tdx `ex`：COMEX 黄金主连 GC00W、NYMEX 原油 CL00W/天然气、CBOT 美债期货 TY00W、上期所主连（AUL8/CUL8/AGL8）、上海黄金 Au99.99 —— 行情+K线均为真实最新数据（详见 §3.4）；tushare `fut_daily` 仅作备份 |
| 汇率（美日/美中/欧元等） | ✅ **实测通过** | easy_tdx `ex` BASIC_FX 16 对（USDCNY/USDCNH/USDJPY/EURUSD/GBPUSD…）+ CROSS_FX 125 交叉，K线+实时报价可用；**美元指数 DXY 无直接品种** → tushare `fx_obasic`（2000分，FX_BASKET=USDOLLAR）或合成 DXY |
| 国债利率（中美） | ✅ **实测通过** | easy_tdx `ex` MACRO_INDICATOR：中国国债即期~10Y（5_CNM*/5_CNT*）、国开债（5_CNDT*）、美国国债各期限（8_AT*，10Y=8_ATY）均为真实日频数据；日本国债无 → 英为财情 |
| 宏观 CPI/PMI/GDP | ✅ 双源 | easy_tdx `ex` MACRO_INDICATOR（3_PMI 实测 49.2、8_ACPI 实测）；tushare `cn_*` 免费作交叉验证 |
| 央行流动性（R007/MLF/SLF/OMO/Shibor） | ✅ **实测通过** | easy_tdx `ex` MACRO_INDICATOR：R007 回购（9_R007 实测 1.43%）、MLF（9_MLF1Y 实测 2.0%）、公开市场逆回购利率（9_OMOR*）、SLF（9_SLF*）、Shibor 全期限（5_SHIBOR/5_SHR*/5_SHS*）、M0/M1/M2 |
| 美联储利率（EFFR/FFR/SOFR） | ✅ **实测通过** | easy_tdx `ex` MACRO_INDICATOR：8_EFFR 实测 3.63%、8_FFRLL/8_FFRUL 上下限、8_SOFR；**美联储资产负债表仍无** → FRED |
| 房产数据 | ❌ 无 | 统计局/第三方（月度低频） |
| 港股 | ✅ 互补 | easy_tdx ex HK；tushare `hk_daily` 免费 |
| 筹码分布 | ❌ 无协议 | 本地用日线成交加权近似（自研几十行） |
| 支撑/阻力位 | ✅ 可算 | 本地计算（前高低点/量价集中区），接入策略框架 |
| 策略扫描/排名/动量 | ✅ 原生 | `screen scan/rank/strength`（纯离线 `.day`，现池 62 只） |
| 经典技术指标 | ✅ 原生 | MyTT 40+ 指标（MACD/KDJ/RSI/BOLL/CCI/OBV/MTM/TRIX/DMI/MFI/BRAR…）；20 内置+17 脚本策略；另有缠论 `chanlun`、因子引擎 `factor` |
| 回测/参数寻优 | ✅ 原生 | BacktestEngine（19 项绩效指标、SL/TP、可插拔执行模型、A股成本模型、自定义列注入）；ParamGridOptimizer（≤200 格、参数约束、热力图） |
| 财务/公告 | ✅ 原生 | `f10`（新浪三表）、`finance-info`（TDX 快照 30+ 项）、`announcement`（巨潮） |
| 微信指数/抖音舆情 | ❌ 无 | 爬虫（合规先行） |
| 大海牛群情绪 | ✅ 本机设施 | 非 easy_tdx：`dahainiu`/`wechat-reader`/`weflow-chat-export` skills（本地聊天库） |
| LLM 决策层/知识图谱 | ❌ 无 | 需自建；easy_tdx 输出 JSON 定位即"喂 AI" |
| 决策记录/事后回归 | ⚠️ 部分 | 信号 JSON + `strategies.db`（策略+业绩快照）有先例；逐笔决策日志与 walk-forward/事件研究**无**，需自建 |

### 3.2 easy_tdx 关键细节与坑（实测结论）

- **成交量单位**：协议 K 线 vol=股；`.day` raw=股；本地 reader ×0.01 输出手。
  算全市场成交额时注意换算（手×100×价）；tushare `index_daily` 的 amount 单位
  是**千元**，深市全市场口径用 399107.SZ（399001.SZ 只含成分股）。
- **资金流口径坑**：classic `get_fund_flow` 是 **L1 tick 重算**（tick 协议最多 5 天），
  且 `main_net_inflow` 与东财 L2 口径**不可直接对比**（issue #52/#55）——只用于
  趋势方向，勿跨源混用数值。tushare `moneyflow`（标准口径）与 `moneyflow_dc`
  （东财口径）同样不可混用。CLI `fund-flow` 目前是 stub（`暂未实现`）。
- **板块指数 880xxx**：可走 classic `get_index_bars`，但部分主机不提供（`client.py:581`
  注释）；稳健路径是 MAC `board-*` 系列 + `board-ranking`。
- **ex 协议（期货/外盘/汇率/黄金）**：代码完整（`ex/client.py`、`mac/enums.py:183-242`
  `ExMarket`、CLI `ex` 组、REST `/api/v1/ex/*`），**2026-09-01 已实测通过**，
  全部关键品种可用（详见 §3.4）；yfinance 不再需要。
- **screen 现状**：纯离线读 `~/new_tdx/vipdoc`，现池 62 只；扩到全市场需先
  `bootstrap_vipdoc.py` 分批直下全量 `.day`（单股一文件、并发 ≤4-8、错峰——TDX 防封红线）。
- **无 walk-forward / 无交易日志**：`benchmark=` 引擎参数是死代码（已确认）；最接近
  "决策复盘"的现成闭环是 `screen rank`（信号→全历史回测排名）。
- **自定义数据注入范式已存在**：`scripts/backtest_161129_premium.py` 演示了给 df 加
  自定义列（premium）→ 策略内 `self.data.premium[i]` 读取 → 回测+网格寻优——
  这是"情绪因子→策略"的标准接法，直接复用。
- **钉钉通知**：`scripts/track_161129_premium.py` 内自包含（env `CUSTOM_WEBHOOK_URLS`
  或 `~/.easy_tdx/custom_webhook_urls`），建议抽成公共 notifier 模块复用。

### 3.3 tushare 接口明细与积分机制（官方文档核查）

#### 需求 → 接口映射

| 数据需求 | 接口 | 最低积分 | 备注 |
|---|---|---|---|
| 两融余额（汇总+分交易所） | `margin` | **2000** | 字段 rzye/rqye/rzrqye；9:05 前更新完 |
| 个股两融明细 | `margin_detail` | **2000** | 券商报送口径 |
| 个股资金流（标准口径） | `moneyflow` | **2000** | `net_mf_amount`（万元）主力净流入 |
| 个股资金流（东财口径） | `moneyflow_dc` | ≥5000 | 2023-09-11 起，与标准口径数值不一致 |
| 板块资金流（东财） | `moneyflow_ind_dc` | 6000（试用 120 分每日 2 次） | 板块代码带 `.DC` 后缀 |
| 北向资金 | `moneyflow_hsgt` | 500 | 可作主力代理指标 |
| 美债收益率曲线 | `us_tycr` | **120（免费可用）** | y2/y10 字段直接取 2Y/10Y |
| 中债国债收益率曲线 | `yc_cb` | **独立权限**（积分无用） | 需联系管理员开通；替代：中债官网/中国货币网 |
| Shibor | `shibor` | 2000（标注有冲突） | 隔夜~1年 9 期限 |
| HIBOR/Libor/LPR | `hibor`/`libor`/`shibor_lpr` | 120 | 免费档 |
| 汇率日线 | `fx_daily` | 免费（标注冲突） | FXCM 源，`.FXCM` 后缀 |
| 美元指数/货币对清单 | `fx_obasic` | **2000** | `FX_BASKET=USDOLLAR` 美元指数、`XAUUSD` 黄金、USDJPY 等 |
| 国内期货日线 | `fut_daily` | **2000** | SHFE/INE/DCE/CZCE/CFFEX/GFEX 六所 |
| 期货主力/连续映射 | `fut_mapping` | 2000 | 配合 `fut_basic` |
| 外盘期货（COMEX/NYMEX/CBOT） | **无接口** | — | 仅 `fx_daily` 的 FXCM CFD 代理（黄金等）；兜底 yfinance 或 easy_tdx ex |
| 日本国债 | **无接口** | — | 英为财情等外部源 |
| DR007/回购利率 | **无接口** | — | `repo_daily` 是逆回购行情非利率；用央行官网 |
| 央行公开市场操作（MLF） | **无接口** | — | 央行官网 |
| 美联储资产负债表/联邦基金利率 | **无接口** | — | FRED/yfinance |
| 房产/房价 | **无接口** | — | 统计局/第三方 |
| 宏观 CPI/PMI/GDP/PPI/M2 | `cn_cpi`/`cn_pmi`/`cn_gdp`/`cn_ppi`/`cn_m` | **免费** | 月度/季度 |
| 指数日线 | `index_daily` | 2000 | `amount` 单位千元；9:00/19:00 更新 |
| 指数估值（PE/PB/换手） | `index_dailybasic` | 2000 | 无成交额字段 |
| 涨跌停统计 | `limit_list`(2000) / `limit_list_d`(5000，含炸板/连板/封单) / `limit_list_ths`(8000，含涨停原因) | 2000-8000 | `stk_limit`（2000）低配替代 |
| 港股日线 | `hk_daily` | 免费 | 港股财报/分钟单独付费 |
| 交易日历 | `trade_cal` | 标注冲突（官方 2000 vs 第三方免费，需实测） | 覆盖全部交易所 |
| 统一复权行情 | `pro_bar` | 2000 | 底层 `daily`+`adj_factor` |

#### 积分与频率机制

- **积分是门槛不是消耗品**：达标即可调，不扣减；一年有效期。注册送 100 分 +
  完善资料 +20 = **120 分免费档**；学生可免费申请更高（初始 1000 分）；
  **200 元/年 = 2000 分档**（覆盖本系统全部核心需求）；500 元/年 = 5000 分。
- **频次**：120 分 = 50 次/分、8000 次/天；2000 分 = 200 次/分、10 万次/天/API——
  对本系统日频快照绰绰有余。
- **Token 接入**：tushare.pro 注册 → 个人主页取 token → `ts.set_token(...)`；
  `pip install tushare`。
- **数据延迟**：`daily` 15-17 点入库（21:00 后最稳妥）；`margin` 9:05 前；
  `index_daily` 9:00/19:00。**无盘中实时数据**——盘中需求全部由 easy_tdx 承担。
- **分钟数据单独付费**（历史 2000 元/年）——本系统不需要（TDX 原生分钟/分时）。

#### 与 easy_tdx 的互补结论（ex 实测后修正）

- ex 实测通过后，easy_tdx 已覆盖：外盘/国内期货、汇率、黄金、中美国债、R007/MLF/
  OMO/SLF/Shibor、联邦基金利率、宏观 CPI/PMI/GDP——tushare 的宏观类接口基本冗余。
- tushare 剩余不可替代的用途仅三个：**两融 `margin`（2000 分）**、**美元指数
  `fx_obasic`（2000 分）**、**资金流东财口径交叉验证 `moneyflow`（2000 分，可选）**。
- 板块资金流用 easy_tdx MAC 免费即可，不必上 tushare 6000 分的 `moneyflow_ind_dc`。
- **档位建议**：先免费 120 分起步（美债 `us_tycr` 等已够交叉验证）；需要两融时
  再上 2000 分（200 元/年）。

---

### 3.4 ex 协议连通性实测报告（2026-09-01 本机实测）

测试方式：`easy-tdx ex markets / quote-list / kline / quote`，本机 MAC-ex 服务器
（端口 7727），全部返回**真实最新数据**（非空壳）。

#### 实测矩阵

| 市场 | 品种 | 结果 | 数据样例（2026-08 末） |
|---|---|---|---|
| BASIC_FX 基本汇率 | USDCNY/USDCNH/USDJPY/EURUSD/GBPUSD/USDHKD 等 16 对 | ✅ K线+实时报价 | USDCNY 6.7227、USDJPY 159.3 |
| CROSS_FX 交叉汇率 | 125 交叉对 | ✅ | — |
| SH_GOLD 上海黄金 | Au99.99/Au(T+D)/Ag(T+D) 等 15 品种 | ✅ K线含量额 | Au99.99 1000 元/克 |
| COMEX_FUTURES | GC00W 黄金主连、HG00W 铜主连（126 合约） | ✅ | GC00W 4647.8 美元/盎司 |
| NYMEX_FUTURES | CL00W 原油主连、NG00W 天然气（149 合约） | ✅ | CL00W 81.91 |
| CBOT_FUTURES | TY00W 美10年国债期货主连、US00W 30年、TU00W 2年、美玉米/大豆（175 合约） | ✅ | TY00W 108.75 |
| SH_FUTURES 上期所 | AUL8 黄金主连、CUL8 铜主连、AGL8 白银主连（376 合约） | ✅ | CUL8 108750 元/吨 |
| HK_MAIN_BOARD | 00700 腾讯 | ✅ | 445.4 HKD |
| US_STOCK 美股 | 4000+ 只（多页） | ✅ | — |
| INTL_INDEX 国际指数 | 道指 A_DJI、纳指 A_IXIC、标普 A_SPX、DAX、日经225期指 NK0Y、富时A50期指 CNY0（66 个） | ✅ | — |
| MACRO_INDICATOR 宏观 | **214 项**（见下） | ✅ | — |
| TREASURY_VALUATION 国债预发行 | — | ❌ 空列表 | — |
| CFFEX_FUTURES / FUTURES_INDEX / MAIN_FUTURES_CONTRACT | — | ⚠️ quote-list 空（中金所未深测；各所主力合约用 L8 代码已可替代） | — |
| 美元指数 DXY | — | ❌ 无直接品种（美股 4000+ 条、国际指数 66 条均无 DINIW/DXY） | → tushare `fx_obasic` 或合成 DXY |

#### MACRO_INDICATOR 214 项宏观指标（最重磅发现）

- **中国国债收益率（日频）**：即期/1M-9M（`5_CNM0Y`~`5_CNM9M`，实测 0.96%）、
  1Y-10Y（`5_CNTY` 10Y 实测 1.6988%）、国开债 1Y-10Y（`5_CNDTOY`~`5_CNDT7Y`）
  —— **a.1 中国国债全期限 ✅，tushare `yc_cb`（需独立权限）不需要了**
- **资金市场利率（日频）**：Shibor 全期限（`5_SHIBOR` O/N、`5_SHR1W/2W`、
  `5_SHS1M-9M`、`5_SHT1Y`）、银行间回购 R001~R1Y（`9_R007` 实测 1.43%）、
  同业拆借 IBO（`9_IBO001`~`9_IBO1Y`）、国库定存（`9_GKDC*`）—— **a.3 中国流动性 ✅**
- **央行政策利率**：MLF（`9_MLF1Y/3M/6M` 实测 2.0% 月频）、公开市场逆回购操作利率
  （`9_OMOR07`~`9_OMOR6M`）、SLF（`9_SLF1D/7D/1M`）、存款准备金率（`5_DDR`）
  —— **tushare 明确无接口的 MLF/OMO 这里全有**
- **美国**：国债各期限（`8_ATY` 10Y 实测 4.73% 日频）、联邦基金有效利率（`8_EFFR`
  实测 3.63% 周频）、FFR 上下限（`8_FFRLL/8_FFRUL`）、SOFR/OBFR、CPI（`8_ACPI` 月频）、
  PMI（`8_APMI`）、GDP（`8_AAGDP`）、非农（`8_NONAG`）、密歇根消费者信心
  （`8_UMCSEN`）、国债总额（`8_USDEBT`）—— **a.3 美联储利率 ✅（资产负债表除外）**
- **其他**：M0/M1/M2、中国 CPI/PPI/PMI（`3_PMI` 实测 49.2）、BDI、黄金/外汇储备、
  美元人民币中间价（`5_RMBUS`）、比特币（`6_BTCUSD`）

#### 协议行为与限制

- **K 线 700 根/页**，`--start` 翻页可回溯（实测 USDCNY 翻到 2023-06 之前），全历史可拉。
- 汇率 K 线 vol=0（外汇无成交量字段，正常）；宏观指标 OHLC 同值（单值序列，取 close）。
- 数据含当日（实测 2026-09-01 有 bar），盘中 `quote` 实时报价可用。
- 主力合约代码约定：外盘 `00W`=主连 / `00Y`=连续；国内 `L8`=主连 / `L7`=次连 /
  `L9`=加权 / `YYMM`=单月。

## 4. 需求报告（Gap 分析）

| 缺口 | 现状 | 方案 |
|---|---|---|
| 两融余额 | easy_tdx 无 | tushare `margin`/`margin_detail`（2000 分），1 个函数 |
| 美元指数 DXY | ex 协议无直接品种 | tushare `fx_obasic`（2000 分）或合成 DXY（EURUSD+USDJPY+GBPUSD 等 ex 汇率加权） |
| 中债收益率 | **ex 协议已实测通过**（5_CNT* 全期限） | 无需外部源 ✅ |
| 日本国债 | tushare/ex 均无 | 英为财情；或降级为"美债+中美利差"代理 |
| 美联储资产负债表 | tushare/ex 均无（利率有） | FRED API（免费）；联邦基金利率用 ex `8_EFFR` ✅ |
| DR007/MLF 操作 | **ex 协议已实测通过**（9_R007/9_MLF1Y/9_OMOR*） | 无需外部源 ✅ |
| 房产 | 无 | 月度低频，手工录入 CSV 或第三方 API，优先级最低 |
| 外盘期货 | **ex 协议已实测通过**（GC00W/CL00W/TY00W） | yfinance 不需要 ✅ |
| 筹码/支撑阻力 | 无协议 | 自研：日线成交量加权成本分布 + 前高低点（~100 行） |
| 舆情（微信指数/抖音） | 无 | 爬虫，合规评估先行；微信群情绪先用本机 skills 顶上 |
| 知识图谱 | 无 | LLM 离线抽取板块上下游 → 本地 JSON/SQLite，人工审核入库 |
| LLM 决策层 | 无 | Claude API + 特征库 JSON 组装 prompt；计划模板化（理由/可信度/止损止盈/时机） |
| 决策记录+回归 | 无 | 新建 decisions.db（仿 strategies.db），launchd 定时任务跑事件研究 |

---

## 5. 落地可行性判断

### P0（1-2 周，纯 easy_tdx，零新数据源）—— 立即开工

- **a.0 情绪面板**：`market-stat` + `quote-list` + `capital-flow` + `board-ranking`
  日频快照落特征库，"成交量>2万亿"等阈值判断直接用。
- **b 板块资金**：MAC `board-summary/ranking` 原生闭环，无需任何新代码即可出板块资金榜。
- **c.4 策略扫描**：`screen scan/rank/strength` 现成三步工作流；**全市场扩展**：
  `bootstrap_vipdoc.py` 分批拉全量 `.day`（唯一外部工作量，注意防封红线）。
- **c.2 量价模型**：支撑/筹码近似写成策略文件，走现有回测框架验证。
- **日报推送**：复用 161129 脚本的 webhook 代码（建议抽公共 notifier）。
- 风险：TDX 防封（62→全市场需分批错峰）；资金流口径坑（只做趋势不做绝对值）。

### P1（2-4 周，接入外部源 + 本机设施）

- **ex 协议特征库已落地（2026-09-01）**：`easy_tdx.features` 包 + `scripts/sync_features.py`
  （launchd 23:00 错峰，48 系列全历史已回填，中债 10Y 回溯至 2006 年）。
  P1 剩余工作：在此之上构建 a.0 市场情绪面板与日报。
- **tushare 接入层（收缩为三接口）**：`margin`（两融，2000 分）、`fx_obasic`
  （美元指数，2000 分）、`moneyflow`（资金流东财口径交叉验证，可选）。
  免费 120 分档先跑 `us_tycr`/`cn_*` 做交叉验证；不充值也能推进。
- **大海牛群情绪**：复用 `dahainiu`/`wechat-reader` skills → 话题热度 → 情绪分特征。
- **f 决策日志**：decisions.db（SQLite）+ 信号 JSON 归档。
- 风险：两融需 2000 分档（200 元/年）才可用；日债缺（英为财情或降级为
  美债+中美利差）；合成 DXY 需回测与真实美元指数的相关性。

### P2（持续迭代，LLM 决策层）

- 知识图谱（LLM 抽取 + 人工审核）→ 板块标签。
- LLM 编排器：宏观→板块→标的→出入点，计划模板化，可信度来自 f 的回归统计。
- 事后回归管道：决策→N日后收益→胜率/盈亏比→反哺 prompt 与阈值校准。
- c.1 宏观策略规则化：卢麒元/留得超模型**先写成规则+信号**（可回测），LLM 只做
  叙事解释，不做黑箱判断。
- 低频外部源补全：中债官网、央行 DR007/MLF、FRED、房产、英为财情（日债）。
- 舆情爬虫（微信指数/抖音）：合规评估后最后做。

### 总体结论

**可行，且 ex 协议实测后数据面覆盖提升至约 85%+**。easy_tdx 单仓提供：A 股全周期
行情、资金流、板块、外盘/国内期货、汇率、上海黄金、中美国债收益率、R007/MLF/OMO/
SLF/Shibor、联邦基金利率、宏观 CPI/PMI/GDP、港股美股，以及指标/策略/回测/寻优全栈
基建；tushare 仅需补**两融 + 美元指数 + 资金流交叉验证**（免费档可起步，两融需
2000 分）；剩余长尾缺口仅日债、美联储资产负债表、房产、舆情四项，全部有免费源/
本机设施兜底。按 P0→P1→P2 推进，每阶段都有独立可用的产出（情绪面板 → 板块资金+
宏观舆情 → 完整决策闭环）。
