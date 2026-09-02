"""盘中价格跟踪守护进程：关注列表涨跌幅/点位越界 → 钉钉预警。

用法::

    .venv/bin/python scripts/track_intraday.py                # 盘中轮询（默认 15s，收盘自停）
    .venv/bin/python scripts/track_intraday.py --once         # 拉一次快照打印表格后退出（测试）
    .venv/bin/python scripts/track_intraday.py --dry-run      # 告警只打印不发钉钉
    .venv/bin/python scripts/track_intraday.py --config /path/to/watchlist.json

配置（默认 ~/.easy_tdx/intraday_watchlist.json，缺失时自动生成三只示例标的 ±4%）::

    {
      "poll_seconds": 15,          # 轮询间隔（秒）
      "hysteresis_pct": 0.5,       # 回落重武装缓冲（%）：防止阈值附近抖动刷屏
      "symbols": [{
        "code": "002594", "market": "sz", "name": "比亚迪",
        "rise_pct": 4.0,           # 涨超 +4% 预警；null 禁用该方向
        "fall_pct": 4.0,           # 跌超 -4% 预警
        "price_above": null,       # 可选：上破固定点位预警
        "price_below": null,       # 可选：下破固定点位预警
        "pullback_pct": 4.0,       # 自当日高点回落 ≥4% 预警（基准=日内高点）
        "rebound_pct": 4.0         # 自当日低点反弹 ≥4% 预警（基准=日内低点）
      }]
    }

报警策略：每次从阈值内穿越到阈值外报一次；回落到阈值内（hysteresis 缓冲）后
重新武装，再次越界才会再报。状态在内存，盘中重启会每个条件重报一次。

配置热加载：修改配置文件后**无需重启**，下轮轮询前（≤15s）自动重载生效；
重载时告警状态重武装（已触发条件可能重报一次），解析失败沿用旧配置并告警日志。

调度（macOS launchd，工作日 09:10 启动，脚本收盘 15:05 后自行退出）::

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>9</integer>
        <key>Minute</key><integer>10</integer>
        <key>Weekday</key><array>
            <integer>1</integer><integer>2</integer><integer>3</integer>
            <integer>4</integer><integer>5</integer>
        </array>
    </dict>

防封：仅 A 股交易时段（09:15-11:30 / 13:00-15:00）内轮询，单批 ≤80 只，
15s 间隔远低于 TDX 红线；节假日行情冻结（涨跌幅 0）自然无告警。
启动连接失败自动 30s 重试至收盘（睡眠唤醒时 WiFi 未就绪，实测 2026-09-02）。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from easy_tdx import MacClient, Market
from easy_tdx.exceptions import TdxConnectionError
from easy_tdx.notify import notify

SESSIONS = ((9 * 60 + 15, 11 * 60 + 30), (13 * 60, 15 * 60))  # 与 realtime.feed 一致，end 不含
CLOSE_HM = 15 * 60 + 5  # > 1505 退出（与 track_161129_premium.py 一致）
OUT_OF_SESSION_SLEEP = 30  # 窗口外检查间隔（秒）
CONNECT_RETRY_DELAY = 30.0  # 启动连接失败重试间隔（秒）
STATUS_EVERY_N_POLLS = 12  # ~3 分钟一条状态行
MAX_SYMBOLS = 80  # 协议报价批上限
DEFAULT_RISE_PCT, DEFAULT_FALL_PCT = 4.0, 4.0
DEFAULT_PULLBACK_PCT, DEFAULT_REBOUND_PCT = 4.0, 4.0
DEFAULT_SYMBOLS = [
    {"code": "002594", "market": "sz", "name": "比亚迪", "rise_pct": 4.0, "fall_pct": 4.0},
    {"code": "159558", "market": "sz", "name": "半导体设备ETF", "rise_pct": 4.0, "fall_pct": 4.0},
    {"code": "161129", "market": "sz", "name": "原油LOF", "rise_pct": 4.0, "fall_pct": 4.0},
]


def log(msg: str) -> None:
    """launchd 重定向到文件时 Python 块缓冲会吞掉实时状态，必须 flush。"""
    print(msg, flush=True)


@dataclass(frozen=True)
class WatchSymbol:
    code: str
    market: str  # "sz" / "sh"
    name: str
    rise_pct: float | None
    fall_pct: float | None
    price_above: float | None
    price_below: float | None
    pullback_pct: float | None  # 自当日高点回落幅度阈值（基准=日内高点）
    rebound_pct: float | None  # 自当日低点反弹幅度阈值（基准=日内低点）


@dataclass(frozen=True)
class WatchlistConfig:
    poll_seconds: float
    hysteresis_pct: float
    symbols: list[WatchSymbol]


def default_config_path() -> Path:
    """配置路径：`EASY_TDX_CONFIG_DIR` 环境变量优先（与 easy_tdx.config 同约定）。

    调用时读取环境变量（懒读取），测试可 monkeypatch；默认 `~/.easy_tdx`。
    """
    return Path(os.environ.get("EASY_TDX_CONFIG_DIR", str(Path.home() / ".easy_tdx"))) / (
        "intraday_watchlist.json"
    )


def write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"poll_seconds": 15, "hysteresis_pct": 0.5, "symbols": DEFAULT_SYMBOLS},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _opt_pct(s: dict, key: str, default: float) -> float | None:
    """阈值字段语义：键缺省 → 默认值；显式 null → 禁用（None）；负数 → 报错。"""
    if key not in s:
        return default
    if s[key] is None:
        return None
    v = float(s[key])
    if v < 0:
        raise ValueError(f"{key} 不能为负：{s[key]!r}")
    return v


def _opt_point(s: dict, key: str) -> float | None:
    """点位字段语义：缺省或显式 null → 禁用（None）；非正数 → 报错。"""
    if key not in s or s[key] is None:
        return None
    v = float(s[key])
    if v <= 0:
        raise ValueError(f"{key} 必须为正数：{s[key]!r}")
    return v


def _opt_num(value: object, default: float, minimum: float, key: str) -> float:
    """数值字段（poll_seconds/hysteresis_pct）：缺省或 null → 默认；低于下限 → 报错。"""
    if value is None:
        return default
    v = float(value)
    if v < minimum:
        raise ValueError(f"{key} 不能小于 {minimum}：{value!r}")
    return v


def load_config(path: Path) -> WatchlistConfig:
    """读取关注列表配置；文件缺失时生成默认配置（三只示例 ±4%）。"""
    if not path.exists():
        write_default_config(path)
        log(f"[配置] 未找到 {path}，已生成默认配置（3 只 ±4%），请按需修改。")
    raw = json.loads(path.read_text(encoding="utf-8"))
    symbols = raw.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("配置缺少 symbols 列表")
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError(f"symbols 数量 {len(symbols)} 超过协议批上限 {MAX_SYMBOLS}")
    parsed = []
    for i, s in enumerate(symbols):
        code, market, name = s.get("code"), s.get("market"), s.get("name")
        if not isinstance(code, str) or not (len(code) == 6 and code.isdigit()):
            raise ValueError(f"symbols[{i}] code 必须是 6 位数字：{code!r}")
        if market not in ("sz", "sh"):
            raise ValueError(f"symbols[{i}] market 必须是 sz/sh：{market!r}")
        parsed.append(
            WatchSymbol(
                code=code,
                market=market,
                name=name if isinstance(name, str) and name else code,
                rise_pct=_opt_pct(s, "rise_pct", DEFAULT_RISE_PCT),
                fall_pct=_opt_pct(s, "fall_pct", DEFAULT_FALL_PCT),
                price_above=_opt_point(s, "price_above"),
                price_below=_opt_point(s, "price_below"),
                pullback_pct=_opt_pct(s, "pullback_pct", DEFAULT_PULLBACK_PCT),
                rebound_pct=_opt_pct(s, "rebound_pct", DEFAULT_REBOUND_PCT),
            )
        )
    return WatchlistConfig(
        poll_seconds=_opt_num(raw.get("poll_seconds"), 15.0, 1.0, "poll_seconds"),
        hysteresis_pct=_opt_num(raw.get("hysteresis_pct"), 0.5, 0.0, "hysteresis_pct"),
        symbols=parsed,
    )


def resolve_stocks(cfg: WatchlistConfig) -> list[tuple[int, str]]:
    """关注列表 → 协议请求参数（Market 枚举 int）。"""
    return [(Market.SZ if s.market == "sz" else Market.SH, s.code) for s in cfg.symbols]


def with_poll_override(cfg: WatchlistConfig, poll_seconds: float | None) -> WatchlistConfig:
    """`--poll-seconds` 命令行覆盖（热加载后同样需要重新应用）。"""
    if poll_seconds is None:
        return cfg
    return WatchlistConfig(
        poll_seconds=max(1.0, poll_seconds),
        hysteresis_pct=cfg.hysteresis_pct,
        symbols=cfg.symbols,
    )


def in_session(hm: int) -> bool:
    """hm = hour*60+minute；SESSIONS 各区间 [start, end)。"""
    return any(start <= hm < end for start, end in SESSIONS)


def seconds_to_next_session(hm: int) -> int | None:
    """距下一交易时段开始的秒数（当前不在时段内时）；今天已无时段返回 None。"""
    for start, _ in SESSIONS:
        if hm < start:
            return (start - hm) * 60
    return None


def _file_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def reload_config_if_changed(
    path: Path, last_mtime: int | None, current: WatchlistConfig
) -> tuple[WatchlistConfig, int | None]:
    """配置热加载：文件 mtime 变化时重读；解析失败沿用旧配置。

    解析失败时仍推进 mtime，避免每轮轮询都重试同一份坏文件；
    文件被删除则按启动语义重新生成默认配置并加载（有日志提示）。
    """
    mtime = _file_mtime(path)
    if mtime == last_mtime:
        return current, last_mtime
    try:
        new = load_config(path)
    except ValueError as e:
        log(f"[配置热加载失败，沿用旧配置] {e}")
        return current, mtime
    log(f"[配置热加载] {path.name} 已更新：{len(new.symbols)} 只标的，轮询 {new.poll_seconds:g}s")
    return new, mtime


def safe_price(v: object) -> float | None:
    """无效报价（None/NaN/≤0/不可转）→ None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f <= 0:  # NaN 或不正价（停牌归零）
        return None
    return f


