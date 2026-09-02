"""交易计划命令：easy-tdx plan。

"念动想买入"时一条命令出计划：技术点位状态 + 市场情绪 + 公共舆情 → 风险评分 →
止损/止盈（事件条件 + 具体点位）+ 仓位建议。
"""

from __future__ import annotations

import json

import click


@click.command()
@click.argument("symbol")
@click.option(
    "--base-position", default=0.10, type=float, show_default=True,
    help="基础仓位比例（0-1）",
)
@click.option("--no-public", is_flag=True, help="跳过公共舆情抓取（离线用）")
@click.option("--notify", is_flag=True, help="钉钉推送计划摘要")
@click.option("--save", is_flag=True, help="计划落 decisions.db 决策日志")
@click.option("--table", "use_table", is_flag=True, help="只打印人类可读的计划摘要")
@click.option("--output", "output_fmt", type=click.Choice(["json", "table"]), default="json")
def plan(
    symbol: str,
    base_position: float,
    no_public: bool,
    notify: bool,
    save: bool,
    use_table: bool,
    output_fmt: str,
) -> None:
    """生成单只标的的交易计划（止损/止盈点位 + 仓位 + 风险评分）。

    SYMBOL: 标的代码，支持 "SZ 002594" / "SZ002594" / "002594"。

    示例：

      easy-tdx plan 002594

      easy-tdx plan SZ 002594 --table

      easy-tdx plan 600519 --base-position 0.15 --notify --save

    数据来源：本地 .day 日线（缺失时 TDX 协议兜底）+ market-stat 市场情绪 +
    东财人气榜/新闻舆情 + 本地宏观特征库。离线时自动降级为纯技术面计划。
    """
    from ..planner import build_full_plan, parse_symbol

    fmt = "table" if use_table else output_fmt
    try:
        market, code = parse_symbol(symbol)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e

    if not 0 < base_position <= 0.5:
        raise click.BadParameter("--base-position 须在 (0, 0.5] 区间")

    result = build_full_plan(
        market,
        code,
        base_position=base_position,
        with_public=not no_public,
    )
    if "error" in result:
        click.echo(json.dumps(result, ensure_ascii=False), err=True)
        raise click.ClickException(result["error"])

    if save:
        from ..journal import add_decision

        add_decision(
            symbol=f"{market} {code}",
            plan=result,
            source="easy-tdx plan",
            note="",
        )
        click.echo(f"[决策日志] 已写入 decisions.db（{market} {code}）", err=True)

    if notify:
        from ..notify import notify as send_notify

        send_notify(f"交易计划·{market} {code}", result["summary"])

    if fmt == "table":
        click.echo(result["summary"])
    else:
        # JSON 输出去掉 summary 纯文本（--table 或钉钉看它）
        out = {k: v for k, v in result.items() if k != "summary"}
        click.echo(json.dumps(out, ensure_ascii=False, indent=2, default=str))
