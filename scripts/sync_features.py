"""每日同步 ex 扩展市场特征到本地特征库（默认 ``~/.easy_tdx/features/``）。

数据源：MAC 扩展行情协议（端口 7727），覆盖汇率/商品期货主连/上海黄金/中美国债/
资金市场与央行利率/宏观指标/国际指数（2026-09-01 连通性实测通过，见
docs/trading_system_requirements.md §3.4）。

增量模式（默认）：每系列取尾部 60 根并入家族长表（按 date+code 去重），约
48 系列 × 1 请求 ≈ 1 分钟。回填模式（--backfill）：每系列翻页拉全历史
（700 根/页，48 系列 ≈ 60~100 请求，首次初始化用，建议白天手动跑一次）。

系列增删：直接编辑 ``~/.easy_tdx/features/manifest.json``（enabled=false 停用），
下次运行自动生效；代码内置新增的默认系列也会自动并入。

用法::

    .venv/bin/python scripts/sync_features.py                # 尾部增量（launchd 23:00 错峰）
    .venv/bin/python scripts/sync_features.py --backfill     # 全历史回填（首次）
    .venv/bin/python scripts/sync_features.py --only fx_USDCNY --only liq_R007
    .venv/bin/python scripts/sync_features.py --workers 4
    .venv/bin/python scripts/sync_features.py --list         # 查看系列注册表
"""

from __future__ import annotations

import concurrent.futures as cf
import sys
import time
from datetime import datetime
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from easy_tdx.features.collector import (  # noqa: E402
    fetch_all,
    fetch_tail,
    new_client,
    refresh_best_host,
)
from easy_tdx.features.manifest import STALE_DAYS, ensure_manifest, save_manifest  # noqa: E402
from easy_tdx.features.store import features_dir, update_family  # noqa: E402


def _fetch_one(job: tuple[dict, bool, int]) -> dict:
    """单系列抓取（worker 内独立连接）。"""
    series, backfill, tail_count = job
    t0 = time.time()
    client = new_client()
    try:
        df = fetch_all(client, series) if backfill else fetch_tail(client, series, tail_count)
        return {"series": series, "df": df, "error": None, "elapsed": time.time() - t0}
    except Exception as e:  # noqa: BLE001 - 单系列失败不阻断整体
        return {"series": series, "df": None, "error": repr(e), "elapsed": time.time() - t0}
    finally:
        client.close()


@click.command()
@click.option("--dir", "fdir", type=click.Path(path_type=Path), default=None,
              help="特征库目录（默认 ~/.easy_tdx/features，可用 EASY_TDX_CONFIG_DIR 覆盖）")
@click.option("--backfill", is_flag=True, help="全历史回填（翻页拉全历史，首次初始化用）")
@click.option("--only", "only_ids", multiple=True, help="只同步指定系列 id（可重复）")
@click.option("--workers", default=4, type=int, show_default=True, help="并发连接数（≤4-8）")
@click.option("--tail-count", default=60, type=int, show_default=True,
              help="增量模式每系列取尾部根数")
@click.option("--list", "list_only", is_flag=True, help="只打印系列注册表后退出")
def main(fdir, backfill, only_ids, workers, tail_count, list_only) -> None:
    series_all = ensure_manifest(fdir)
    if list_only:
        click.echo(f"特征库: {features_dir(fdir)} | 系列 {len(series_all)} 个")
        for s in series_all:
            state = "on " if s.get("enabled") else "off"
            click.echo(
                f"  [{state}] {s['id']:<22} {s['market']:<16} {s['code']:<10} "
                f"{s['name']} ({s['freq']})  last={s.get('last_bar') or '-'}"
            )
        return

    jobs: list[tuple[dict, bool, int]] = []
    for s in series_all:
        if not s.get("enabled", True):
            continue
        if only_ids and s["id"] not in only_ids:
            continue
        jobs.append((s, backfill, tail_count))
    if not jobs:
        click.echo("没有待同步的系列（检查 enabled / --only 过滤）。")
        return

    mode = "全历史回填" if backfill else f"尾部增量（{tail_count} 根）"
    click.echo(f"特征库: {features_dir(fdir)} | {mode} | 系列 {len(jobs)} | 并发 {workers}")

    # 并发前单线程测速刷新一次最优主机缓存（worker 内不写 config，避免竞态）
    refresh_best_host()
    # 抓取并发（每 worker 一连接），合并写串行（同家族文件只有一个，避免写冲突）
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_fetch_one, jobs))

    failures, stale, updated = 0, 0, 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for r in results:
        s = r["series"]
        if r["error"] is not None:
            failures += 1
            click.echo(f"  ✗ {s['id']:<22} {r['error']} ({r['elapsed']:.1f}s)")
            continue
        df = r["df"]
        added = update_family(fdir, s["family"], df)
        updated += 1
        last = str(df["date"].max().date()) if not df.empty else None
        s["last_sync"] = now
        if last:
            s["last_bar"] = last
        if added == 0:
            click.echo(f"  ✓ {s['id']:<22} 无新增 ({r['elapsed']:.1f}s)  last={last}")
            continue
        if last and _is_stale(last, s["freq"]):
            stale += 1
        click.echo(f"  ✓ {s['id']:<22} +{added} 根 ({r['elapsed']:.1f}s)  last={last}")
    save_manifest(series_all, fdir)

    click.echo(
        f"完成: 成功 {updated} / 失败 {failures} / 陈旧告警 {stale}"
        + ("（见上方 last 日期，按频率阈值 D=3d W=21d M=90d）" if stale else "")
    )
    sys.exit(1 if failures else 0)


def _is_stale(last_bar: str, freq: str) -> bool:
    try:
        days = (datetime.now().date() - datetime.fromisoformat(last_bar).date()).days
    except ValueError:
        return False
    return days > STALE_DAYS.get(freq, 3)


if __name__ == "__main__":
    main()
