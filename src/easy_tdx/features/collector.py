"""特征采集：经 MAC 扩展行情协议（端口 7727）拉取系列日线。

单系列单请求；服务端单次响应上限 700 根，更早历史用 ``start`` 偏移翻页（实测
2023-06 之前可翻到）。TDX 防封红线：并发 ≤4-8，每个 worker 独立连接；
每日增量只取尾部（``fetch_tail``），全历史回填用 ``fetch_all``（首次约 1~3 页/系列）。
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from ..ex.mac_client import MacExClient
from ..mac.enums import ExMarket, Period

logger = logging.getLogger(__name__)

# 服务端单次 K 线响应上限（实测：请求 count=800 只回 700 根）
_PAGE_BARS = 700
_MAX_PAGES = 30  # 翻页护栏：30 页 ≈ 21000 根，远超任何系列的全历史

# 长表保留列（丢弃 float_shares 等非必需字段）
_KEEP_COLUMNS = ["date", "open", "high", "low", "close", "vol", "amount"]


def refresh_best_host() -> None:
    """单线程刷新最优主机缓存（并发放起前调用一次）。

    不能在 worker 内并发调用 from_best_host：它会写 config.json（config.tmp +
    replace 非原子），并发会互相抢 tmp 文件（实测 FileNotFoundError 竞态）。
    """
    try:
        MacExClient.from_best_host().close()
    except Exception:
        logger.warning("最优主机测速失败，沿用缓存/默认主机", exc_info=True)


def new_client() -> MacExClient:
    """每个 worker 独立连接，只读缓存的 best_mac_ex_host（不写 config）。"""
    client = MacExClient()
    client.connect()
    return client


def _market_of(series: dict[str, Any]) -> ExMarket:
    """manifest 的市场名（如 BASIC_FX）→ ExMarket 枚举值。"""
    try:
        return ExMarket[series["market"]]
    except KeyError:
        raise ValueError(
            f"manifest 中市场名无效: {series['market']!r}（series={series.get('id')!r}）"
        ) from None


def _normalize(df: pd.DataFrame, code: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.rename(columns={"datetime": "date"}) if "datetime" in df.columns else df
    keep = [c for c in _KEEP_COLUMNS if c in df.columns]
    out = df[keep].copy()
    out["code"] = code
    out["date"] = pd.to_datetime(out["date"])
    return out


def fetch_tail(
    client: MacExClient,
    series: dict[str, Any],
    count: int = 60,
) -> pd.DataFrame:
    """取最新 count 根（服务端从最新往前截断返回，升序）。

    增量同步用：count 取 60 即可覆盖周末/长假间隔，且单请求流量恒定。
    """
    df = client.goods_kline(
        _market_of(series), series["code"], Period.DAILY, start=0, count=count
    )
    return _normalize(df, series["code"])


def fetch_all(client: MacExClient, series: dict[str, Any]) -> pd.DataFrame:
    """翻页拉全历史（--backfill 用），700 根/页，短页（<700）或空页终止。"""
    pages: list[pd.DataFrame] = []
    start = 0
    for _ in range(_MAX_PAGES):
        df = client.goods_kline(
            _market_of(series), series["code"], Period.DAILY, start=start, count=_PAGE_BARS
        )
        if df.empty:
            break
        pages.append(df)
        if len(df) < _PAGE_BARS:
            break
        start += len(df)
    if not pages:
        return pd.DataFrame()
    return _normalize(pd.concat(pages, ignore_index=True), series["code"])
