"""自选股路由：加入 / 列出 / 移除（SQLite 持久化），以及近 N 交易日涨跌幅锚点。

``GET /watchlist/returns``（issue #7）只回**锚点收盘价**，涨跌幅由前端用 SSE
实时价现算——三列跟着报价免费跳动，盘中无需轮询本接口。

取数语义对齐 ``/bars``：MAC 优先（``adjust=QFQ``，除权日不出假跌幅）→ MAC
不可用/失败时降级标准 TdxClient（**不复权**，日志标注，不静默）。个股当日序列与
交易日历（上证指数日线）都用进程内缓存——两者当日不变、次日失效；日历的重取时机
见 :func:`_calendar_stale`（盘前启动的 serve 必须能等到今天的 bar 生成，否则
``T`` 会整体前移一个交易日）。

口径与锚定算法见 :mod:`easy_tdx.web.returns`（纯计算，本模块只负责取数/缓存）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, NamedTuple

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, model_serializer

from easy_tdx.exceptions import TdxConnectionError
from easy_tdx.mac.enums import Adjust, Period
from easy_tdx.models.enums import KlineCategory, Market
from easy_tdx.realtime.session import SHANGHAI_TZ, is_trading_time
from easy_tdx.web.convert import market_from_str, market_value_from_str
from easy_tdx.web.deps import get_client, get_mac_client_optional
from easy_tdx.web.returns import (
    DEFAULT_WINDOWS,
    StockReturns,
    compute_stock_returns,
    resolve_trade_date,
)
from easy_tdx.web.watchlist_store import WatchItem, get_watchlist_store

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["watchlist"])

# 6 位数字代码（自选会被 QuoteStreamer 拿去轮询，非数字代码产生无效请求）
_CODE_PATTERN = r"^\d{6}$"

# ── 近 N 交易日涨跌幅（issue #7）────────────────────────────────────────────

_CALENDAR_MARKET = Market.SH  # 交易日历 = 上证指数（个股停牌会缺日期，不能当日历）
_CALENDAR_CODE = "000001"
# 取数根数 = 最大窗口 + 缓冲。数学上 max(DEFAULT_WINDOWS)+1=11 根就够（D_10 之后最多
# 10 个交易日，第 11 根必然 <= D_10），+10 缓冲吸收序列端部意外；20 根仍是 MAC 单页
# （700 根/页）。实测 MAC 服务端 QFQ 复权锚与请求窗口无关（count=11 vs 800 收盘价
# 完全一致），缩小窗口不改变锚点值，800 根的 2 页请求纯属浪费。
_BAR_COUNT = max(DEFAULT_WINDOWS) + 10
_CONCURRENCY = 4  # TDX 防封红线：并发 ≤ 4
# 日历"未确认"（缺今天）时的重取间隔，详见 _calendar_stale
_CALENDAR_RETRY_SECONDS = 60.0


class _CalendarEntry(NamedTuple):
    """交易日历缓存值。"""

    calendar: list[date]
    fetched_at: datetime


# 交易日历进程内缓存：{"当时日历日": _CalendarEntry}，一天一条
_calendar_cache: dict[str, _CalendarEntry] = {}
# 个股日线进程内缓存：symbol → (取数当日, 序列)。锚点只用历史 bar（当日不变），
# 同一天里前端加载/增删自选各拉一次都零行情请求；次日 key 不匹配自动失效。
_bars_cache: dict[str, tuple[str, list[tuple[date, float]]]] = {}


class WatchItemAdd(BaseModel):
    """加入自选请求。name 由前端从行情数据带过来。"""

    market: str = Field(..., pattern=r"^(SZ|SH|BJ)$")
    code: str = Field(..., pattern=_CODE_PATTERN)
    name: str = Field("", max_length=64)
    group: str = Field("默认", max_length=32)


class WatchlistResponse(BaseModel):
    items: list[dict[str, object]]
    count: int


class ReturnAnchorItem(BaseModel):
    """单个窗口的锚点（``close``/``date`` 为 null = 数据不足，前端显示 ``-``）。"""

    days: int
    close: float | None = None
    date: str | None = None


class WatchReturnsItem(BaseModel):
    """一只自选的锚点结果；取数失败时只落 ``error``（不影响整表）。

    字段全为可选：失败项只设 ``error``，其余 ``None`` 字段由
    :meth:`_drop_none` 从 JSON 中剔除。
    """

    last_close: float | None = None
    last_date: str | None = None
    stale_days: int | None = None
    anchors: list[ReturnAnchorItem] | None = None
    error: str | None = None

    @model_serializer(mode="wrap")
    def _drop_none(self, handler: Any) -> dict[str, Any]:
        """``None`` 字段不落 JSON：失败项即 ``{"error": "no_data"}``（契约同款）。"""
        return {k: v for k, v in handler(self).items() if v is not None}


class WatchlistReturnsResponse(BaseModel):
    """``trade_date`` = 锚定出的 ``T``（自选为空时为 null，不请求行情）。"""

    trade_date: str | None
    items: dict[str, WatchReturnsItem]


# ── 交易日历 / 个股日线取数（纯 IO，缓存与降级都在这里）──────────────────────


def _today() -> date:
    """今天的日历日（沪市时区，与主机时区无关；单测可 monkeypatch）。"""
    return datetime.now(SHANGHAI_TZ).date()


def _now() -> datetime:
    """当前沪市时间（单测可 monkeypatch；须与 ``_today`` 的桩同一天）。"""
    return datetime.now(SHANGHAI_TZ)


def _calendar_stale(entry: _CalendarEntry, today: date, now: datetime) -> bool:
    """日历缓存是否该重取。

    **为什么不能无脑缓存一天**：``T`` 由"日历中 ``<=`` 今天的最后一个交易日"定出，
    而今天的日线 bar 要等开盘后才生成。若 serve 当天第一次取数发生在**开盘前**
    （机器早开机、服务常驻），日历里就没有今天 → ``T`` 退到前一个交易日 →
    三个锚点**整体前移一个交易日**。缓存键就是日期本身，当天不会自我纠正，
    会一路错到次日，且数值看起来完全合理、不报任何错。

    **为什么缺今天不是每个请求都重取**：交易日与节假日无法从日历本身分辨 ——
    "缺今天"既可能是"今天的 bar 还没生成"，也可能是"今天根本不开市"。所以只在
    交易时段内、距上次取数满 :data:`_CALENDAR_RETRY_SECONDS` 才重取。真正的交易日
    今天的 bar 一出现就命中确认、此后当天不再请求（正常盘中路径零额外请求）；
    节假日则退化成每个请求间隔最多 1 次指数日线，与页面打开时拉一次同级。
    """
    if today in entry.calendar:
        return False
    if not is_trading_time(now):
        return False
    return (now - entry.fetched_at).total_seconds() >= _CALENDAR_RETRY_SECONDS


def _series_from_df(df: Any) -> list[tuple[date, float]]:
    """DataFrame → ``(日期, 收盘价)`` 升序去重序列（MAC 的 datetime / 标准的 date 列都认）。

    非正收盘价丢弃：QFQ 深层历史可能返回 0/负价（见 ``/bars`` 文档），
    作锚点算涨跌幅无意义。
    """
    if df is None or getattr(df, "empty", True) or "close" not in getattr(df, "columns", []):
        return []
    time_col = next((c for c in ("date", "datetime") if c in df.columns), None)
    if time_col is None:
        return []
    times = pd.to_datetime(df[time_col], errors="coerce")
    closes = pd.to_numeric(df["close"], errors="coerce")
    out: dict[date, float] = {}
    for ts, close in zip(times, closes):
        if pd.isna(ts) or not close > 0:
            continue
        out[ts.date()] = float(close)
    return sorted(out.items())


async def _fetch_bars(
    market: str, code: str, mac_client: Any, client: Any, *, is_index: bool = False
) -> list[tuple[date, float]]:
    """按 ``/bars`` 语义取日线：MAC 优先（QFQ）→ 标准 TdxClient 降级（不复权）。

    Raises:
        最后一级失败时的原始异常（调用方按"单只失败不影响整表"处理）。
    """
    if mac_client is not None:
        try:
            df = await mac_client.get_stock_kline(
                market=market_value_from_str(market),
                code=code,
                period=Period.DAILY,
                start=0,
                count=_BAR_COUNT,
                times=1,
                adjust=Adjust.QFQ,
            )
            bars = _series_from_df(df)
            if bars:
                return bars
            _logger.info("/watchlist/returns MAC 返回空，转标准 TdxClient (%s%s)", market, code)
        except Exception as exc:  # noqa: BLE001 — 降级到标准客户端，不中断
            _logger.warning(
                "/watchlist/returns MAC 获取失败，转标准 TdxClient (%s%s): %s", market, code, exc
            )
    else:
        _logger.warning(
            "/watchlist/returns MAC 客户端未连接，降级标准 TdxClient"
            "（%s%s 不复权，除权日可能出现假跌幅）",
            market,
            code,
        )
    market_enum = market_from_str(market)
    if is_index:
        df = await client.get_index_bars(market_enum, code, KlineCategory.DAY, 0, _BAR_COUNT)
    else:
        df = await client.get_security_bars(market_enum, code, KlineCategory.DAY, 0, _BAR_COUNT)
    return _series_from_df(df)


async def _trade_calendar(mac_client: Any, client: Any, today: date, now: datetime) -> list[date]:
    """交易日历 = 上证指数日线的日期列（进程内缓存，刷新时机见 :func:`_calendar_stale`）。

    含今天的日历取一次即长期命中；缺今天（盘前首次取数）则按退避节奏探针若干次，
    直到今天的 bar 生成、或判定今天不开市而停止。
    """
    key = today.isoformat()
    cached = _calendar_cache.get(key)
    if cached is not None and not _calendar_stale(cached, today, now):
        return cached.calendar
    bars = await _fetch_bars(
        _CALENDAR_MARKET.name, _CALENDAR_CODE, mac_client, client, is_index=True
    )
    # 取数失败时沿用当天旧日历（比整个端点 503 好）；时间戳照常刷新，下轮按间隔再试
    calendar = sorted({d for d, _ in bars}) or (cached.calendar if cached is not None else [])
    if calendar:
        _calendar_cache.clear()  # 只保留当天一条，避免跨日堆积
        _calendar_cache[key] = _CalendarEntry(calendar, now)
    return calendar


async def _returns_for(
    item: WatchItem,
    calendar: list[date],
    today: date,
    day: str,
    mac_client: Any,
    client: Any,
) -> WatchReturnsItem:
    """单只自选 → 锚点结果；任何失败都收敛成 ``error``（整表不受影响）。"""
    try:
        cached = _bars_cache.get(item.symbol)
        bars = cached[1] if cached is not None and cached[0] == day else None
        if bars is None:
            bars = await _fetch_bars(item.market, item.code, mac_client, client)
            if bars:
                _bars_cache[item.symbol] = (day, bars)
        if not bars:
            return WatchReturnsItem(error="no_data")
        result: StockReturns | None = compute_stock_returns(calendar, bars, today=today)
        if result is None:
            return WatchReturnsItem(error="no_data")
        return WatchReturnsItem(
            last_close=None if result.last_close is None else round(result.last_close, 4),
            last_date=result.last_date.isoformat() if result.last_date else None,
            stale_days=result.stale_days,
            anchors=[
                ReturnAnchorItem(
                    days=a.days,
                    close=None if a.close is None else round(a.close, 4),
                    date=a.date.isoformat() if a.date else None,
                )
                for a in result.anchors
            ],
        )
    except Exception as exc:  # noqa: BLE001 — 单只失败不影响整表
        _logger.warning("/watchlist/returns 单只取数失败 %s: %s", item.symbol, exc)
        return WatchReturnsItem(error="fetch_failed")


# ── 端点 ────────────────────────────────────────────────────────────────────


@router.get("/watchlist", response_model=WatchlistResponse)
async def list_watchlist(
    group: str | None = Query(None, description="按分组过滤"),
) -> WatchlistResponse:
    """列出全部自选（按加入顺序）。"""
    items = get_watchlist_store().list_all(group=group)
    return WatchlistResponse(items=[i.to_dict() for i in items], count=len(items))


@router.get("/watchlist/returns", response_model=WatchlistReturnsResponse)
async def watchlist_returns(
    mac_client: Any = Depends(get_mac_client_optional),
    client: Any = Depends(get_client),
) -> WatchlistReturnsResponse:
    """自选列表「近 3 日 / 近 1 周 / 近 2 周」涨跌幅的**锚点收盘价**（前端用实时价现算）。

    窗口固定为 :data:`easy_tdx.web.returns.DEFAULT_WINDOWS`——列名与窗口一一对应
    （``web-ui/.../WatchlistView.vue`` 的 ``WINDOWS`` 必须与它同步）。

    锚定算法（详见 :mod:`easy_tdx.web.returns`）：``T`` = 上证指数日线（交易日历）
    中 ``<=`` 今天的最后一个交易日；``D_n`` = 日历中 ``T`` 往前 n 个交易日的日期；
    锚点 = 个股日线（``/bars`` 同款 QFQ，count=按窗口推导的 ``_BAR_COUNT``）中
    ``date <= D_n`` 的最后一根 bar。

    容错：今日非交易日 → ``T`` 自动回退；个股锚点日停牌 → 退到最近一根并回实际
    ``date``；数据不足（次新）→ ``anchors[].close`` 为 ``null``；长期停牌 → 回
    ``last_date`` + ``stale_days``；单只取数失败 → 该 key 只落 ``error``，整表照常
    返回。``last_close`` 供前端在没有实时报价时兜底算涨跌幅。

    性能：个股日线与交易日历都是进程内缓存（当日不变、次日失效），同一天重复拉
    零行情请求；日历缺今天（serve 盘前启动，今天的 bar 尚未生成）时会按
    :func:`_calendar_stale` 的间隔重取，避免 ``T`` 整体前移一个交易日且当天不自我
    纠正。个股取数并发 ≤ 4。
    """
    items = get_watchlist_store().list_all()
    if not items:
        return WatchlistReturnsResponse(trade_date=None, items={})

    today = _today()
    now = _now()
    calendar = await _trade_calendar(mac_client, client, today, now)
    trade_date = resolve_trade_date(calendar, today)
    if trade_date is None:
        raise TdxConnectionError("交易日历为空（上证指数日线获取失败），无法锚定近 N 日涨跌幅")

    day = today.isoformat()
    sem = asyncio.Semaphore(_CONCURRENCY)  # MAC 单连接本身串行，信号量做背压与秩序

    async def one(item: WatchItem) -> tuple[str, WatchReturnsItem]:
        async with sem:
            return item.symbol, await _returns_for(item, calendar, today, day, mac_client, client)

    pairs = await asyncio.gather(*(one(i) for i in items))
    return WatchlistReturnsResponse(trade_date=trade_date.isoformat(), items=dict(pairs))


@router.post("/watchlist", response_model=dict[str, object])
async def add_watch_item(req: WatchItemAdd) -> dict[str, object]:
    """加入自选（幂等：重复加入仅刷新名称）。"""
    item = get_watchlist_store().add(req.market, req.code, name=req.name, group=req.group)
    return {"ok": True, "item": item.to_dict()}


@router.delete("/watchlist/{market}/{code}", response_model=dict[str, object])
async def remove_watch_item(
    market: str,
    code: str = PathParam(..., pattern=_CODE_PATTERN, description="6位数字代码"),
) -> dict[str, object]:
    """移除自选。"""
    if market.upper() not in {"SZ", "SH", "BJ"}:
        raise HTTPException(status_code=400, detail=f"非法市场: {market}")
    removed = get_watchlist_store().remove(market, code)
    if not removed:
        raise HTTPException(status_code=404, detail=f"自选中不存在 {market}{code}")
    return {"ok": True}
