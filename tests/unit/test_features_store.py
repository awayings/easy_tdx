"""特征库存储层测试（纯离线，无网络）。"""

from __future__ import annotations

import pandas as pd

from easy_tdx.features.store import (
    feature_series,
    load_all,
    merge_features,
    read_family,
    snapshot,
    to_wide,
    update_family,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bars(code: str, rows: list[tuple[str, float]]) -> pd.DataFrame:
    """构造新 bar 长表：rows = [(YYYY-MM-DD, close), ...]。"""
    closes = [r[1] for r in rows]
    return pd.DataFrame(
        {
            "date": pd.to_datetime([r[0] for r in rows]),
            "code": [code] * len(rows),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "vol": [0] * len(rows),
            "amount": [0.0] * len(rows),
        }
    )


def _seed(tmp_path, family: str, code: str, rows: list[tuple[str, float]]) -> int:
    return update_family(tmp_path, family, _bars(code, rows))


# ---------------------------------------------------------------------------
# update_family
# ---------------------------------------------------------------------------


def test_update_family_creates_new_file(tmp_path):
    added = _seed(tmp_path, "fx", "USDCNY", [("2026-08-26", 6.72), ("2026-08-27", 6.73)])
    assert added == 2
    df = read_family(tmp_path, "fx")
    assert list(df.columns[:2]) == ["date", "code"]
    assert len(df) == 2
    assert (tmp_path / "fx.csv").exists()
    assert not (tmp_path / "fx.csv.tmp").exists()  # 原子写不留临时文件


def test_update_family_merge_dedupe_keep_last(tmp_path):
    _seed(tmp_path, "fx", "USDCNY", [("2026-08-26", 6.72), ("2026-08-27", 6.73)])
    # 重叠日期新值覆盖 + 新增一天 + 同家族另一系列
    new = pd.concat(
        [
            _bars("USDCNY", [("2026-08-27", 6.74), ("2026-08-28", 6.75)]),
            _bars("USDJPY", [("2026-08-28", 159.3)]),
        ],
        ignore_index=True,
    )
    added = update_family(tmp_path, "fx", new)
    # added 只计新的 (date, code) 键：USDCNY 08-28 + USDJPY 08-28 = 2（重写不算新增）
    assert added == 2
    df = read_family(tmp_path, "fx")
    assert len(df) == 4
    row = df.loc[(df["code"] == "USDCNY") & (df["date"] == "2026-08-27"), "close"]
    assert float(row.iloc[0]) == 6.74
    # 整体按 (code, date) 排序
    assert df["code"].is_monotonic_increasing


def test_update_family_empty_df_returns_zero(tmp_path):
    _seed(tmp_path, "fx", "USDCNY", [("2026-08-26", 6.72)])
    assert update_family(tmp_path, "fx", pd.DataFrame()) == 0
    assert len(read_family(tmp_path, "fx")) == 1


def test_update_family_fully_duplicate_returns_zero(tmp_path):
    _seed(tmp_path, "fx", "USDCNY", [("2026-08-26", 6.72)])
    assert update_family(tmp_path, "fx", _bars("USDCNY", [("2026-08-26", 6.72)])) == 0
    assert len(read_family(tmp_path, "fx")) == 1


# ---------------------------------------------------------------------------
# 读取 API
# ---------------------------------------------------------------------------


def test_feature_series_and_snapshot(tmp_path):
    _seed(tmp_path, "fx", "USDCNY", [("2026-08-26", 6.72), ("2026-08-27", 6.73)])
    _seed(tmp_path, "bond", "5_CNTY", [("2026-08-27", 1.70)])
    s = feature_series(tmp_path, "fx_USDCNY")
    assert s.name == "fx_USDCNY"
    assert float(s.loc["2026-08-27"]) == 6.73
    snap = snapshot(tmp_path)
    assert snap == {"fx_USDCNY": 6.73, "bond_5_CNTY": 1.70}
    # 截止日期：只取 ≤ date 的最新值
    snap_cut = snapshot(tmp_path, date="2026-08-26")
    assert snap_cut["fx_USDCNY"] == 6.72
    assert "bond_5_CNTY" not in snap_cut


def test_load_all_and_to_wide(tmp_path):
    _seed(tmp_path, "fx", "USDCNY", [("2026-08-26", 6.72), ("2026-08-27", 6.73)])
    _seed(tmp_path, "fx", "USDJPY", [("2026-08-26", 159.3)])
    all_df = load_all(tmp_path)
    assert set(all_df) == {"fx"}
    wide = to_wide(all_df["fx"])
    assert list(wide.columns) == ["USDCNY", "USDJPY"]
    assert float(wide.loc["2026-08-26", "USDCNY"]) == 6.72
    assert pd.isna(wide.loc["2026-08-27", "USDJPY"])


def test_merge_features_adds_columns_with_nan(tmp_path):
    _seed(tmp_path, "fx", "USDCNY", [("2026-08-26", 6.72), ("2026-08-27", 6.73)])
    _seed(tmp_path, "bond", "5_CNTY", [("2026-08-27", 1.70)])
    kline = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-08-26", "2026-08-27"]),
            "close": [10.0, 10.5],
        }
    )
    out = merge_features(kline, ["fx_USDCNY", "bond_5_CNTY"], tmp_path)
    assert list(out.columns) == ["date", "close", "fx_USDCNY", "bond_5_CNTY"]
    assert float(out.loc[0, "fx_USDCNY"]) == 6.72
    assert pd.isna(out.loc[0, "bond_5_CNTY"])
    assert float(out.loc[1, "bond_5_CNTY"]) == 1.70
    # 原 df 不被修改
    assert list(kline.columns) == ["date", "close"]
