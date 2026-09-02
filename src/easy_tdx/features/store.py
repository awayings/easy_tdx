"""特征库存储层：家族长表 CSV + 读取 API。

目录结构（默认 ``~/.easy_tdx/features/``）::

    manifest.json          系列注册表（市场/代码/名称/频率/同步状态）
    fx.csv                 汇率家族长表（date, code, open, high, low, close, vol, amount）
    commodity.csv          商品/贵金属家族
    bond.csv               国债收益率家族
    liq.csv               资金市场/央行/美联储利率家族
    macro.csv              宏观指标家族
    index.csv              国际指数家族

更新语义：追加合并（concat + 按 ``(date, code)`` 去重 keep=last）+ 原子写
（tmp + rename）。宏观指标序列 OHLC 同值，消费时取 close；汇率 vol 恒 0（协议无
成交量字段）。回测/策略接入范式：``feature_series``/``merge_features`` 把特征作为
自定义列并入行情 df，与 ``scripts/backtest_161129_premium.py`` 的 premium 列同构。
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from .manifest import features_dir

# 长表标准列。date 为日线日期（无时分），code 为系列代码（如 USDCNY、5_CNTY）。
COLUMNS = ["date", "code", "open", "high", "low", "close", "vol", "amount"]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def family_path(path: str | Path | None, family: str) -> Path:
    return features_dir(path) / f"{family}.csv"


def read_family(path: str | Path | None = None, family: str = "") -> pd.DataFrame:
    """读取家族长表；文件不存在时返回带标准列的空 DataFrame。"""
    fp = family_path(path, family)
    if not fp.exists():
        return _empty()
    df = pd.read_csv(fp)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _write_atomic(fp: Path, df: pd.DataFrame) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, fp)


def update_family(
    path: str | Path | None,
    family: str,
    new_df: pd.DataFrame,
) -> int:
    """把 new_df（单系列新 bar，含 code 列）并入家族长表，返回新增 bar 数。

    按 (date, code) 去重（新值覆盖旧值，keep=last），整体按 (code, date) 排序后
    原子写回。new_df 为空或全部与本地重复时返回 0。
    """
    fp = family_path(path, family)
    if new_df.empty:
        return 0
    old = read_family(path, family)
    if old.empty:
        merged = new_df.drop_duplicates(subset=["date", "code"], keep="last")
        merged = merged.sort_values(["code", "date"], ignore_index=True)
        _write_atomic(fp, merged)
        return len(merged)
    merged = pd.concat([old, new_df], ignore_index=True)
    merged = merged.drop_duplicates(subset=["date", "code"], keep="last")
    merged = merged.sort_values(["code", "date"], ignore_index=True)
    old_keys = set(zip(old["date"], old["code"]))
    new_keys = set(zip(merged["date"], merged["code"]))
    _write_atomic(fp, merged)
    return len(new_keys - old_keys)


def load(path: str | Path | None = None, family: str = "") -> pd.DataFrame:
    """读取单个家族长表（按 code/date 排序）。"""
    return read_family(path, family)


def load_all(path: str | Path | None = None) -> dict[str, pd.DataFrame]:
    """读取全部家族：{family: 长表 DataFrame}（不存在的家族为空表）。"""
    d = features_dir(path)
    families: dict[str, pd.DataFrame] = {}
    if d.exists():
        for fp in sorted(d.glob("*.csv")):
            family = fp.stem
            families[family] = read_family(path, family)
    return families


def to_wide(df: pd.DataFrame, value: str = "close") -> pd.DataFrame:
    """长表 → 宽表：行为 date、列为 code，取 value 字段。"""
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="date", columns="code", values=value, aggfunc="last")


def feature_series(
    path: str | Path | None,
    series_id: str,
) -> pd.Series:
    """取单个系列（id=family_code）的 close 序列，索引为 date。"""
    family, _, code = series_id.partition("_")
    df = read_family(path, family)
    if df.empty:
        return pd.Series(dtype=float, name=series_id)
    sub = df.loc[df["code"] == code, ["date", "close"]].drop_duplicates("date", keep="last")
    return sub.set_index("date")["close"].rename(series_id)


def snapshot(
    path: str | Path | None = None,
    date: str | pd.Timestamp | None = None,
) -> dict[str, float]:
    """全特征快照：{series_id: 最新 close}；给定 date 时取 ≤date 的最新值。

    series_id 按 ``{family}_{code}`` 重建（与 manifest 的 id 约定一致）。
    供 LLM 交易计划（e 模块）与日报使用。
    """
    result: dict[str, float] = {}
    cutoff = pd.Timestamp(date) if date is not None else None
    for family, df in load_all(path).items():
        if df.empty:
            continue
        for code, sub in df.groupby("code"):
            if cutoff is not None:
                sub = sub.loc[sub["date"] <= cutoff]
            if sub.empty:
                continue
            last = sub.sort_values("date").iloc[-1]
            if pd.notna(last["close"]):
                result[f"{family}_{code}"] = float(last["close"])
    return result


def merge_features(
    ohlcv_df: pd.DataFrame,
    series_ids: list[str],
    path: str | Path | None = None,
) -> pd.DataFrame:
    """把指定特征系列作为自定义列按 date 并入行情 df（回测/策略用）。

    ohlcv_df 须含 ``date`` 列；返回副本，新增列名为 series_id，缺失日期填 NaN。
    策略内经 StrategyDataProxy 以 ``self.data.<series_id>[i]`` 访问。
    """
    out = ohlcv_df.copy()
    dates = pd.to_datetime(out["date"])
    for sid in series_ids:
        s = feature_series(path, sid)
        out[sid] = dates.map(s).to_numpy()
    return out
