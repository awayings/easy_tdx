"""引导本地 vipdoc 日线数据，供 `easy-tdx screen scan` 等离线功能使用。

本机（macOS）无 Windows 通达信，~/new_tdx/vipdoc 不存在；`screen scan` 是纯离线
扫描（读本地 .day 文件），需要先有数据。

默认模式（file）：用 0x06B9 文件下载命令从行情主站直下**服务器原生 .day 文件**
（每股一次分段下载拿全历史，2026-08-31 实测 62 只 ≈ 1 分钟）。相比走 K 线协议
（800 根/页 + 本地编码），直下模式：
  - 拿全历史（浦发 6379 根，含上市以来全部）
  - 字节与通达信官方盘后下载一致，无 vol 单位/编码问题
  - 极端天量（如京东方单日 60 亿股）以官方文件的降级存法原样落地

fallback 模式（kline）：走 K 线协议逐只取最近 800 根，本地编码写入（含股→手
换算与天量钳位），仅当文件下载命令不可用时使用。

幂等：file 模式覆盖写（服务器文件即最新全量），kline 模式按日期追加。
每日更新：收盘后重跑一次即可（file 模式 = 62 个文件传输）。

用法::

    .venv/bin/python scripts/bootstrap_vipdoc.py               # 全池直下
    .venv/bin/python scripts/bootstrap_vipdoc.py --workers 4   # 并发（每 worker 一连接）
    .venv/bin/python scripts/bootstrap_vipdoc.py --only sz002812
    .venv/bin/python scripts/bootstrap_vipdoc.py --mode kline  # 协议 fallback
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import struct
import sys
import time
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from easy_tdx.commands.report_file import GetReportFileCmd  # noqa: E402
from easy_tdx.transport import TdxConnection  # noqa: E402
from easy_tdx.transport.sync import KNOWN_HOSTS  # noqa: E402

# 沪深流动性好的股票池（扫描演示用，可按需增改；加新股后用 --only 只同步新代码）
UNIVERSE: dict[str, list[str]] = {
    "sh": [
        "600519", "600036", "601318", "600900", "600030", "601899",
        "600887", "601888", "600276", "603259", "601398", "601288",
        "601988", "601939", "600000", "601166", "601088", "600690",
        "600104", "601668", "600048", "600809", "603288", "600031",
        "601138", "688981", "600941", "601857", "600028", "600585",
    ],
    "sz": [
        "000001", "000002", "000333", "000651", "000858", "002594",
        "300750", "002415", "000725", "002475", "000568", "002714",
        "300059", "002352", "000063", "002304", "300760", "002230",
        "000100", "300015", "002027", "000538", "300124", "300498",
        "002460", "300274", "000977", "000625", "300308", "002050",
        "002812", "002241",
    ],
}

PORT = 7709
CHUNK = 60000  # 服务器单次文件下载响应上限（实测 60KB，请求更大也只回 60KB）
HOSTS = KNOWN_HOSTS  # 失败自动换台

# 股票池配置文件（首次运行自动从 UNIVERSE 生成；之后增删股票只改这个文件）
CONFIG_PATH = Path.home() / ".easy_tdx" / "vipdoc_universe.txt"


def _load_universe() -> dict[str, list[str]]:
    """读取股票池配置文件；缺失时用内置 UNIVERSE 生成。

    格式：每行一只，"sh600000" / "sz000001"，或裸 6 位代码
    （6xx/68x/9xx 归沪，其余归深）；# 开头为注释。
    """
    if not CONFIG_PATH.is_file():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# easy_tdx vipdoc 股票池（bootstrap_vipdoc.py 每日 22:45 同步）",
            "# 每行一只：sh600000 / sz000001 或裸代码（6xx/68x/9xx 归沪，其余归深）",
            "# 增删股票后，下次定时任务/手动重跑自动生效",
        ]
        for ex, codes in UNIVERSE.items():
            lines.append("")
            lines.append(f"# {ex.upper()}")
            lines.extend(f"{ex}{code}" for code in codes)
        CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        click.echo(f"已生成股票池配置: {CONFIG_PATH}")

    work: dict[str, list[str]] = {"sh": [], "sz": []}
    for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        token = line.strip().lower()
        if not token or token.startswith("#"):
            continue
        if token[:2] in ("sh", "sz") and len(token) == 8 and token[2:].isdigit():
            work[token[:2]].append(token[2:8])
        elif len(token) == 6 and token.isdigit():
            work["sh" if token[0] in ("6", "9") else "sz"].append(token)
        else:
            click.echo(f"忽略无法识别的行: {line}", err=True)
    return work


# ── file 模式：直下服务器原生 .day ─────────────────────────────────────────────


def _fetch_file(conn: TdxConnection, filename: str) -> bytes:
    """分段下载服务器文件至完整字节（0x06B9，每段 ≤60KB）。"""
    data = bytearray()
    pos = 0
    while True:
        chunk = conn.execute(GetReportFileCmd(filename, pos, CHUNK))
        if not chunk:
            break
        data.extend(chunk)
        pos += len(chunk)
        if len(chunk) < CHUNK:
            break
    return bytes(data)


def _download_file(job: tuple[str, str], vipdoc: Path) -> str:
    """下载单只 .day 并原子覆盖写盘，返回状态消息。失败依次换台。"""
    exchange, code = job
    filename = f"vipdoc/{exchange}/lday/{exchange}{code}.day"
    filepath = vipdoc / exchange / "lday" / f"{exchange}{code}.day"
    last_err: Exception | None = None

    for host in HOSTS:
        try:
            conn = TdxConnection(host, PORT, 10.0)
            conn.connect()
            try:
                data = _fetch_file(conn, filename)
            finally:
                conn.close()

            if not data or len(data) < 32 or len(data) % 32 != 0:
                raise ValueError(f"文件异常: {len(data)} 字节")

            tmp = filepath.with_suffix(".day.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, filepath)

            n = len(data) // 32
            date_int = struct.unpack_from("<I", data, (n - 1) * 32)[0]
            return f"{exchange}{code}: {n} 根 (截止 {date_int})"
        except Exception as e:  # noqa: BLE001 换下一台
            last_err = e

    return f"{exchange}{code}: ✗ {last_err}"


def _run_file_mode(jobs: list[tuple[str, str]], vipdoc: Path, workers: int) -> None:
    total = len(jobs)
    if workers <= 1:
        for idx, job in enumerate(jobs, 1):
            click.echo(f"[{idx}/{total}] {_download_file(job, vipdoc)}")
    else:
        # 每 job 自建连接，线程池并发；服务器单次 60KB 上限，62 只 × 4 worker
        # 约 20 秒，对服务器负载可控
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_download_file, job, vipdoc): job for job in jobs}
            for idx, fut in enumerate(cf.as_completed(futures), 1):
                job = futures[fut]
                click.echo(f"[{idx}/{total}] {fut.result()}")


# ── kline 模式：协议 K 线 + 本地编码（fallback） ──────────────────────────────


def _run_kline_mode(jobs: list[tuple[str, str]], vipdoc: Path, sleep_s: float) -> None:
    from easy_tdx.client import TdxClient
    from easy_tdx.cli.cmd_offline import _df_to_bars
    from easy_tdx.models.enums import KlineCategory, Market
    from easy_tdx.offline import append_daily_bars
    from easy_tdx.offline.daily_bar import _SECURITY_COEFFICIENTS, _detect_security_type

    total = len(jobs)
    ok = fail = 0

    with TdxClient.from_best_host() as client:
        for idx, (exchange, code) in enumerate(jobs, 1):
            market = Market.SH if exchange == "sh" else Market.SZ
            filepath = vipdoc / exchange / "lday" / f"{exchange}{code}.day"
            try:
                df = client.get_security_bars(market, code, KlineCategory.DAY, start=0, count=800)
                if df.empty:
                    fail += 1
                    click.echo(f"[{idx}/{total}] {exchange}{code}: 服务端无数据，跳过")
                    continue
                bars = _df_to_bars(df)
                sec_type = _detect_security_type(filepath.name)
                price_coeff, vol_coeff = _SECURITY_COEFFICIENTS.get(sec_type, (0.01, 0.01))
                # 协议成交量单位是股；encode 在 vol_coeff=0.01 时 ×100 写入
                # （.day 原始字段为股，读取端 ×0.01 还原为手），须先换算为手，
                # 否则大成交量股票溢出 uint32（同 cmd_offline._sync_one_daily）。
                if vol_coeff == 0.01:
                    for b in bars:
                        b.vol /= 100
                # 极端天量钳位：京东方 2026-05-22 单日 60 亿股（60M 手），
                # 按 encode 约定 raw=手×100=6e9 仍爆 uint32（服务器原生 .day
                # 对该 bar 也是降级存储）。钳位保住 K 线完整性，仅极端日失真。
                for b in bars:
                    b.vol = min(b.vol, 42_000_000.0)
                written = append_daily_bars(filepath, bars, price_coeff, vol_coeff)
                ok += 1
                click.echo(f"[{idx}/{total}] {exchange}{code}: 800 根（新增 {written}）")
            except Exception as e:  # noqa: BLE001 单只失败不中断
                fail += 1
                click.echo(f"[{idx}/{total}] {exchange}{code}: ✗ {e}")
            if sleep_s > 0:
                time.sleep(sleep_s)

    click.echo(f"完成: 成功 {ok} | 失败 {fail}")


# ── CLI ────────────────────────────────────────────────────────────────────────


@click.command()
@click.option(
    "--mode",
    type=click.Choice(["file", "kline"]),
    default="file",
    help="file=直下服务器原生 .day（默认，全历史）; kline=协议 K 线 800 根 fallback",
)
@click.option("--workers", default=1, type=int, help="并发连接数（file 模式，默认 1，4 约 20 秒）")
@click.option("--sleep", "sleep_s", default=0.0, type=float, help="kline 模式每只间隔秒数")
@click.option(
    "--only",
    "only_codes",
    multiple=True,
    help='只同步指定代码（格式 "sh600000" / "sz000001"，可多次），加单只新股票时避免重拉全池',
)
def main(mode: str, workers: int, sleep_s: float, only_codes: tuple[str, ...]) -> None:
    vipdoc = Path.home() / "new_tdx" / "vipdoc"
    for exchange in ("sh", "sz"):
        (vipdoc / exchange / "lday").mkdir(parents=True, exist_ok=True)
    click.echo(f"vipdoc: {vipdoc} | 模式: {mode} | 并发: {workers}")

    if only_codes:
        work: dict[str, list[str]] = {"sh": [], "sz": []}
        for token in only_codes:
            if not (len(token) == 8 and token[:2] in ("sh", "sz")):
                raise click.UsageError(f'--only 格式错误: {token}（应为 "sh600000" / "sz000001"）')
            work[token[:2]].append(token[2:8])
    else:
        work = _load_universe()

    jobs = [(ex, code) for ex, codes in work.items() for code in codes]
    if not jobs:
        click.echo("股票池为空，请检查 " + str(CONFIG_PATH))
        return
    if mode == "file":
        _run_file_mode(jobs, vipdoc, workers)
    else:
        _run_kline_mode(jobs, vipdoc, sleep_s)

    click.echo("验证: easy-tdx offline daily SZ 000001 --table | 或 screen scan")


if __name__ == "__main__":
    main()
