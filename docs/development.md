# 开发环境与流程

本文档是开发者的完整工作流参考。命令速查亦见 [CLAUDE.md](../CLAUDE.md)。

## 环境初始化

```bash
git clone git@github.com:awayings/easy_tdx.git && cd easy_tdx
uv sync --all-extras          # 建 .venv 并装全部 extras（普通 uv sync 只装基础依赖）
```

CI 不用 uv，走 `pip install -e ".[dev]"` + `pip install -r requirements-dev.txt`。改依赖时两套流程都要考虑：pyproject.toml 只写下界+上界，`uv.lock` 与 `requirements-dev.txt` 才是可复现锁（dev 工具链锁定版本必须兼容 Python 3.10，CI 矩阵跑 3.10/3.12/3.13）。

前端：`cd web-ui && npm ci && npm run build`（`build` = vue-tsc 类型检查 + vite build）。**`pip install -e .` 要求 `web-ui/dist` 存在**（hatchling `force-include` 把它打进 wheel 的 `easy_tdx/web/dist/`），所以改前端后、跑 Python 测试/安装前必须先构建。

## 测试

```bash
python -m pytest tests/unit/ -v                             # 单元测试（无需网络，全部 mock）
XMTDX_LIVE=1 python -m pytest tests/integration/ -v        # 集成测试（连真实通达信服务器，默认 skip）
python -m pytest tests/unit/ --cov src/easy_tdx --cov-fail-under=60   # 覆盖率门槛 60
```

三层测试分布：

| 层 | 位置 | 跑在哪 | 说明 |
|---|---|---|---|
| 单元 | `tests/unit/` | CI（6 格矩阵）+ 本地 | 全 mock：`tests/fixtures/` 真实协议 hex dump + JSON 对照，`tests/golden/` 期望输出 |
| 集成 | `tests/integration/` | **仅本地** | `XMTDX_LIVE=1` 才执行，连真实 TDX 服务器 smoke test |
| 前端 E2E | `web-ui/e2e/*.spec.ts` | CI frontend job + 本地 | Playwright，`EASY_TDX_E2E_MOCK=1` 后端合成行情，不连真实服务器 |

- `tests/conftest.py` 默认设 `EASY_TDX_NO_TASK_DB=1`，防单测污染 `~/.easy_tdx/tasks.db`；需要测试存储的用例用 `EASY_TDX_CONFIG_DIR` 指向 `tmp_path` 显式重开。
- `tests/unit/test_ai_llm.py` 依赖 LLM API key 轮询，CI 用 `--ignore` 跳过，本地有 mock 可跑。
- `asyncio_mode = "auto"`：async 测试无需 `@pytest.mark.asyncio`。

## 静态检查

```bash
mypy src/                            # strict 模式（pyproject 配置）
ruff check src/ tests/               # E/F/I/UP 规则
ruff format --check src/ tests/
```

mypy/ruff 均排除 `src/easy_tdx/exchange_margin.py`；`MyTT.py` 有手写 `MyTT.pyi` stub 保持 strict 检查。

本地一键门禁（等价 CI 三 job 的本地版）：

```bash
bash scripts/verify_ci.sh            # ruff → format → mypy → pytest（排除 integration）→ 前端 build + E2E
bash scripts/verify_ci.sh --fast     # 只跑静态检查
# 可选：ln -s ../../scripts/verify_ci.sh .git/hooks/pre-push
```

## 开发流程

### a. 需求（Issue）

- 功能/缺陷先在 GitHub Issues 提出；代码注释与提交说明中引用编号（如 `#58`、`审计 #9`）。
- 复杂功能先写设计文档再动手（历史范本：`docs/board-overview-design.md`、`docs/hotspot-rolling-design.md`，以及 `docs/superpowers/` 下的 specs）。
- 功能变更时同步评估文档更新（见「文档规范」）。

### b. 开发与测试

- 新功能/修复按 TDD 习惯先写红测试再实现；单元测试必须零网络、零真实服务器依赖。
- 命令形态变更时 `easy-tdx --help` 自检；Web 路由变更时 `/docs` Swagger 自检。

### c. 提交与 CI

- 提交信息带前缀（`feat:` / `fix:` / `test:` / `docs:` / `refactor:`，可带作用域如 `feat(web):`），中文描述。push/PR 到 `main` 触发 `ci.yml`：
  - test job：ubuntu/windows × py3.10/3.12/3.13 六格矩阵跑单测（覆盖率 ≥60）+ ruff + format
  - mypy job：strict 模式（py3.13）
  - frontend job：vue-tsc + Playwright mock E2E
- **集成测试不进 CI**（依赖真实 TDX 服务器），发布前本地 `XMTDX_LIVE=1` 手动跑一遍。

### d. 发布版本

1. 收敛时打一个聚合提交 `release: vX.Y.Z — 中文一句话摘要`，同一提交内包含：代码与测试改动 + `pyproject.toml` 版本号 bump + `CHANGELOG.md` 新增版本小节。
2. CHANGELOG 遵循 Keep a Changelog（zh-CN）：`## [X.Y.Z] — 日期` 倒序最新在前，先一段加粗导语再按主题分 `###` 小节，条目带文件链接，测试小节报数字验收（如「全量 1820 通过，ruff / mypy / vue-tsc 全绿」）。**无 Unreleased 区，CHANGELOG 在 release 提交时写入**。
3. 打 tag 推送即自动发布：`git tag vX.Y.Z && git push origin vX.Y.Z`，触发两个独立 workflow——`publish.yml`（PyPI trusted publishing，OIDC 无 token）与 `release.yml`（Windows EXE via PyInstaller + GitHub Release，正文含 SHA256/SmartScreen 说明；GitHub release notes 自动生成，**不读取 CHANGELOG.md**）。
4. 版本单一来源是 `pyproject.toml`（前端品牌区版本来自后端 `GET /api/v1/meta`）；文档一律不写死版本号。

## 文档规范

- **大小限制**：每份文档约 500 行为上限，超限必须拆分，通过 [index.md](./index.md) 与 README 文档导航串联。过大文件不可新增。
- **类型两分**：教程（how-to，代码示例）与参考（速查/字段/命令表）原则上分开成文。
- **可达性标准**：任何文档变更必须以「[README.md](../README.md) + [docs/index.md](./index.md) 可达的文件」为标准——新文件必须同时进 README 文档导航与 index.md；删除/改名必须同步修复全部链接。
- **禁止硬编码版本横幅**：版本信息以 `pyproject.toml` 与 `CHANGELOG.md` 为准；功能引入说明里的版本标注（如「v1.29.1」）除外。
