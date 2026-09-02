"""特征库系列注册表（manifest.json）。

每个系列一条记录：市场（ExMarket 名称）、代码、展示名、频率（D/W/M）、启用开关，
以及同步状态（last_sync/last_bar，由 sync 脚本回写）。注册表存于特征库目录下，
用户可手动增删系列或禁用（enabled=false），重新运行同步即生效。

默认系列均为 2026-09-01 ex 协议连通性实测通过（或已列入服务器合约清单）的代码，
实测报告见 docs/trading_system_requirements.md §3.4。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1

# 频率到"陈旧告警阈值（天）"的映射：D 日频 4 天（覆盖周末 + T+1 发布系列如 SOFR）、
# W 周频 21 天、M 月频 90 天。超过阈值且同步成功的系列会打告警日志。
STALE_DAYS: dict[str, int] = {"D": 4, "W": 21, "M": 90}

# 初始默认系列（48 个）。id 严格约定为 {family}_{code}（机器键，可读性由 name 字段
# 承担），store.feature_series/snapshot 依赖该约定从 id 反推 (family, code)。
DEFAULT_SERIES: list[dict[str, Any]] = [
    # ---- fx 汇率（BASIC_FX，日频）----
    {"id": "fx_USDCNY", "family": "fx", "market": "BASIC_FX", "code": "USDCNY",
     "name": "美元兑人民币", "freq": "D", "enabled": True},
    {"id": "fx_USDCNH", "family": "fx", "market": "BASIC_FX", "code": "USDCNH",
     "name": "美元兑离岸人民币", "freq": "D", "enabled": True},
    {"id": "fx_USDJPY", "family": "fx", "market": "BASIC_FX", "code": "USDJPY",
     "name": "美元兑日元", "freq": "D", "enabled": True},
    {"id": "fx_EURUSD", "family": "fx", "market": "BASIC_FX", "code": "EURUSD",
     "name": "欧元兑美元", "freq": "D", "enabled": True},
    # ---- commodity 商品/贵金属（外盘主连 00W / 国内主连 L8 / 上海黄金现货，日频）----
    {"id": "commodity_GC00W", "family": "commodity", "market": "COMEX_FUTURES", "code": "GC00W",
     "name": "COMEX黄金主连", "freq": "D", "enabled": True},
    {"id": "commodity_HG00W", "family": "commodity", "market": "COMEX_FUTURES", "code": "HG00W",
     "name": "COMEX铜主连", "freq": "D", "enabled": True},
    {"id": "commodity_CL00W", "family": "commodity", "market": "NYMEX_FUTURES", "code": "CL00W",
     "name": "NYMEX原油主连", "freq": "D", "enabled": True},
    {"id": "commodity_AUL8", "family": "commodity", "market": "SH_FUTURES", "code": "AUL8",
     "name": "沪金主连", "freq": "D", "enabled": True},
    {"id": "commodity_AGL8", "family": "commodity", "market": "SH_FUTURES", "code": "AGL8",
     "name": "沪银主连", "freq": "D", "enabled": True},
    {"id": "commodity_CUL8", "family": "commodity", "market": "SH_FUTURES", "code": "CUL8",
     "name": "沪铜主连", "freq": "D", "enabled": True},
    {"id": "commodity_Au99.99", "family": "commodity", "market": "SH_GOLD", "code": "Au99.99",
     "name": "上海金Au99.99", "freq": "D", "enabled": True},
    {"id": "commodity_Ag(T+D)", "family": "commodity", "market": "SH_GOLD", "code": "Ag(T+D)",
     "name": "上海银Ag(T+D)", "freq": "D", "enabled": True},
    # ---- bond 国债收益率（MACRO_INDICATOR 中债 5_CNT* / 美债 8_AT*，日频）+ 美债期货主连 ----
    {"id": "bond_5_CNTY", "family": "bond", "market": "MACRO_INDICATOR", "code": "5_CNTY",
     "name": "中国10年期国债收益率", "freq": "D", "enabled": True},
    {"id": "bond_5_CNT7Y", "family": "bond", "market": "MACRO_INDICATOR", "code": "5_CNT7Y",
     "name": "中国7年期国债收益率", "freq": "D", "enabled": True},
    {"id": "bond_5_CNTFY", "family": "bond", "market": "MACRO_INDICATOR", "code": "5_CNTFY",
     "name": "中国5年期国债收益率", "freq": "D", "enabled": True},
    {"id": "bond_5_CNTTY", "family": "bond", "market": "MACRO_INDICATOR", "code": "5_CNTTY",
     "name": "中国3年期国债收益率", "freq": "D", "enabled": True},
    {"id": "bond_5_CNTOY", "family": "bond", "market": "MACRO_INDICATOR", "code": "5_CNTOY",
     "name": "中国1年期国债收益率", "freq": "D", "enabled": True},
    {"id": "bond_8_ATY", "family": "bond", "market": "MACRO_INDICATOR", "code": "8_ATY",
     "name": "美国10年期国债收益率", "freq": "D", "enabled": True},
    {"id": "bond_8_AT7Y", "family": "bond", "market": "MACRO_INDICATOR", "code": "8_AT7Y",
     "name": "美国7年期国债收益率", "freq": "D", "enabled": True},
    {"id": "bond_8_ATFY", "family": "bond", "market": "MACRO_INDICATOR", "code": "8_ATFY",
     "name": "美国5年期国债收益率", "freq": "D", "enabled": True},
    {"id": "bond_8_ATTY", "family": "bond", "market": "MACRO_INDICATOR", "code": "8_ATTY",
     "name": "美国3年期国债收益率", "freq": "D", "enabled": True},
    {"id": "bond_8_ATOY", "family": "bond", "market": "MACRO_INDICATOR", "code": "8_ATOY",
     "name": "美国1年期国债收益率", "freq": "D", "enabled": True},
    {"id": "bond_TY00W", "family": "bond", "market": "CBOT_FUTURES", "code": "TY00W",
     "name": "美10年国债期货主连", "freq": "D", "enabled": True},
    {"id": "bond_US00W", "family": "bond", "market": "CBOT_FUTURES", "code": "US00W",
     "name": "美30年国债期货主连", "freq": "D", "enabled": True},
    # ---- liquidity 资金市场/央行/美联储利率 ----
    {"id": "liq_5_SHIBOR", "family": "liq", "market": "MACRO_INDICATOR", "code": "5_SHIBOR",
     "name": "上海同业拆借O/N", "freq": "D", "enabled": True},
    {"id": "liq_5_SHR1W", "family": "liq", "market": "MACRO_INDICATOR", "code": "5_SHR1W",
     "name": "上海同业拆借1周", "freq": "D", "enabled": True},
    {"id": "liq_5_SHS3M", "family": "liq", "market": "MACRO_INDICATOR", "code": "5_SHS3M",
     "name": "上海同业拆借3月", "freq": "D", "enabled": True},
    {"id": "liq_9_R001", "family": "liq", "market": "MACRO_INDICATOR", "code": "9_R001",
     "name": "银行间回购R001", "freq": "D", "enabled": True},
    {"id": "liq_9_R007", "family": "liq", "market": "MACRO_INDICATOR", "code": "9_R007",
     "name": "银行间回购R007", "freq": "D", "enabled": True},
    {"id": "liq_9_R014", "family": "liq", "market": "MACRO_INDICATOR", "code": "9_R014",
     "name": "银行间回购R014", "freq": "D", "enabled": True},
    # 注：央行 2025-03 起 MLF 不再是政策利率，数据源已停更（实测 last=2025-02-25）
    {"id": "liq_9_MLF1Y", "family": "liq", "market": "MACRO_INDICATOR", "code": "9_MLF1Y",
     "name": "MLF利率1年（已停更）", "freq": "M", "enabled": False},
    {"id": "liq_9_OMOR07", "family": "liq", "market": "MACRO_INDICATOR", "code": "9_OMOR07",
     "name": "公开市场逆回购7天利率", "freq": "D", "enabled": True},
    # 注：数据源已停更（实测 last=2025-05-31），保留存档但默认不参与每日同步
    {"id": "liq_9_SLF7D", "family": "liq", "market": "MACRO_INDICATOR", "code": "9_SLF7D",
     "name": "常备借贷便利SLF7天（已停更）", "freq": "D", "enabled": False},
    {"id": "liq_5_DDR", "family": "liq", "market": "MACRO_INDICATOR", "code": "5_DDR",
     "name": "存款准备金率", "freq": "M", "enabled": True},
    {"id": "liq_8_EFFR", "family": "liq", "market": "MACRO_INDICATOR", "code": "8_EFFR",
     "name": "美国联邦基金有效利率", "freq": "W", "enabled": True},
    {"id": "liq_8_SOFR", "family": "liq", "market": "MACRO_INDICATOR", "code": "8_SOFR",
     "name": "美国SOFR利率", "freq": "D", "enabled": True},
    # ---- macro 宏观指标（月频）----
    {"id": "macro_3_PMI", "family": "macro", "market": "MACRO_INDICATOR", "code": "3_PMI",
     "name": "中国制造业PMI", "freq": "M", "enabled": True},
    {"id": "macro_2_CPI", "family": "macro", "market": "MACRO_INDICATOR", "code": "2_CPI",
     "name": "中国CPI", "freq": "M", "enabled": True},
    {"id": "macro_2_PPI", "family": "macro", "market": "MACRO_INDICATOR", "code": "2_PPI",
     "name": "中国PPI", "freq": "M", "enabled": True},
    {"id": "macro_5_M2", "family": "macro", "market": "MACRO_INDICATOR", "code": "5_M2",
     "name": "中国M2货币供应", "freq": "M", "enabled": True},
    {"id": "macro_8_ACPI", "family": "macro", "market": "MACRO_INDICATOR", "code": "8_ACPI",
     "name": "美国CPI", "freq": "M", "enabled": True},
    {"id": "macro_8_APMI", "family": "macro", "market": "MACRO_INDICATOR", "code": "8_APMI",
     "name": "美国ISM制造业PMI", "freq": "M", "enabled": True},
    {"id": "macro_8_NONAG", "family": "macro", "market": "MACRO_INDICATOR", "code": "8_NONAG",
     "name": "美国非农就业", "freq": "M", "enabled": True},
    # ---- index 国际指数（日频）----
    {"id": "index_A_DJI", "family": "index", "market": "INTL_INDEX", "code": "A_DJI",
     "name": "道琼斯工业指数", "freq": "D", "enabled": True},
    {"id": "index_A_IXIC", "family": "index", "market": "INTL_INDEX", "code": "A_IXIC",
     "name": "纳斯达克综合", "freq": "D", "enabled": True},
    {"id": "index_A_SPX", "family": "index", "market": "INTL_INDEX", "code": "A_SPX",
     "name": "标普500", "freq": "D", "enabled": True},
    {"id": "index_NK0Y", "family": "index", "market": "INTL_INDEX", "code": "NK0Y",
     "name": "日经225期指连续", "freq": "D", "enabled": True},
    {"id": "index_CNY0", "family": "index", "market": "INTL_INDEX", "code": "CNY0",
     "name": "富时A50期指连续", "freq": "D", "enabled": True},
]


def features_dir(path: str | Path | None = None) -> Path:
    """特征库目录，默认 ``~/.easy_tdx/features``（可被 EASY_TDX_CONFIG_DIR 覆盖）。"""
    if path is not None:
        return Path(path)
    base = os.environ.get("EASY_TDX_CONFIG_DIR", str(Path.home() / ".easy_tdx"))
    return Path(base) / "features"


def manifest_path(path: str | Path | None = None) -> Path:
    return features_dir(path) / "manifest.json"


def ensure_manifest(path: str | Path | None = None) -> list[dict[str, Any]]:
    """确保 manifest.json 存在：缺失则按默认系列创建，已存在则并入新增默认系列。

    已存在的系列原样保留（含用户的 enabled 修改与同步状态）；只追加默认列表里
    尚未出现的系列（默认值，未同步状态）。
    """
    mp = manifest_path(path)
    series: list[dict[str, Any]] = []
    if mp.exists():
        try:
            data = json.loads(mp.read_text("utf-8"))
            series = list(data.get("series", []))
        except (json.JSONDecodeError, OSError):
            series = []  # 损坏则重建
    ids = {s["id"] for s in series}
    for s in DEFAULT_SERIES:
        if s["id"] not in ids:
            series.append(dict(s))
            ids.add(s["id"])
    save_manifest(series, path)
    return series


def save_manifest(series: list[dict[str, Any]], path: str | Path | None = None) -> None:
    """原子写 manifest.json（tmp + rename）。"""
    mp = manifest_path(path)
    mp.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": MANIFEST_VERSION, "series": series}
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, mp)
