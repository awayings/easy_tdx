# 06. 财务数据与 F10 公司信息

通达信原生协议（TdxClient）提供两类财务/公司数据，独立于 [新浪三表 `f10`](../../docs/cli-finance.md#财务)：

| 命令 / 接口 | 数据 | 说明 |
|-------------|------|------|
| `finance-info` / `GET /api/v1/finance` | **最新财务快照** | 单期 37 字段（股本、资产负债、利润、现金流、每股指标） |
| `company-info`（无板块名）/ `GET /api/v1/company/category` | **F10 板块目录** | 列出全部板块及其文件偏移 |
| `company-info "板块名"` / `GET /api/v1/company/content` | **F10 板块正文** | 读取指定板块的 GBK 文本 |

> **与 `f10` 的区别**：`f10` 走新浪 HTTP，返回多期结构化利润表/负债表/现金流（DataFrame，可数值计算）；本目录的命令走通达信协议，`finance-info` 是最新一期的快照，`company-info` 是 F10 全文板块（纯文本）。

## company-info 命令的两种用法

`company-info` 根据是否传入板块名参数自动切换行为：

```bash
# 用法 1：无板块名 → 列出 F10 板块目录
easy-tdx company-info SH 600519 --table

# 用法 2：有板块名 → 读取该板块正文
easy-tdx company-info SH 600519 "公司概况"
easy-tdx company-info SH 600519 "分红扩股" --length 2048
```

读正文时**只需传板块名即可读完整内容**（自动按目录 length 分块循环，大板块也一次读全），无需关心 offset/length。也可换成文件名（`600519.txt`），此时用 `--offset`/`--length` 控制。

## F10 板块完整列表

`company-info` 列目录时返回的全部板块（以贵州茅台 600519 为例，个股板块可能略有差异）：

| 板块名 | 说明 |
|--------|------|
| 最新提示 | 最新指标、近期公告、机构持股变化、概念板块 |
| 公司概况 | 基本资料、发行上市、关联企业 |
| 财务分析 | 主要财务指标、环比/同比分析 |
| 股东研究 | 十大股东、流通股东、股东变化 |
| 股本结构 | 股本结构、股本变化、限售流通、股权激励 |
| 资本运作 | 募集资金使用、重大事项 |
| 业内点评 | 机构点评、投资建议 |
| 行业分析 | 行业地位、市场前景 |
| 公司大事 | 重要事件、公告 |
| 研究报告 | 券商研报、盈利预测 |
| 经营分析 | 主营业务、经营情况 |
| 主力追踪 | 主力资金、筹码集中度 |
| 分红扩股 | 分红送转、历史派息 |
| 高层治理 | 高管简历、薪酬 |
| 龙虎榜单 | 龙虎榜上榜记录 |
| 关联个股 | 同行业/同概念关联股票 |

## 示例文件

| 文件 | 调用方式 | 覆盖内容 |
|------|----------|----------|
| `finance_info.py` | Python API | 最新财务快照（37 字段） |
| `company_info.py` | Python API | F10 板块目录 + 遍历各板块正文 |
| `company_cli.sh` | CLI | `finance-info` / `company-info` 全用法 |
| `company_web_api.py` | Web API | `/finance` `/company/category` `/company/content` |

## 快速开始

### CLI

```bash
# 最新财务快照
easy-tdx finance-info SH 600519 --table

# F10 板块目录
easy-tdx company-info SH 600519

# 读 F10 正文
easy-tdx company-info SH 600519 "公司概况"
```

### Python API

```python
from easy_tdx import Market, TdxClient

with TdxClient.from_best_host() as c:
    # 最新财务快照（单行 DataFrame）
    finance = c.get_finance_info(Market.SH, "600519")

    # F10 板块目录
    cats = c.get_company_info_category(Market.SH, "600519")

    # 读某板块正文：先从目录查 filename/start/length
    row = cats[cats["name"] == "公司概况"].iloc[0]
    content = c.get_company_info_content(
        Market.SH, "600519", row["filename"], int(row["start"]), int(row["length"])
    )
    print(content)
```

### Web API

```bash
# 启动服务
easy-tdx serve

# 最新财务快照
curl "http://localhost:8000/api/v1/finance?market=SH&code=600519"

# F10 板块目录
curl "http://localhost:8000/api/v1/company/category?market=SH&code=600519"

# 读 F10 正文（filename/offset/length 从目录接口获取）
curl "http://localhost:8000/api/v1/company/content?market=SH&code=600519&filename=600519.txt&offset=0&length=2048"
```

## 字段说明

### finance-info（最新财务快照）

| 字段 | 单位 | 说明 |
|------|------|------|
| `liutong_guben` / `zong_guben` | 万股 | 流通股本 / 总股本 |
| `guojia_gu` / `faren_gu` / `b_gu` / `h_gu` | 万股 | 国家股 / 法人股 / B股 / H股 |
| `zong_zichan` / `liudong_zichan` / `guding_zichan` | 元 | 总/流动/固定资产 |
| `liudong_fuzhai` / `changqi_fuzhai` / `jing_zichan` | 元 | 流动/长期负债 / 净资产 |
| `zhuying_shouru` / `yingye_lirun` / `jing_lirun` | 元 | 主营收入 / 营业利润 / 净利润 |
| `jingying_xianjinliu` / `zong_xianjinliu` | 元 | 经营现金流 / 总现金流 |
| `meigujing_zichan` | 元 | 每股净资产 |
| `updated_date` / `ipo_date` | YYYYMMDD | 财务更新日 / 上市日 |

完整字段见 `finance_info.py` 顶部注释。

### company-info（F10 板块目录）

| 列 | 类型 | 说明 |
|----|------|------|
| `name` | str | 板块名（如「公司概况」） |
| `filename` | str | 内容文件名（如 `600519.txt`） |
| `start` | int | 内容在该文件中的起始偏移（字节） |
| `length` | int | 内容长度（字节） |