def pct_change(close: float, pre_close: float | None) -> float | None:
    """日内涨跌幅（%），以昨收为锚；昨收无效返回 None。"""
    if pre_close is None or pre_close <= 0:
        return None
    return (close / pre_close - 1) * 100


def evaluate_symbol(
    sym: WatchSymbol,
    close: float,
    pct: float | None,
    hysteresis: float,
    states: dict[tuple[str, str], str],
    *,
    high: float | None = None,
    low: float | None = None,
) -> list[dict]:
    """单标的阈值状态机：穿越触发一次，回落越过缓冲后重武装。

    states: {(code, kind) -> "armed"|"triggered"}，缺省视为 armed（首观即武装，
    开盘跳空越过阈值即触发）。返回本轮触发列表
    [{kind, threshold, close, pct, value, extreme}]。

    pullback/rebound 基准为当日高低点（报价的 high/low 列）：创新高/新低后
    幅度自动归零（close≈high/low），状态随之自然重武装，可反复触发。
    """
    h = hysteresis
    conds = []
    if sym.rise_pct is not None and pct is not None:
        conds.append(
            ("rise", pct >= sym.rise_pct, pct < sym.rise_pct - h, sym.rise_pct, None, None)
        )
    if sym.fall_pct is not None and pct is not None:
        conds.append(
            ("fall", pct <= -sym.fall_pct, pct > -sym.fall_pct + h, sym.fall_pct, None, None)
        )
    if sym.price_above is not None:
        conds.append(
            (
                "above",
                close >= sym.price_above,
                close < sym.price_above * (1 - h / 100),
                sym.price_above,
                None,
                None,
            )
        )
    if sym.price_below is not None:
        conds.append(
            (
                "below",
                close <= sym.price_below,
                close > sym.price_below * (1 + h / 100),
                sym.price_below,
                None,
                None,
            )
        )
    if sym.pullback_pct is not None and high is not None and high > 0:
        pullback = (high - close) / high * 100
        conds.append(
            (
                "pullback",
                pullback >= sym.pullback_pct,
                pullback < sym.pullback_pct - h,
                sym.pullback_pct,
                pullback,
                high,
            )
        )
    if sym.rebound_pct is not None and low is not None and low > 0:
        rebound = (close - low) / low * 100
        conds.append(
            (
                "rebound",
                rebound >= sym.rebound_pct,
                rebound < sym.rebound_pct - h,
                sym.rebound_pct,
                rebound,
                low,
            )
        )
    alerts = []
    for kind, crossed, rearmed, threshold, value, extreme in conds:
        key = (sym.code, kind)
        state = states.get(key, "armed")
        if state == "armed" and crossed:
            states[key] = "triggered"
            alerts.append(
                {
                    "kind": kind,
                    "threshold": threshold,
                    "close": close,
                    "pct": pct,
                    "value": value,
                    "extreme": extreme,
                }
            )
        elif state == "triggered" and rearmed:
            states[key] = "armed"
    return alerts


