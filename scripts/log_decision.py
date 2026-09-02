"""决策日志读写（docs/trading_system_tasks.md P1-T5）。

把 ``easy-tdx plan`` 输出的计划 JSON 落 decisions.db，或列出历史决策。

用法::

    .venv/bin/easy-tdx plan 002594 > plan.json
    .venv/bin/python scripts/log_decision.py plan.json --source "manual"
    .venv/bin/python scripts/log_decision.py --list            # 最近 20 条
    .venv/bin/python scripts/log_decision.py --list --symbol "SZ 002594"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from easy_tdx.journal import add_decision, list_decisions  # noqa: E402


@click.command()
@click.argument("plan_file", required=False, type=click.Path(path_type=Path))
@click.option("--source", default="log_decision", help="来源标记")
@click.option("--note", default="", help="备注")
@click.option("--list", "list_mode", is_flag=True, help="列出最近决策（不写库）")
@click.option("--symbol", default=None, help="按标的过滤（--list 时生效）")
@click.option("--limit", default=20, type=int, help="--list 条数")
def main(plan_file, source, note, list_mode, symbol, limit):
    if list_mode:
        rows = list_decisions(limit=limit, symbol=symbol)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if plan_file is None:
        raise click.UsageError("须提供计划 JSON 文件或 --list")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    decision_id = add_decision(
        symbol=plan.get("symbol", plan_file.stem),
        plan=plan,
        source=source,
        note=note,
    )
    print(f"[决策日志] 已写入 #{decision_id} {plan.get('symbol')}", flush=True)


if __name__ == "__main__":
    main()
