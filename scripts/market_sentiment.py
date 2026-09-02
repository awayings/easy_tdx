"""市场情绪日频快照与日报（docs/trading_system_tasks.md P0-T1/P0-T2）。

收盘后产出 a.0 情绪快照 JSON 落 ``~/.easy_tdx/cache/``，可选钉钉日报。

数据来源（全部 easy_tdx 原生，零新数据源）：
- `market-stat`：全市场涨跌家数、涨停/跌停数、两市成交额
- 指数日线 amount：上证 000001.SH + 深市全市场 399107.SZ（勿用 399001.SZ）
- MAC `board-ranking`：行业板块涨跌幅/主力净流入流出榜（P0-T2）
- `easy_tdx.features.snapshot()`：汇率/商品/国债/流动性/宏观/国际指数最新值

快照结构：{date, breadth, index, board_ranking, features, market_score}。
``--report`` 发钉钉日报（复用 easy_tdx.notify，正文恒含"预警"关键词）。

用法::

    .venv/bin/python scripts/market_sentiment.py                 # 快照落盘 + 打印摘要
    .venv/bin/python scripts/market_sentiment.py --report        # 追加钉钉日报
    .venv/bin/python scripts/market_sentiment.py --score-only    # 只打印市场评分 JSON
    .venv/bin/python scripts/market_sentiment.py --date 20260902 # 快照标注日期
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
from datetime import datetime
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from easy_tdx import config as _config  # noqa: E402
from easy_tdx import features as feature_lib  # noqa: E402
from easy_tdx.notify import notify  # noqa: E402
from easy_tdx.sentiment import (  # noqa: E402
    build_snapshot,
    fetch_board_ranking,
    fetch_breadth,
    fetch_index_bars,
)

CACHE_DIR = _config.config_dir() / "cache"


def dated_snapshot_path(date: str | None = None) -> Path:
    return CACHE_DIR / f"sentiment_{date or datetime.now().strftime('%Y%m%d')}.json"


def latest_snapshot_path() -> Path:
    return CACHE_DIR / "sentiment_latest.json"


def collect(date: str | None = None) -> dict:
    """抓取全部情绪数据并组装快照（单项失败自动降级为 None）。

    四路数据源相互独立（TDX 宽度 / MAC 指数 / MAC 板块 / 本地特征 CSV），
    并发抓取避免串行延迟叠加（日报路径约 3 次网络往返）。
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        breadth_f = pool.submit(fetch_breadth)
        index_f = pool.submit(fetch_index_bars, count=60)
        board_f = pool.submit(fetch_board_ranking, "HY", 10)
        features_f = pool.submit(feature_lib.snapshot)
        breadth = breadth_f.result()
        index = index_f.result()
        board = board_f.result()
        features = features_f.result()
    return build_snapshot(breadth=breadth, index=index, board=board, features=features, date=date)


def format_report(snap: dict) -> str:
    """快照 → 钉钉日报正文。"""
    ms = snap["market_score"]
    lines = [f"市场情绪：{ms['label']}（风险分 {ms['score']}/30）"]
    for f in ms.get("factors", []):
        lines.append(f"- {f['note']}")
    if ms.get("turnover_2m"):
        lines.append(f"- 两市成交额 {ms['turnover_2m'] / 1e8:.0f} 亿")
    if snap.get("index"):
        idx = snap["index"]
        lines.append(f"- 上证指数 {idx['sh_close']}（{idx['sh_change_pct']:+.2f}%）")
    if snap.get("breadth"):
        b = snap["breadth"]
        lines.append(
            f"- 涨跌 {b['up_count']}/{b['down_count']}，"
            f"涨停 {b['limit_up_count']}，跌停 {b['limit_down_count']}"
        )
    if snap.get("board_ranking"):
        br = snap["board_ranking"]
        inflow = "、".join(f"{r.get('name', '')}" for r in br.get("inflow", [])[:5])
        outflow = "、".join(f"{r.get('name', '')}" for r in br.get("outflow", [])[:5])
        if inflow:
            lines.append(f"- 板块净流入榜：{inflow}")
        if outflow:
            lines.append(f"- 板块净流出榜：{outflow}")
    # 正文恒含"预警"关键词（钉钉机器人关键词要求，见 CLAUDE.md）
    lines.append(f"预警项：{len(ms.get('factors', []))} 条")
    return "\n".join(lines)


@click.command()
@click.option("--report", is_flag=True, help="快照落盘后追加钉钉日报推送")
@click.option("--score-only", is_flag=True, help="只打印市场评分 JSON，不落盘")
@click.option("--date", "target_date", default=None, help="快照标注日期 YYYY-MM-DD（默认今天）")
def main(report: bool, score_only: bool, target_date: str | None) -> None:
    snap = collect(date=target_date)
    ms = snap["market_score"]
    if score_only:
        print(json.dumps(ms, ensure_ascii=False, indent=2))
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for p in (dated_snapshot_path(target_date), latest_snapshot_path()):
        p.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[情绪快照] {dated_snapshot_path(target_date)}", flush=True)
    print(json.dumps(ms, ensure_ascii=False), flush=True)
    if report:
        title = f"市场情绪日报·{ms['label']}"
        notify(title, format_report(snap))


if __name__ == "__main__":
    main()