_KIND_LABEL = {
    "rise": "涨超阈值",
    "fall": "跌超阈值",
    "above": "上破点位",
    "below": "下破点位",
    "pullback": "自高点回落",
    "rebound": "自低点反弹",
}


def format_alert(sym: WatchSymbol, alert: dict, now: datetime) -> str:
    kind = alert["kind"]
    if kind in ("rise", "fall"):
        sign = "+" if kind == "rise" else "-"
        th = f"阈值 {sign}{alert['threshold']:.1f}%"
        pct = "" if alert["pct"] is None else f" ({alert['pct']:+.2f}%)"
    elif kind == "pullback":
        th = f"高点 {alert['extreme']:.2f}, 阈值 {alert['threshold']:.1f}%"
        pct = f" (回落 {alert['value']:.2f}%)"
    elif kind == "rebound":
        th = f"低点 {alert['extreme']:.2f}, 阈值 {alert['threshold']:.1f}%"
        pct = f" (反弹 {alert['value']:.2f}%)"
    else:
        th = f"点位 {alert['threshold']:.2f}"
        pct = ""
    return (
        f"{sym.name}({sym.code}) {_KIND_LABEL[kind]}: "
        f"现价 {alert['close']:.2f}{pct}, {th} [{now:%H:%M:%S}]"
    )


