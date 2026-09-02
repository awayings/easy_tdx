"""批量交易计划：对 screen scan 信号股逐一出交易计划（P2-T3 批量模式）。

对每个策略信号（screen scan 输出的 signals JSON）调用交易计划生成器：
技术点位状态 + 市场情绪（批量预取一次）+ 公共舆情 → 止损/止盈点位 + 仓位。
可选钉钉推送汇总、可选落 decisions.db。

用法::

    .venv/bin/python scripts/trading_plan.py --signals ~/.easy_tdx/cache/signals_macd.json
    .venv/bin/python scripts/trading_plan.py \\
        --signals ~/.easy_tdx/cache/signals_macd.json --top 5 --notify --save
    .venv/bin/python scripts/trading_plan.py --symbol 002594 --table
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from easy_tdx import features as feature_lib  # noqa: E402
from easy_tdx.notify import notify  # noqa: E402
from easy_tdx.planner import build_full_plan  # noqa: E402
from easy_tdx.public_sentiment import fetch_hot_rank  # noqa: E402
from easy_tdx.sentiment import fetch_breadth, fetch_index_bars, score_market  # noqa: E402


def _load_signals(source: str) -> list[dict]:
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    return data.get("signals", [])


@click.command()
@click.option(
    "--signals", "signals_file", default=None, type=click.Path(path_type=Path),
    help="screen scan 信号 JSON",
)
@click.option(
    "--symbol", default=None,
    help="直接给单只标的（如 002594 / SZ 002594），忽略 --signals",
)
@click.option("--top", "top_n", default=10, type=int, help="只处理信号前 N 只（默认 10）")
@click.option("--base-position", default=0.10, type=float, help="基础仓位比例（默认 10%）")
@click.option("--notify", "notify_flag", is_flag=True, help="钉钉推送汇总")
@click.option("--save", is_flag=True, help="计划落 decisions.db")
@click.option("--table", is_flag=True, help="打印人类可读摘要而非 JSON")
def main(signals_file, symbol, top_n, base_position, notify_flag, save, table):
    if symbol:
        from easy_tdx.planner import parse_symbol

        market, code = parse_symbol(symbol)
        signals = [{"market": market, "code": code}]
    elif signals_file:
        signals = _load_signals(str(signals_file))
        if not signals:
            raise click.ClickException(f"信号文件无信号: {signals_file}")
    else:
        raise click.UsageError("须提供 --signals 或 --symbol")

    # 市场情绪批量预取一次（避免每股重复请求 market-stat + 指数日线）
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        breadth_f = pool.submit(fetch_breadth)
        index_f = pool.submit(fetch_index_bars, count=60)
        breadth, index_bars = breadth_f.result(), index_f.result()
    market_score = score_market(breadth, index_bars)
    click.echo(
        f"市场情绪：{market_score['label']}（{market_score['score']}/30）", err=True
    )

    # 人气榜与特征快照同样批量预取一次，复用给每股计划
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        rank_f = pool.submit(fetch_hot_rank)
        feat_f = pool.submit(feature_lib.snapshot)
        hot_rank, features = rank_f.result(), feat_f.result()

    plans = []
    for sig in signals[:top_n]:
        result = build_full_plan(
            sig["market"],
            sig["code"],
            base_position=base_position,
            market_score=market_score,
            hot_rank=hot_rank,
            features=features,
        )
        plans.append(result)
        if "error" in result:
            click.echo(f"[跳过] {sig['market']} {sig['code']}: {result['error']}", err=True)
            continue
        click.echo(f"\n{result['summary']}", err=False)
        if save:
            from easy_tdx.journal import add_decision

            add_decision(
                symbol=result["symbol"],
                plan=result,
                source="trading_plan",
                note="screen 信号批量计划",
            )

    if notify_flag:
        blocks = [
            f"市场情绪：{market_score['label']}（{market_score['score']}/30）\n"
        ]
        blocks += [
            p["summary"]
            for p in plans
            if "error" not in p
        ]
        notify("交易计划汇总·预警", "\n\n".join(blocks))

    if not table:
        print(
            json.dumps(
                [{k: v for k, v in p.items() if k != "summary"} for p in plans],
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )


if __name__ == "__main__":
    main()
