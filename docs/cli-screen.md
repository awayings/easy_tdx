# CLI 参考 — 策略选股扫描

## 策略选股扫描（screen）

把策略翻转成选股器：给定一个策略，扫描全市场找出今天触发买入信号的股票，再对这些信号做历史回测排名。**纯离线数据**，读取本地通达信 `.day` 文件，全市场约 30-60 秒。

两步走工作流：

**第一步：信号扫描（scan）**

```bash
# 扫描沪深全 A，找出 RSI 超卖触发的股票
easy-tdx screen scan --strategy strategies/rsi_reversal.py --output signals.json

# 缩小范围
easy-tdx screen scan --strategy strategies/macd_cross.py --universe sz --output signals.json

# 从自定义股票列表扫描
easy-tdx screen scan --strategy strategies/bollinger_breakout.py --universe my_stocks.txt --output signals.json

# 并发扫描（推荐 4-8 进程，速度提升 4-8 倍）
easy-tdx screen scan --strategy strategies/rsi_reversal.py --workers 4 --output signals.json

# 增量扫描（缓存未修改的 .day 文件，跳过重复计算）
easy-tdx screen scan --strategy strategies/rsi_reversal.py --cache scan_cache.json --output signals.json
```

输出示例（JSON）：

```json
{
  "scan_time": "2026-06-10T18:30:00",
  "strategy": "RSIStrategy",
  "total_scanned": 4832,
  "total_signals": 37,
  "signals": [
    {"code": "000001", "market": "SZ", "signal_date": 20260610, "last_close": 12.35},
    {"code": "600519", "market": "SH", "signal_date": 20260610, "last_close": 1800.0}
  ]
}
```

**第二步：回测排名（rank）**

```bash
# 按夏普比率排名（默认）
easy-tdx screen rank --from signals.json --sort sharpe --top 20 --table

# 按最大回撤排名（越小越好，用 --sort-reverse）
easy-tdx screen rank --from signals.json --sort max_drawdown --sort-reverse --table

# 管道模式：一步到位
easy-tdx screen scan --strategy strategies/rsi_reversal.py | easy-tdx screen rank --from - --table

# 补齐股票名称（需要网络）
easy-tdx screen rank --from signals.json --sort sharpe --top 10 --table --names
```

输出示例（`--table`）：

```
[*] 信号排名 (按 sharpe 降序, 共 37 只)
══════════════════════════════════════════════════════════════════════════
排名  代码       名称      总收益率  年化收益  最大回撤  夏普   胜率   交易
 *1   SZ300308  中际旭创   45.23%   18.72%   12.35%   1.85  62.5%    16
 *2   SH600519  贵州茅台   38.10%   15.90%    8.21%   1.62  58.3%    12
```

| 参数 | 说明 |
|------|------|
| `--universe` | `all`（默认，沪深全 A）/ `sh` / `sz` / 文件路径（每行 "市场 代码"） |
| `--vipdoc` | 离线数据目录（默认自动检测通达信安装路径） |
| `--workers` | 并发进程数：`0` 串行（默认）/ `2+` ProcessPoolExecutor 并发（推荐 4-8） |
| `--cache` | 增量扫描缓存文件路径（JSON，mtime 未变的文件自动跳过） |
| `--sort` | 排序指标：`sharpe`（默认）/ `total_return` / `max_drawdown` / `win_rate` 等 |
| `--sort-reverse` | 升序（用于回撤等越小越好的指标） |
| `--names` | 在线补齐股票名称（默认关闭，只查排名中的几十只） |
| `--count` | rank 使用最近 N 条 K 线（0=全部，默认 0） |

### 强势股排名（strength）

按 **5 / 20 / 60 日涨幅加权**合成强势分，从全市场选出"最近最强"的股票。**纯离线数据**，读取本地通达信 `.day` 文件，全市场约 30-60 秒（并发可压到 10 秒内）。

**三种预设模式：**