def fetch_snapshot(client: MacClient, stocks: list[tuple[int, str]]) -> dict[str, pd.Series]:
    """单批拉取全部标的报价，按 code 索引（响应顺序无保证）。异常返回空 dict。"""
    try:
        df = client.get_stock_quotes(stocks)
    except Exception as e:
        log(f"[取数失败] {e}")
        return {}
    if df is None or df.empty:
        return {}
    return {row["code"]: row for _, row in df.iterrows()}


def poll_once(
    client: MacClient,
    cfg: WatchlistConfig,
    stocks: list[tuple[int, str]],
    states: dict[tuple[str, str], str],
    *,
    poll_no: int,
    no_notify: bool,
    dry_run: bool,
    verbose: bool,
) -> int:
    """一轮轮询：取数 → 逐标的评估状态机 → 告警。返回本轮告警数。"""
    rows = fetch_snapshot(client, stocks)
    if not rows:
        return 0
    alerts = 0
    status = []
    for sym in cfg.symbols:
        row = rows.get(sym.code)
        if row is None:
            continue  # 响应中缺该标的（罕见），跳过
        close = safe_price(row.get("close"))
        if close is None:
            continue  # 停牌/无行情：全部方向跳过
        pct = pct_change(close, safe_price(row.get("pre_close")))
        for alert in evaluate_symbol(
            sym,
            close,
            pct,
            cfg.hysteresis_pct,
            states,
            high=safe_price(row.get("high")),
            low=safe_price(row.get("low")),
        ):
            text = format_alert(sym, alert, datetime.now())
            if dry_run:
                log(f"[DRY-RUN] {text}")
            elif not no_notify:
                notify("盘中预警", text)
            else:
                log(f"[告警(未发送)] {text}")
            alerts += 1
        pct_s = "" if pct is None else f"{pct:+.2f}%"
        status.append(f"{sym.name} {close:.2f}({pct_s})")
    if verbose or poll_no % STATUS_EVERY_N_POLLS == 0:
        log(f"[{datetime.now():%H:%M:%S}] 第{poll_no}轮 " + " | ".join(status))
    return alerts


def should_exit(now: datetime) -> str | None:
    """退出条件（工作日/收盘守卫），返回退出原因或 None。"""
    if now.weekday() >= 5:
        return f"非交易日（{now:%A}），退出。"
    if now.hour * 60 + now.minute > CLOSE_HM:
        return "已过收盘时间，退出。"
    return None


