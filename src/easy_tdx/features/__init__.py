"""扩展市场特征库：采集（ex 协议）+ 存储（家族长表 CSV）+ 读取 API。

数据覆盖（2026-09-01 连通性实测，见 docs/trading_system_requirements.md §3.4）：
汇率、商品/贵金属期货主连、上海黄金现货、中美国债收益率、资金市场与央行利率
（Shibor/R007/MLF/OMO/SLF/准备金率）、美联储利率（EFFR/SOFR）、宏观指标
（PMI/CPI/PPI/M2）、国际指数。日频增量由 scripts/sync_features.py 定时同步。
"""

from .collector import fetch_all, fetch_tail, new_client
from .manifest import DEFAULT_SERIES, STALE_DAYS, ensure_manifest, features_dir, save_manifest
from .store import (
    COLUMNS,
    feature_series,
    load,
    load_all,
    merge_features,
    read_family,
    snapshot,
    to_wide,
    update_family,
)

__all__ = [
    "COLUMNS",
    "DEFAULT_SERIES",
    "STALE_DAYS",
    "ensure_manifest",
    "feature_series",
    "features_dir",
    "fetch_all",
    "fetch_tail",
    "load",
    "load_all",
    "merge_features",
    "new_client",
    "read_family",
    "save_manifest",
    "snapshot",
    "to_wide",
    "update_family",
]