| 模式 | 性格 | 权重 (w5/w20/w60) | 波动率惩罚 | 适合 |
|------|------|-------------------|-----------|------|
| `steady`（默认） | 中长期稳健 | 0.2 / 0.3 / 0.5 | ✅ 除以 vol_20 | 选"稳着涨"的票，妖股被高波动压低 |
| `breakout` | 近期妖股爆发 | 0.6 / 0.3 / 0.1 | ❌ 纯加权涨幅 | 选"短期最猛"的票，妖股本身就是高波动 |
| `balanced` | 三周期均衡 | 等权 + vol 调整 | ✅ 除以 vol_20 | 不确定时的安全默认 |

> 💡 **为什么 breakout 不除波动率？** 妖股本质高波动，除以 vol 会把它压下去，与"找妖股"目标矛盾。steady 除以 vol 是为了奖励"稳着涨"的票（vol 小，score 放大）。

```bash
# 中长期稳健强势 Top 50（默认 steady 模式）
easy-tdx screen strength --preset steady --top 50 --table

# 近期妖股爆发 Top 20（补齐股票名称）
easy-tdx screen strength --preset breakout --top 20 --names --table

# 三周期均衡
easy-tdx screen strength --preset balanced --top 30 --table

# 自定义权重（自动归一化，5:3:2 = 0.5:0.3:0.2）
easy-tdx screen strength --w5 0.5 --w20 0.3 --w60 0.2 --top 30 --table

# 并发扫描（推荐 4-8 进程）
easy-tdx screen strength --preset steady --top 100 --workers 4 --table

# 过滤低流动性（最近 5 日日均成交额 ≥ 5000 万）
easy-tdx screen strength --preset breakout --top 30 --min-amount 50000000 --table

# 缩小范围 + 输出到文件
easy-tdx screen strength --universe sz --top 30 --output sz_strength.json
```

输出示例（`--table`）：

```
[*] 强势股排名 [steady] 共 50 只
    数据截止: 2026-06-24 | 中长期稳健强势：权重偏 60 日，波动率惩罚，选出稳着涨的票
════════════════════════════════════════════════════════════════════════════════
排名  代码       名称     现价       5日     20日     60日   波动率   强势分
 *1   SZ300308  中际旭创     85.20    8.12%   15.34%   30.21%  0.0180     9.52
 *2   SH600519  贵州茅台  1800.00    3.25%    5.10%   10.05%  0.0120     6.21
```

输出示例（JSON）：

```json
{
  "scan_time": "2026-06-25T10:30:00",
  "preset": "steady",
  "preset_desc": "中长期稳健强势：权重偏 60 日，波动率惩罚...",
  "data_date": 20260624,
  "total_ranked": 50,
  "ranking": [
    {"rank": 1, "code": "300308", "market": "SZ", "name": "中际旭创",
     "last_close": 85.20, "last_date": 20260624,
     "ret_5": 0.0812, "ret_20": 0.1534, "ret_60": 0.3021,
     "vol_20": 0.0180, "strength": 9.52}
  ]
}
```

| 参数 | 说明 |
|------|------|
| `--preset` | 预设模式：`steady`（默认）/ `breakout` / `balanced` |
| `--w5` `--w20` `--w60` | 自定义三周期权重（覆盖预设，自动归一化） |
| `--vol-adjusted` / `--no-vol-adjusted` | 波动率惩罚开关（覆盖预设） |
| `--top` | 返回前 N 名（默认 50） |
| `--universe` | `all`（默认）/ `sh` / `sz` / 文件路径 |
| `--min-listed-days` | 最小上市天数（默认 65，保证能算 60 日涨幅） |
| `--min-amount` | 最近 5 日日均成交额下限（元，默认 0 不过滤） |
| `--workers` | 并发进程数：`0` 串行 / `4+` 并发（推荐 4-8） |
| `--names` | 在线补齐股票名称（默认关闭） |
| `--output` | 输出 JSON 文件（默认 stdout） |

> ⚠️ **数据时效**：strength 依赖本地 `.day` 文件。输出中的 `data_date` / `last_date` 字段标注数据截止日，请先用 `easy-tdx offline sync` 同步最新数据。