def open_client() -> MacClient:
    """建立行情连接：网络未就绪时 30s 重试，收盘后放弃。

    launchd 睡眠补跑触发时 WiFi 常尚未连上（Errno 51 Network is unreachable，
    实测 2026-09-02 早 09:10 启动即因此崩溃静默失联），故启动阶段重试。
    返回**已连接** client；调用方用 try/finally close 替代 with——
    `connect()` 非幂等（每次新建 socket），勿对已连接实例二次 `__enter__`。
    """
    while True:
        client: MacClient | None = None
        try:
            client = MacClient.from_best_host()
            client.connect()
            return client
        except TdxConnectionError as e:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            if time.localtime().tm_hour * 60 + time.localtime().tm_min > CLOSE_HM:
                raise
            log(f"[连接失败，{CONNECT_RETRY_DELAY:g}s 后重试] {e}")
            time.sleep(CONNECT_RETRY_DELAY)


def once_mode(cfg: WatchlistConfig, stocks: list[tuple[int, str]]) -> None:
    """拉一次快照打印表格后退出（绕过守卫，盘后测试用）。"""
    with MacClient.from_best_host() as client:
        rows = fetch_snapshot(client, stocks)
    if not rows:
        log("[取数失败] 无报价返回")
        return
    print(f"{'代码':<8}{'名称':<12}{'现价':>10}{'涨跌幅':>10}")
    for sym in cfg.symbols:
        row = rows.get(sym.code)
        if row is None:
            print(f"{sym.code:<8}{sym.name:<12}{'缺数据':>10}{'':>10}")
            continue
        close = safe_price(row.get("close"))
        if close is None:
            print(f"{sym.code:<8}{sym.name:<12}{'停牌/无效':>10}{'':>10}")
            continue
        pct = pct_change(close, safe_price(row.get("pre_close")))
        pct_s = "" if pct is None else f"{pct:+.2f}%"
        print(f"{sym.code:<8}{sym.name:<12}{close:>10.2f}{pct_s:>10}")


def run_tracker(cfg: WatchlistConfig, args: argparse.Namespace) -> None:
    now = datetime.now()
    reason = should_exit(now)
    if reason:
        log(reason)
        return
    stocks = resolve_stocks(cfg)
    if args.startup_notify and not args.no_notify and not args.dry_run:
        notify(
            "盘中预警",
            f"盘中跟踪已启动：{', '.join(s.name for s in cfg.symbols)}，轮询 {cfg.poll_seconds:g}s",
        )
    config_mtime = _file_mtime(args.config)
    states: dict[tuple[str, str], str] = {}
    poll_no = 0
    client = open_client()  # 心跳保活整天，命令级自动重连兜底
    try:
        while True:
            new_cfg, config_mtime = reload_config_if_changed(args.config, config_mtime, cfg)
            if new_cfg is not cfg:
                cfg = with_poll_override(new_cfg, args.poll_seconds)
                stocks = resolve_stocks(cfg)
                states = {}  # 重武装：已触发条件可能重报一次（与重启行为一致）
            hm = time.localtime().tm_hour * 60 + time.localtime().tm_min
            if hm > CLOSE_HM:
                log("已收盘，退出。")
                break
            if not in_session(hm):
                nxt = seconds_to_next_session(hm)
                time.sleep(
                    min(OUT_OF_SESSION_SLEEP, nxt) if nxt is not None else OUT_OF_SESSION_SLEEP
                )
                continue
            poll_no += 1
            poll_once(
                client,
                cfg,
                stocks,
                states,
                poll_no=poll_no,
                no_notify=args.no_notify,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
            time.sleep(cfg.poll_seconds)
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="盘中价格跟踪：涨跌幅/点位越界 → 钉钉预警")
    parser.add_argument(
        "--config", type=Path, default=default_config_path(), help="关注列表配置 JSON"
    )
    parser.add_argument(
        "--poll-seconds", type=float, default=None, help="轮询间隔（秒），默认用配置值"
    )
    parser.add_argument("--once", action="store_true", help="拉一次快照打印表格后退出（绕过守卫）")
    parser.add_argument("--no-notify", action="store_true", help="不发钉钉")
    parser.add_argument("--dry-run", action="store_true", help="告警只打印不发送")
    parser.add_argument("--startup-notify", action="store_true", help="会话开始时发一条启动通知")
    parser.add_argument("--verbose", action="store_true", help="每轮打印状态")
    args = parser.parse_args()
    cfg = with_poll_override(load_config(args.config), args.poll_seconds)
    if args.once:
        once_mode(cfg, resolve_stocks(cfg))
    else:
        run_tracker(cfg, args)


if __name__ == "__main__":
    main()
