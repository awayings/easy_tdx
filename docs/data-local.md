# 本地数据：K 线仓库与离线文件

## 本地 K 线仓库（v1.26）

行情沉淀为 DuckDB 单文件列存（可选依赖：`pip install easy-tdx[warehouse]`），增量同步 + 临时收盘价状态机 + 健康自检：

```bash
easy-tdx warehouse sync --symbols SH:600519,SZ:000001   # 首次全量，此后只补尾部
easy-tdx warehouse query SH 600519 --count 30           # 默认忽略未收盘的临时 bar
easy-tdx warehouse stats                                # 各标的行数 / 数据范围
easy-tdx warehouse check                                # 缺口 / 除权跳变 / 新鲜度体检
```

## 离线数据 CLI

从本地通达信安装目录直接读取数据文件，无需网络连接：

```bash
easy-tdx offline home                                # 检测通达信安装目录
easy-tdx offline daily SH 600000 --count 10 --table  # A 股日线
easy-tdx offline min SZ 000001 --type lc5 --table    # 分钟线（5min/lc1/lc5）
easy-tdx offline ex-files --table                    # 列出扩展市场可用文件
easy-tdx offline ex-daily 38#2_CPI --count 5 --table # 扩展市场日线（期货/港股/外盘）
easy-tdx offline gbbq C:\new_jyplug\T0002\hq_cache\gbbq --table        # 股本变迁
easy-tdx offline financial C:\new_jyplug\vipdoc\fin\gpcw20260331.dat    # 历史财务
easy-tdx offline blocks C:\new_jyplug\T0002\blocknew --table            # 自定义板块
```

从服务端获取最新日线并写入本地 .day 文件，替代通达信内置下载功能：

```bash
# 同步单只股票日线（自动增量/全量）
easy-tdx offline sync-daily SZ 000001
easy-tdx offline sync-daily SH 600519 --vipdoc C:\new_jyplug\vipdoc

# 一键同步沪深全市场（每天一条命令）
easy-tdx offline sync-all
```

> 建议在通达信关闭时执行 sync 命令，避免文件被锁定。空文件自动全量下载，已有数据只做增量追加。


## 离线数据读取（Python）

无需网络，从本地通达信安装目录直接读取：

```python
from easy_tdx.offline import detect_tdx_home, read_daily_bars, find_daily_bar_file
from easy_tdx import Market

home = detect_tdx_home()
filepath = find_daily_bar_file(Market.SH, "600000")
bars = read_daily_bars(filepath)
```

支持：日线、分钟线、扩展市场日线、板块、股本变迁、历史财务数据。

## 离线数据写入同步（Python）

从服务端获取最新数据并追加写入本地通达信数据文件：

```python
from easy_tdx.offline import (
    encode_daily_bar, append_daily_bars, get_last_bar_date,
    encode_5min_bar, append_5min_bars,
    encode_lc_min_bar, append_lc_min_bars,
)
from easy_tdx import Market
from easy_tdx.client import TdxClient

# 追加日线到 .day 文件（自动跳过重复日期）
from easy_tdx.offline import sync_daily_bars_from_security_bars

# 手动编码单条记录
bar_bytes = encode_daily_bar(bar, price_coeff=0.01, vol_coeff=0.01)

# 获取文件末尾日期
last_date = get_last_bar_date("C:/new_jyplug/vipdoc/sh/lday/sh600000.day")
```

v1.5.0 起可通过 CLI 直接使用：

```bash
easy-tdx offline daily SH 600000 --count 10 --table
easy-tdx offline ex-files --table
easy-tdx offline ex-daily 29#A1801 --table
```

