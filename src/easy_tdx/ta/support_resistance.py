"""支撑/阻力与筹码分布近似（docs/trading_system_tasks.md P0-T4）。

输入日线 DataFrame（datetime/open/high/low/close/vol/amount，与 screen scanner
的 ``_bars_to_df`` 输出同构），输出：

- ``chip_distribution``：近 250 日成交量加权成本分布。协议无筹码分布数据，
  按三角分布近似——每根 bar 的成交量以 (H+L+C)/3 为峰摊入 [low, high]
  价格区间；返回直方图、获利盘比例与筹码峰（成交密集区）。
- ``find_support_resistance``：候选支撑/阻力（前高前低 fractal、筹码峰、
  MA20/60、布林带、20/60 日高低点），按价格容差聚类去重后输出
  ``{supports, resistances}``，每项带 price/type/strength/distance_pct。
- ``analyze_technical_state``：技术点位状态（趋势、RSI、MACD、ATR 波动、
  量比、距支撑阻力距离、放量突破），供交易计划生成器做风险评分与点位选取。

纯函数、无网络依赖；指标复用 MyTT（RSI/MACD/BOLL/ATR，与通达信口径一致）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from easy_tdx.MyTT import ATR, BOLL, MA, MACD, RSI

# 计算筹码分布与支撑阻力的日线回溯窗口（约一年交易日）。
CHIP_WINDOW = 250
# 筹码峰阈值：峰成交量占总成交量比例下限（3 根 bar 平滑后，低于此视为噪声）。
CHIP_PEAK_MIN_RATIO = 0.008
# 最多保留的筹码峰个数（按峰成交量降序）。
CHIP_PEAK_MAX = 8
# 支撑/阻力聚类容差（相对价格 %）。
CLUSTER_TOLERANCE_PCT = 1.5
# fractal 摆动点窗口：高/低点须是前后各 w 根 bar 的极值。
SWING_WINDOW = 5


def _arrays(df: pd.DataFrame) -> dict[str, np.ndarray]:
    """DataFrame → numpy 数组（close/high/low/vol 为 float64）。"""
    return {
        "close": np.asarray(df["close"], dtype=float),
        "high": np.asarray(df["high"], dtype=float),
        "low": np.asarray(df["low"], dtype=float),
        "vol": np.asarray(df["vol"], dtype=float),
    }


def _round2(x: float | None) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 2)


# ---------------------------------------------------------------------------
# 筹码分布（成交量加权成本分布，三角近似）
# ---------------------------------------------------------------------------


def chip_distribution(df: pd.DataFrame, window: int = CHIP_WINDOW, bins: int = 120) -> dict:
    """近 window 日成交量加权成本分布。

    Returns:
        {bin_centers, volumes, total_volume, profit_ratio, peaks}，
        peaks 为 [{price, ratio}]（ratio=峰成交量/总成交量），按峰成交量降序。
    """
    sub = df.tail(window)
    if sub.empty:
        empty = {
            "bin_centers": [], "volumes": [], "total_volume": 0.0,
            "profit_ratio": None, "peaks": [],
        }
        return empty
    a = _arrays(sub)
    low, high, close, vol = a["low"], a["high"], a["close"], a["vol"]

    lo, hi = float(np.nanmin(low)), float(np.nanmax(high))
    if hi <= lo:  # 单一定价（如连续一字板），扩展出小区间避免退化
        hi = lo + max(lo * 0.01, 0.01)
    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    profile = np.zeros(bins)

    for i in range(len(sub)):
        bar_low, h, c, v = low[i], high[i], close[i], vol[i]
        if not np.isfinite(v) or v <= 0:
            continue
        peak = (bar_low + h + c) / 3  # 三角分布峰值 = 典型价
        half = max((h - bar_low) / 2, 1e-9)
        w = np.maximum(0.0, 1 - np.abs(centers - peak) / half)
        if w.sum() <= 0:
            profile[np.argmin(np.abs(centers - peak))] += v
        else:
            profile += w / w.sum() * v

    total = float(profile.sum())
    if total <= 0:
        empty = {
            "bin_centers": [], "volumes": [], "total_volume": 0.0,
            "profit_ratio": None, "peaks": [],
        }
        return empty
    profit = float(profile[centers <= close[-1]].sum() / total)

    # 筹码峰：3 根 bar 平滑后取局部极大值且占比达标（峰值叠加了噪声，不平滑
    # 单 bin 占比被三角分布摊薄，实测 4% 阈值会漏掉所有筹码峰）
    smooth = np.convolve(profile, np.ones(3) / 3, mode="same")
    peaks: list[dict] = []
    n = len(smooth)
    for i in range(1, n - 1):
        if smooth[i] > smooth[i - 1] and smooth[i] >= smooth[i + 1]:
            ratio = float(smooth[i] / total)
            if ratio >= CHIP_PEAK_MIN_RATIO:
                peaks.append({"price": round(float(centers[i]), 2), "ratio": round(ratio, 4)})
    peaks.sort(key=lambda p: p["ratio"], reverse=True)
    peaks = peaks[:CHIP_PEAK_MAX]

    return {
        "bin_centers": [round(float(c), 2) for c in centers],
        "volumes": [round(float(v), 2) for v in profile],
        "total_volume": round(total, 2),
        "profit_ratio": round(profit, 4),
        "peaks": peaks,
    }


# ---------------------------------------------------------------------------
# 支撑/阻力候选与聚类
# ---------------------------------------------------------------------------


def _swing_levels(high: np.ndarray, low: np.ndarray, lookback: int = 150) -> tuple[list, list]:
    """fractal 摆动高/低点：前/后各 SWING_WINDOW 根 bar 的极值，取近 lookback 根。"""
    n = len(high)
    w = SWING_WINDOW
    start = max(w, n - lookback)
    highs, lows = [], []
    for i in range(start, n - w):
        if high[i] == np.nanmax(high[i - w : i + w + 1]):
            highs.append(float(high[i]))
        if low[i] == np.nanmin(low[i - w : i + w + 1]):
            lows.append(float(low[i]))
    return highs, lows


def _cluster_levels(candidates: list[dict], ref_price: float, tol_pct: float) -> list[dict]:
    """按价格容差聚类候选点位：组内取最强 strength，type 合并去重。"""
    tol = ref_price * tol_pct / 100
    groups: list[dict] = []
    for c in sorted(candidates, key=lambda x: x["price"]):
        for g in groups:
            if abs(c["price"] - g["price"]) <= tol:
                g["strength"] = round(max(g["strength"], c["strength"]), 2)
                if c["type"] not in g["type"]:
                    g["type"] = f"{g['type']}+{c['type']}"
                break
        else:
            groups.append({"price": c["price"], "type": c["type"], "strength": c["strength"]})
    return groups


def find_support_resistance(
    df: pd.DataFrame,
    window: int = CHIP_WINDOW,
    max_levels: int = 5,
    tol_pct: float = CLUSTER_TOLERANCE_PCT,
) -> dict:
    """找支撑/阻力位。

    Returns:
        {supports: [{price, type, strength, distance_pct}]（价格降序，最近的在最前）,
         resistances: [...]（价格升序）}；数据不足时返回空列表。
    """
    if len(df) < 30:
        return {"supports": [], "resistances": []}
    a = _arrays(df.tail(window))
    close, high, low = a["close"], a["high"], a["low"]
    price = float(close[-1])

    supports: list[dict] = []
    resistances: list[dict] = []

    def add(level: float, typ: str, strength: float) -> None:
        if not np.isfinite(level) or level <= 0:
            return
        # 与现价重合（±0.5%）的位不构成支撑/阻力（盘中进行中 bar 的
        # 当日高低点常与现价重合，直接采纳会产出 0% 距离的退化止盈位）
        if abs(level / price - 1) < 0.005:
            return
        item = {"price": round(level, 2), "type": typ, "strength": round(float(strength), 2)}
        (supports if level < price else resistances).append(item)

    # 1) 筹码峰（成交密集区）
    chip = chip_distribution(df, window=window)
    for p in chip["peaks"]:
        add(p["price"], "筹码峰", min(1.0, p["ratio"] * 12))

    # 2) 前高/前低（fractal）
    swing_highs, swing_lows = _swing_levels(high, low)
    for h in swing_highs:
        if h > price:
            add(h, "前高", 0.6)
    for swl in swing_lows:
        if swl < price:
            add(swl, "前低", 0.6)

    # 3) 均线（动态支撑/阻力）
    ma20 = float(MA(close, 20)[-1]) if len(close) >= 20 else np.nan
    ma60 = float(MA(close, 60)[-1]) if len(close) >= 60 else np.nan
    add(ma20, "MA20", 0.5)
    add(ma60, "MA60", 0.5)

    # 4) 布林带
    if len(close) >= 20:
        upper, _, lower = BOLL(close, 20, 2)
        add(float(upper[-1]), "布林上轨", 0.4)
        add(float(lower[-1]), "布林下轨", 0.4)

    # 5) 20/60 日高低点（突破与回踩的关键位）
    if len(high) >= 20:
        add(float(np.nanmax(high[-20:])), "20日高点", 0.55)
        add(float(np.nanmin(low[-20:])), "20日低点", 0.55)
    if len(high) >= 60:
        add(float(np.nanmax(high[-60:])), "60日高点", 0.5)
        add(float(np.nanmin(low[-60:])), "60日低点", 0.5)

    sups = _cluster_levels(supports, price, tol_pct)
    ress = _cluster_levels(resistances, price, tol_pct)
    sups.sort(key=lambda x: x["price"], reverse=True)
    ress.sort(key=lambda x: x["price"])

    for item in sups:
        item["distance_pct"] = round((price - item["price"]) / price * 100, 2)
    for item in ress:
        item["distance_pct"] = round((item["price"] - price) / price * 100, 2)

    return {"supports": sups[:max_levels], "resistances": ress[:max_levels]}


# ---------------------------------------------------------------------------
# 技术点位状态
# ---------------------------------------------------------------------------


def analyze_technical_state(df: pd.DataFrame) -> dict:
    """个股技术点位状态：趋势、动量指标、波动、距支撑阻力距离、突破状态。

    数据不足 30 根 bar 时返回仅含 close/date 与 error 字段的最小结构。
    """
    result: dict = {
        "close": _round2(float(df["close"].iloc[-1])) if not df.empty else None,
        "date": str(df["datetime"].iloc[-1].date()) if not df.empty else None,
    }
    if len(df) < 30:
        result["error"] = f"日线数据不足（{len(df)} 根 < 30）"
        return result

    a = _arrays(df)
    close, high, low, vol = a["close"], a["high"], a["low"], a["vol"]
    price = float(close[-1])

    ma5 = float(MA(close, 5)[-1])
    ma20 = float(MA(close, 20)[-1])
    ma60 = float(MA(close, 60)[-1]) if len(close) >= 60 else np.nan
    if ma5 > ma20 and (np.isnan(ma60) or ma20 > ma60):
        trend = "上升趋势"
    elif ma5 < ma20 and (np.isnan(ma60) or ma20 < ma60):
        trend = "下降趋势"
    else:
        trend = "震荡"

    rsi = float(RSI(close, 14)[-1]) if len(close) >= 20 else np.nan
    rsi_state = "超买" if rsi >= 70 else ("超卖" if rsi <= 30 else "中性")

    dif, dea, hist = MACD(close)
    atr14 = float(ATR(close, high, low, 14)[-1]) if len(close) >= 15 else np.nan
    atr_pct = atr14 / price * 100 if np.isfinite(atr14) and price > 0 else np.nan

    prev_vol = np.nanmean(vol[-21:-1]) if len(vol) >= 21 else np.nan
    vol_ratio = float(vol[-1] / prev_vol) if np.isfinite(prev_vol) and prev_vol > 0 else np.nan

    high20 = float(np.nanmax(high[-20:]))
    low20 = float(np.nanmin(low[-20:]))
    high60 = float(np.nanmax(high[-60:])) if len(high) >= 60 else np.nan
    low60 = float(np.nanmin(low[-60:])) if len(low) >= 60 else np.nan

    sr = find_support_resistance(df)
    near_support = sr["supports"][0] if sr["supports"] else None
    near_resistance = sr["resistances"][0] if sr["resistances"] else None

    result.update(
        {
            "trend": trend,
            "ma5": _round2(ma5),
            "ma20": _round2(ma20),
            "ma60": _round2(ma60),
            "price_vs_ma20_pct": round((price - ma20) / ma20 * 100, 2),
            "price_vs_ma60_pct": round((price - ma60) / ma60 * 100, 2)
            if np.isfinite(ma60)
            else None,
            "rsi14": _round2(rsi),
            "rsi_state": rsi_state,
            "macd_dif": _round2(float(dif[-1])),
            "macd_dea": _round2(float(dea[-1])),
            "macd_hist": _round2(float(hist[-1])),
            "atr14": _round2(atr14),
            "atr14_pct": _round2(atr_pct),
            "vol_ratio": _round2(vol_ratio),
            "high_20d": _round2(high20),
            "low_20d": _round2(low20),
            "high_60d": _round2(high60),
            "low_60d": _round2(low60),
            "broke_20d_high": bool(price > high20),
            "broke_60d_high": bool(np.isfinite(high60) and price > high60),
            "near_support": near_support,
            "near_resistance": near_resistance,
            "support_resistance": sr,
        }
    )
    return result
