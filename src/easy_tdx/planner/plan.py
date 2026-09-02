"""交易计划纯计算（docs/trading_system_tasks.md P2-T3 核心）。

输入日线 df + 市场情绪评分 + 个股公共舆情 + 宏观特征快照，输出结构化交易计划：

- **风险评分**（0-100）：波动风险（ATR）+ 技术点位风险（距支撑距离/RSI/趋势）+
  舆情风险（人气榜排名/新闻情绪）+ 市场情绪风险（0-30 折 0-20），各因素可解释。
- **操作判定**（买入/等待/观望）：风险分 + 趋势 + 盈亏比三重门。
- **止损**：事件条件 + 具体点位。取"破位止损"（最近支撑 × 0.985）与
  "ATR 止损"（入场价 − 2.2×ATR）中先触发者（更高价），无支撑时用 ATR。
- **止盈**：多档点位 + 事件条件（最近阻力减半仓 → 第二阻力清仓）+ 移动止盈。
- **仓位**：基础仓位 × 风险系数 × 市场情绪系数 × 波动系数，单标的上限 20%。

纯函数、无网络/IO 依赖（可单测）；数据抓取在 ``easy_tdx.planner`` 包
``__init__`` 的 ``build_full_plan`` 里完成。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from easy_tdx.ta import analyze_technical_state
from easy_tdx.utils import round2 as _round2

# ---- 风险评分权重与阈值 ----
VOL_RISK_MAX = 15  # 波动（ATR%）风险上限
TECH_RISK_MAX = 40  # 技术点位风险上限
SENTI_RISK_MAX = 25  # 舆情风险上限
MARKET_RISK_MAX = 20  # 市场情绪风险上限（市场分 0-30 折 2/3）

# 止损/止盈参数
STOP_ATR_MULT = 2.2  # ATR 止损倍数
STOP_SUPPORT_BUFFER = 0.985  # 支撑位下方缓冲（1.5%）
TRAILING_MIN_PCT = 8.0  # 移动止盈最低回撤 %
TRAILING_ATR_MULT = 2.0  # 移动止盈 = max(8%, 2×ATR%)
RR_MIN = 1.2  # 第一止盈位盈亏比下限（不足则判定"等待"）

# 仓位参数
SINGLE_STOCK_CAP = 0.20  # 单标的上限 20%

_ACTION_TEXT = {
    "buy": "买入（可分批）",
    "wait": "等待（回踩/确认后再买）",
    "avoid": "观望（不建议买入）",
}


def _pct(a: float, b: float) -> float:
    return (b - a) / a * 100 if a > 0 else 0.0


def _risk_label(score: float) -> str:
    if score >= 75:
        return "高风险"
    if score >= 55:
        return "中高风险"
    if score >= 30:
        return "中风险"
    return "低风险"


# ---------------------------------------------------------------------------
# 风险评分
# ---------------------------------------------------------------------------


def compute_risk(
    technical: dict,
    public: dict | None = None,
    market_score: dict | None = None,
) -> dict:
    """综合风险评分 0-100（越高越不宜买入），各分量可解释。

    Args:
        technical: analyze_technical_state() 输出。
        public: analyze_stock_public() 输出（None=无舆情数据，舆情计 0 分）。
        market_score: score_market() 输出（None=无市场数据，市场计 0 分）。

    Returns:
        {score, label, components: {volatility, technical, sentiment, market},
         factors: [{name, points, note}]}
    """
    factors: list[dict] = []
    volatility = 0.0
    technical_pts = 0.0
    sentiment = 0.0
    market = 0.0

    # 1) 波动风险（ATR%）
    atr_pct = technical.get("atr14_pct")
    if atr_pct is not None and np.isfinite(atr_pct):
        if atr_pct > 6:
            volatility = 15.0
        elif atr_pct > 4:
            volatility = 10.0
        elif atr_pct > 3:
            volatility = 6.0
        elif atr_pct > 2:
            volatility = 3.0
        if volatility:
            factors.append(
                {"name": "波动", "points": volatility, "note": f"ATR 日波动 {atr_pct:.1f}%"}
            )
    volatility = min(volatility, VOL_RISK_MAX)

    # 2) 技术点位风险
    supports = technical.get("support_resistance", {}).get("supports", [])
    dist_sup = supports[0]["distance_pct"] if supports else None
    if dist_sup is not None:
        if dist_sup > 12:
            pts, note = 20.0, f"距最近支撑 {dist_sup:.1f}%（>12%，下方无依托）"
        elif dist_sup > 8:
            pts, note = 14.0, f"距最近支撑 {dist_sup:.1f}%（8-12%）"
        elif dist_sup > 5:
            pts, note = 8.0, f"距最近支撑 {dist_sup:.1f}%（5-8%）"
        elif dist_sup > 2:
            pts, note = 3.0, f"距最近支撑 {dist_sup:.1f}%（2-5%）"
        else:
            pts, note = 0.0, f"贴近支撑（{dist_sup:.1f}%），回踩止损位近"
        technical_pts += pts
        if pts:
            factors.append({"name": "距支撑", "points": pts, "note": note})
    else:
        technical_pts += 20.0
        factors.append({"name": "距支撑", "points": 20.0, "note": "无有效支撑位"})

    rsi = technical.get("rsi14")
    if rsi is not None and np.isfinite(rsi):
        if rsi >= 75:
            technical_pts += 8.0
            factors.append({"name": "RSI", "points": 8.0, "note": f"RSI {rsi:.0f} 严重超买"})
        elif rsi >= 65:
            technical_pts += 4.0
            factors.append({"name": "RSI", "points": 4.0, "note": f"RSI {rsi:.0f} 偏热"})
        elif rsi <= 25:
            technical_pts += 4.0
            note = f"RSI {rsi:.0f} 超卖（下跌趋势中勿接飞刀）"
            factors.append({"name": "RSI", "points": 4.0, "note": note})

    trend = technical.get("trend")
    if trend == "下降趋势":
        technical_pts += 8.0
        factors.append({"name": "趋势", "points": 8.0, "note": "MA 空头排列（下降趋势）"})
    elif trend == "震荡":
        technical_pts += 3.0
        factors.append({"name": "趋势", "points": 3.0, "note": "均线纠缠（震荡）"})

    price_vs_ma20 = technical.get("price_vs_ma20_pct")
    if price_vs_ma20 is not None and price_vs_ma20 < 0:
        technical_pts += 4.0
        factors.append(
            {"name": "MA20", "points": 4.0, "note": f"价格位于 MA20 下方 {abs(price_vs_ma20):.1f}%"}
        )
    technical_pts = min(technical_pts, TECH_RISK_MAX)

    # 3) 舆情风险
    if public and public.get("available"):
        rank = public.get("hot_rank")
        if rank is not None:
            if rank <= 20:
                pts, note = 15.0, f"人气榜第 {rank} 名（情绪亢奋，追高风险）"
            elif rank <= 50:
                pts, note = 10.0, f"人气榜第 {rank} 名（关注度高）"
            else:
                pts, note = 6.0, f"人气榜第 {rank} 名"
            sentiment += pts
            factors.append({"name": "人气榜", "points": pts, "note": note})
            rc = public.get("hot_rank_change")
            if rc is not None and rc > 10:
                sentiment += 5.0
                note = f"排名较昨日上升 {rc} 位"
                factors.append({"name": "人气上升", "points": 5.0, "note": note})
        n3 = public.get("news_articles_3d") or 0
        if n3 >= 10:
            sentiment += 5.0
            note = f"近 3 日新闻 {n3} 条（不确定性高）"
            factors.append({"name": "消息密集", "points": 5.0, "note": note})
        ns = public.get("news_sentiment")
        if ns is not None and ns <= -0.3:
            sentiment += 5.0
            note = f"近期新闻偏负面（情绪分 {ns}）"
            factors.append({"name": "新闻情绪", "points": 5.0, "note": note})
    sentiment = min(sentiment, SENTI_RISK_MAX)

    # 4) 市场情绪风险（0-30 折 0-20）
    if market_score and market_score.get("score") is not None:
        market = round(market_score["score"] * 2 / 3, 1)
        if market:
            factors.append(
                {
                    "name": "市场情绪",
                    "points": market,
                    "note": f"市场 {market_score.get('label')}（{market_score['score']}/30）",
                }
            )
    market = min(market, MARKET_RISK_MAX)

    score = round(volatility + technical_pts + sentiment + market, 1)
    score = min(score, 100.0)
    return {
        "score": score,
        "label": _risk_label(score),
        "components": {
            "volatility": volatility,
            "technical": technical_pts,
            "sentiment": sentiment,
            "market": market,
        },
        "factors": factors,
    }


# ---------------------------------------------------------------------------
# 仓位
# ---------------------------------------------------------------------------


def compute_position(
    risk_score: float,
    market_score: dict | None,
    atr_pct: float | None,
    base_position: float = 0.10,
) -> dict:
    """仓位建议：基础仓位 × 风险系数 × 市场系数 × 波动系数，上限 20%。

    Returns:
        {pct, notes}；风险 ≥75 或判定观望时 pct=0。
    """
    notes: list[str] = []
    pct = base_position

    if risk_score < 30:
        mult, notes_ = 1.0, []
    elif risk_score < 55:
        mult, notes_ = 0.7, [f"风险系数 0.7（风险分 {risk_score:.0f}）"]
    elif risk_score < 75:
        mult, notes_ = 0.4, [f"风险系数 0.4（风险分 {risk_score:.0f}）"]
    else:
        mult, notes_ = 0.0, [f"风险分 {risk_score:.0f} ≥75，不开仓"]
    pct *= mult
    notes.extend(notes_)

    if market_score and market_score.get("label") == "偏空":
        pct *= 0.5
        notes.append("市场偏空，仓位减半")

    if atr_pct is not None and np.isfinite(atr_pct) and atr_pct > 5:
        pct *= 0.7
        notes.append(f"高波动（ATR {atr_pct:.1f}%），仓位再 ×0.7")

    pct = min(pct, SINGLE_STOCK_CAP)
    pct = round(pct * 50) / 50  # 取整到 2% 的倍数
    return {"pct": max(pct, 0.0), "notes": notes}


# ---------------------------------------------------------------------------
# 操作判定
# ---------------------------------------------------------------------------


def decide_verdict(risk: dict, technical: dict, rr1: float | None) -> tuple[str, list[str]]:
    """三重门判定：风险分 → 趋势 → 盈亏比。

    Returns:
        (verdict, notes)，verdict ∈ {buy, wait, avoid}。
    """
    notes: list[str] = []
    verdict = "buy"
    if risk["score"] >= 75:
        verdict = "avoid"
        notes.append(f"风险分 {risk['score']:.0f} ≥75，风险收益比不值得参与")
    elif risk["score"] >= 55:
        verdict = "wait"
        notes.append(f"风险分 {risk['score']:.0f} 偏高，等待回踩支撑或情绪修复")
    elif technical.get("trend") == "下降趋势" and technical.get("price_vs_ma20_pct", 0) < 0:
        verdict = "wait"
        notes.append("下降趋势且位于 MA20 下方，勿接飞刀")
    if rr1 is not None and rr1 < RR_MIN and verdict == "buy":
        verdict = "wait"
        notes.append(f"第一止盈位盈亏比 {rr1} < {RR_MIN}，当前点位追入不划算")
    return verdict, notes


# ---------------------------------------------------------------------------
# 交易计划
# ---------------------------------------------------------------------------


def build_plan(
    df: pd.DataFrame,
    *,
    symbol: str = "",
    name: str = "",
    entry_ref: float | None = None,
    entry_ref_is_realtime: bool | None = None,
    market_score: dict | None = None,
    public: dict | None = None,
    features: dict | None = None,
    base_position: float = 0.10,
) -> dict:
    """生成交易计划。

    Args:
        df: 日线 DataFrame（datetime/open/high/low/close/vol/amount）。
        symbol: 展示用代码（如 "SZ 002594"）。
        name: 股票名称（可选，仅展示）。
        entry_ref: 入场参考价（默认最后一根 bar 收盘；盘中可用实时报价）。
        entry_ref_is_realtime: entry_ref 是否盘中实时价（None=按 df 推断）。
        market_score: score_market() 输出。
        public: analyze_stock_public() 输出。
        features: 宏观特征快照（features.snapshot()，仅随计划附带给 LLM/复盘）。
        base_position: 基础仓位比例（0-1，默认 10%）。

    Returns:
        完整计划 dict：{symbol, date, entry_ref, technical, risk, verdict,
        entry, stop_loss, take_profit, trailing_stop, position, risk_reward,
        summary, features}。
    """
    technical = analyze_technical_state(df)
    close = technical.get("close")
    if technical.get("error") or close is None:
        return {"symbol": symbol, "error": technical.get("error", "无日线数据")}
    ref = float(entry_ref) if entry_ref and np.isfinite(entry_ref) else float(close)
    if entry_ref_is_realtime is None:
        entry_ref_is_realtime = bool(entry_ref is not None and entry_ref != close)

    risk = compute_risk(technical, public, market_score)

    sr = technical.get("support_resistance", {})
    supports = sr.get("supports", [])
    resistances = sr.get("resistances", [])
    atr = technical.get("atr14")
    atr_pct = technical.get("atr14_pct")

    # ---- 止损：破位止损与 ATR 止损取先触发者（更高价）----
    if supports:
        support_stop = supports[0]["price"] * STOP_SUPPORT_BUFFER
    else:
        support_stop = None
    atr_stop = ref - STOP_ATR_MULT * atr if atr and np.isfinite(atr) else None
    if support_stop is not None and atr_stop is not None:
        stop = max(support_stop, atr_stop)  # 取先触发者（更高价）
    else:
        stop = support_stop if support_stop is not None else atr_stop
    stop_basis = "支撑破位" if stop == support_stop else "ATR"
    stop_cond = (
        f"收盘价跌破 {stop:.2f} 元止损离场；盘中跌破 30 分钟不收回亦触发；"
        "若低开跳空跌破直接市价止损"
        if stop is not None
        else "数据不足无法给出止损位，请以 -5% 硬止损"
    )
    stop_loss = {
        "price": _round2(stop),
        "pct": round(-abs(_pct(ref, stop)), 2) if stop is not None else None,
        "condition": stop_cond,
        "basis": stop_basis,
    }

    # ---- 止盈：最近阻力减半仓 → 第二阻力清仓 ----
    r1 = resistances[0] if resistances else None
    if len(resistances) >= 2:
        r2_price = resistances[1]["price"]
    elif r1 is not None:
        r2_price = r1["price"] + (r1["price"] - ref) * 1.5
    else:
        r2_price = None
    take_profit: list[dict] = []
    if r1 is not None:
        take_profit.append(
            {
                "tier": 1,
                "price": r1["price"],
                "pct": round(_pct(ref, r1["price"]), 2),
                "type": r1["type"],
                "action": "减半仓",
                "condition": (
                    f"冲高至 {r1['price']:.2f} 元附近滞涨（长上影/放量不涨/30 分钟不过）减仓一半；"
                    f"若放量收盘站上 {r1['price']:.2f} 元则剩余仓位看高一线"
                ),
            }
        )
    if r2_price is not None:
        take_profit.append(
            {
                "tier": 2,
                "price": _round2(r2_price),
                "pct": round(_pct(ref, r2_price), 2),
                "type": "扩展位",
                "action": "清仓",
                "condition": (
                    f"到达 {r2_price:.2f} 元清仓落袋；若市场情绪亢奋且放量突破，"
                    "可留 1/3 仓改用移动止盈"
                ),
            }
        )

    # ---- 移动止盈 ----
    trail_pct = max(TRAILING_MIN_PCT, (TRAILING_ATR_MULT * atr_pct) if atr_pct else 0.0)
    trail_pct = min(round(trail_pct, 1), 15.0)
    trailing_stop = {
        "pct": trail_pct,
        "condition": f"持仓期间自最高价回撤 {trail_pct:.1f}% 止盈离场（收盘确认）",
    }

    # ---- 入场（按形态：回踩型/突破型/均线型）----
    dist_sup = supports[0]["distance_pct"] if supports else None
    dist_res = resistances[0]["distance_pct"] if resistances else None
    ma20 = technical.get("ma20")
    broke20 = technical.get("broke_20d_high")
    if dist_sup is not None and dist_sup <= 3 and supports:
        entry = {
            "style": "回踩型",
            "zone": [supports[0]["price"], round(ref * 1.01, 2)],
            "condition": (
                f"回踩支撑 {supports[0]['price']:.2f} 元附近企稳（缩量十字星/下影线，"
                f"30 分钟不再新低）分批买入，"
                f"跌破 {supports[0]['price'] * STOP_SUPPORT_BUFFER:.2f} 元放弃"
            ),
        }
    elif (dist_res is not None and dist_res <= 2) or broke20:
        target = resistances[0]["price"] if resistances else ref * 1.03
        entry = {
            "style": "突破型",
            "zone": [target, round(target * 1.02, 2)],
            "condition": (
                f"放量（量比 >1.5）突破 {target:.2f} 元且 30 分钟站稳确认后买入，"
                "假突破（冲高回落破位）放弃"
            ),
        }
    else:
        zone_lo, zone_hi = sorted(
            [round(ma20, 2) if ma20 else round(ref, 2), round(ref, 2)]
        )
        entry = {
            "style": "均线型",
            "zone": [zone_lo, zone_hi],
            "condition": (
                f"回踩 MA20（{ma20:.2f} 元）附近不破买入；若直接高开 >3% 不追，等回踩"
                if ma20
                else "无明确支撑，等待回踩前低或放量突破再介入"
            ),
        }

    # ---- 盈亏比与判定 ----
    if stop and r1:
        rr1 = round((r1["price"] - ref) / (ref - stop), 2)
    else:
        rr1 = None
    if stop and r2_price:
        rr2 = round((r2_price - ref) / (ref - stop), 2)
    else:
        rr2 = None
    risk_reward = {"rr1": rr1, "rr2": rr2}

    verdict, verdict_notes = decide_verdict(risk, technical, rr1)
    verdict_text = _ACTION_TEXT[verdict]

    position = compute_position(risk["score"], market_score, atr_pct, base_position)
    if verdict == "avoid":
        position = {"pct": 0.0, "notes": position["notes"] + ["判定观望，不开仓"]}

    # ---- 人类可读摘要 ----
    summary_lines = [f"【交易计划】{symbol}{' ' + name if name else ''}"]
    ref_src = '盘中实时' if entry_ref_is_realtime else '最新收盘'
    summary_lines.append(f"时间：{technical.get('date')}，参考价 {ref:.2f} 元（{ref_src}）")
    if market_score:
        summary_lines.append(
            f"市场情绪：{market_score.get('label')}（{market_score.get('score')}/30），"
            + ("；".join(f["note"] for f in market_score.get("factors", [])[:2]) or "无显著信号")
        )
    elif market_score is None:
        summary_lines.append("市场情绪：无数据（离线）")
    summary_lines.append(
        f"技术状态：{technical.get('trend')}，"
        f"RSI {technical.get('rsi14')} {technical.get('rsi_state')}；"
        f"支撑 {supports[0]['price'] if supports else '—'}，"
        f"阻力 {resistances[0]['price'] if resistances else '—'}"
    )
    if public and public.get("available"):
        parts = []
        if public.get("hot_rank"):
            parts.append(f"人气榜第 {public['hot_rank']} 名")
        parts.append(
            f"近 3 日新闻 {public.get('news_articles_3d', 0)} 条"
            + (
                f"（情绪 {public.get('news_sentiment'):+.2f}）"
                if public.get("news_sentiment") is not None
                else ""
            )
        )
        summary_lines.append("舆情：" + "；".join(parts))
    comp = risk["components"]
    summary_lines.append(
        f"风险度：{risk['score']:.0f}/100（{risk['label']}）——波动 {comp['volatility']:.0f} + "
        f"技术 {comp['technical']:.0f} + 舆情 {comp['sentiment']:.0f} + 市场 {comp['market']:.0f}"
    )
    verdict_note = f"——{'；'.join(verdict_notes)}" if verdict_notes else ""
    summary_lines.append(f"判定：{verdict_text}{verdict_note}")
    summary_lines.append(f"入场（{entry['style']}）：{entry['condition']}")
    summary_lines.append(f"止损：{stop_loss['condition']}")
    for tp in take_profit:
        summary_lines.append(
            f"止盈{tp['tier']}（{tp['action']}）：{tp['price']:.2f} 元"
            f"（{tp['pct']:+.2f}%，{tp['type']}）——{tp['condition']}"
        )
    summary_lines.append(f"移动止盈：{trailing_stop['condition']}")
    pos_notes = "；".join(position["notes"]) if position["notes"] else "无修正"
    summary_lines.append(f"仓位：{position['pct']:.0f}%（基础 {base_position:.0%}；{pos_notes}）")
    if rr1 is not None:
        summary_lines.append(f"盈亏比：第一止盈位 {rr1}，第二止盈位 {rr2}")

    return {
        "symbol": symbol,
        "date": technical.get("date"),
        "entry_ref": _round2(ref),
        "entry_ref_is_realtime": entry_ref_is_realtime,
        "technical": technical,
        "risk": risk,
        "verdict": verdict,
        "verdict_text": verdict_text,
        "verdict_notes": verdict_notes,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "trailing_stop": trailing_stop,
        "position": position,
        "risk_reward": risk_reward,
        "summary": "\n".join(summary_lines),
        "features": features,
    }
