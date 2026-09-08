# CLI 参考 — 财务与公告

## 公告检索（巨潮资讯网）

```bash
easy-tdx announcement 688017                          # 默认 30 条，JSON 输出
easy-tdx announcement 601088 --count 10 --page 2      # 翻页
easy-tdx announcement 000001 --table                  # 表格输出（不截断 url）

# 下载最新 5 条公告的 PDF 到 ./pdfs 目录
easy-tdx announcement 601088 --count 5 --download 5 --download-dir ./pdfs
```

> 独立数据源（巨潮资讯网），无需连接 TDX 行情服务器即可使用。
> 返回的 ``url`` 含 4 参数可直接打开，``pdf_url`` 为 PDF 直链。

## 财务

```bash
easy-tdx f10 600519                          # 茅台利润表，最近 8 期（默认 lrb）
easy-tdx f10 600519 --type fzb --num 4       # 资产负债表，最近 4 期
easy-tdx f10 000001 --type llb --table       # 平安现金流量表，表格输出
```

> 新浪财经数据源，``--type`` 支持 ``lrb``（利润表）/``fzb``（资产负债表）/``llb``（现金流量表）。
> 独立于 TDX 行情服务器，``item_value`` 已转 float 可直接数值计算，同比附 ``{科目}_同比`` 列。

## 通达信原生 F10 与最新财务快照

走通达信协议（与 Web 层 ``/finance`` ``/company/*`` 端点同源），覆盖 ``f10``（新浪三表）之外的 F10 全文板块。完整示例见 [examples/06_finance/](../examples/06_finance/README.md)。

```bash
easy-tdx finance-info SH 600519 --table                          # 最新财务快照（30+ 项单期指标）
easy-tdx company-info SH 600519                                  # F10 板块目录（最新提示/公司概况/...）
easy-tdx company-info SH 600519 "公司概况"                        # 读板块完整正文（自动解析+读全，无需 offset/length）
easy-tdx company-info SH 600519 600519.txt                       # 也可直接传文件名（此时用 --offset/--length）
```

- ``finance-info``：最新一期财务快照，含股本结构、资产负债、利润、现金流、每股指标（37 字段）。与 ``f10`` 互补——前者是单期快照，后者是多期三表。
- ``company-info``：**一个命令两种用法**——无板块名参数列 F10 板块目录，有板块名参数读正文。目录含 16 个板块（最新提示、公司概况、财务分析、股东研究、股本结构、资本运作、业内点评、行业分析、公司大事、研究报告、经营分析、主力追踪、分红扩股、高层治理、龙虎榜单、关联个股）。
- 读正文时传板块名即可自动读取完整内容（按目录 length 分块循环，大板块如「公司大事」也能一次读全）；``--offset``/``--length`` 仅在传文件名时生效。通达信多服务器目录版本不一致时自动重试命中。
