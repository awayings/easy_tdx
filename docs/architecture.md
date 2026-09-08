# 架构

<img src="./easy-tdx-architecture.png" alt="easy-tdx 七层架构总览：接口 → 服务 → 领域 → 持久 → 网关 → 协议 → 外部源" />

七层分层，请求自上而下、数据（pandas DataFrame）自下而上：**① 用户接口层**（Web UI / CLI / Python API / 桌面 EXE）→ **② Web 服务层**（FastAPI + SSE 实时推送 + 异步任务）→ **③ 领域层**（回测 / 指标 / 缠论 / 因子 / 选股 / 组合，纯计算零网络）→ **④ 数据持久层**（DuckDB K 线仓库 + 通达信本地 vipdoc 文件）→ **⑤ 客户端网关层**（8 个客户端 + 健康分 / 故障转移）→ **⑥ 协议层**（通达信二进制协议编解码）→ **⑦ 外部数据源**（通达信服务器 / 中金所 / 新浪 / 巨潮 / LLM）。虚线为旁路直连（HTTP 数据源 / 本地文件 / CLI 与 Python API 越层直调）。

🖼️ 交互版架构图（可缩放平移、悬停查看 38 个模块的职责详情、一键导出 PNG）：[architecture.html](./architecture.html)

## 源码树

```
src/easy_tdx/
├── client.py          # TdxClient / AsyncTdxClient（标准协议）
├── unified.py         # UnifiedTdxClient（统一入口）
├── config.py          # 服务器地址、端口、超时配置
├── indicator.py       # 技术指标计算（34 个，基于 MyTT）
├── MyTT.py            # 麦语言技术指标算法库
├── mac/
│   ├── client.py      # MacClient / AsyncMacClient（MAC 协议）
│   ├── enums.py       # Period, Adjust, Category, ExMarket, SortType, ...
│   ├── models.py      # MacBar, MacQuoteField, MacTick, BoardInfo, ...
│   └── commands/      # MAC 命令（build_request + parse_response，无 IO）
├── ex/
│   ├── client.py      # ExTdxClient / AsyncExTdxClient（标准协议扩展市场）
│   ├── mac_client.py  # MacExClient / AsyncMacExClient（MAC 协议扩展市场）
│   └── transport/     # ExTdxConnection（端口 7727）
├── transport/
│   ├── sync.py        # TdxConnection + ping_host / ping_all
│   └── async_.py      # AsyncTdxConnection（asyncio）
├── commands/          # 标准协议命令（无 IO）
├── codec/             # price / volume / datetime / frame / bitmap 编解码
├── chanlun/           # 缠论技术分析（K线合并/分型/笔/线段/中枢/买卖点/背驰）
├── factor/            # 因子引擎（Factor ABC/19内置因子/截面计算/因子分析/预处理管道）
├── portfolio/         # 组合管理（4优化器/风险模型/再平衡引擎）
├── backtest/          # 回测引擎（Strategy基类/向量化引擎/多因子组合/滑点模型/执行仿真/归因分析）
├── screen/            # 策略选股扫描（scan信号扫描/rank回测排名/并发扫描/增量缓存）
├── realtime/          # 实时数据推送框架（EventBus/事件驱动/asyncio）
├── web/               # Web API（FastAPI REST + WebSocket）
├── models/            # 纯 dataclass，无业务逻辑
├── offline/           # 离线数据读写模块（读取 + 写入同步）
└── cli/               # easy-tdx CLI（click）
```

commands 层不依赖 transport，可独立单测。

## 分层要点

- **协议层无 IO**：`commands/`、`codec/`、`mac/commands/` 只做编解码，可完全离线单测（`tests/fixtures/` 的 hex dump 即其测试镜像）。
- **领域层零网络**：`backtest/`、`chanlun/`、`factor/`、`portfolio/`、`screen/` 只吃 DataFrame，不碰网络——这是回测/扫描可在无行情连接时运行的基础。
- **网关层统一入口**：`unified.py` 封装 8 个客户端（标准/MAC × 同步/异步 × 常规/扩展），带健康分与故障转移；上层（Web、CLI）优先走它。
- **越层直调**：CLI 与 Python API 不经过 Web 服务层，直接调网关/领域层；HTTP 数据源（新浪/巨潮/中金所）与本地文件直读是旁路。因此同一功能常有三处入口（CLI 命令 / Python API / REST 端点），改领域逻辑三处受益；改协议/网关时注意 Web 层的替身切入点（`web/e2e_mock.py` 在 `EASY_TDX_E2E_MOCK=1` 下替换全部客户端）。
- **CLI 薄封装**：`cli/cmd_*.py` 每个文件一个 click 命令组，业务全部在领域/网关层；领域模块内也有 CLI（`backtest/cli.py`、`screen/cli.py`），由 `cli/__init__.py` 汇总注册。入口链：`easy-tdx` 命令 → `_editable_guard:main`（可编辑安装失效时打印修复指引）→ CLI。
- **`python -m easy_tdx` 三种形态**（`__main__.py`）：开发态无参默认等价 `easy-tdx serve`；打包态双击走托盘（uvicorn 后台线程 + 主线程 pystray 托盘）；multiprocessing 子进程拦截保护（Windows spawn 下防止子进程重复启动 uvicorn，一键寻优/screen 扫描等多进程功能依赖它）。
- **离线数据平台差异**：`.day` 文件路径分隔符 / GBK 文件名 / 时区行为在 Windows 与 Linux 不同，CI 有 Windows matrix 覆盖，改 `offline/` 时留意。
