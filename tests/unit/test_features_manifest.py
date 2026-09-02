"""特征库系列注册表（manifest）测试（纯离线，无网络）。"""

from __future__ import annotations

import json

from easy_tdx.features.manifest import (
    DEFAULT_SERIES,
    ensure_manifest,
    features_dir,
    save_manifest,
)


def test_ensure_manifest_creates_defaults(tmp_path):
    series = ensure_manifest(tmp_path)
    assert len(series) == len(DEFAULT_SERIES)
    assert series[0]["id"] == "fx_USDCNY"
    data = json.loads((features_dir(tmp_path) / "manifest.json").read_text("utf-8"))
    assert data["version"] == 1
    assert len(data["series"]) == len(DEFAULT_SERIES)


def test_ensure_manifest_preserves_user_edits_and_merges_new_defaults(tmp_path):
    ensure_manifest(tmp_path)
    series = ensure_manifest(tmp_path)
    # 用户禁用 + 修改名称 → 再次 ensure 不覆盖
    s = next(x for x in series if x["id"] == "fx_USDCNY")
    s["enabled"] = False
    s["name"] = "人民币汇率（自定义）"
    s["last_bar"] = "2026-08-27"
    save_manifest(series, tmp_path)
    series2 = ensure_manifest(tmp_path)
    s2 = next(x for x in series2 if x["id"] == "fx_USDCNY")
    assert s2["enabled"] is False
    assert s2["name"] == "人民币汇率（自定义）"
    assert s2["last_bar"] == "2026-08-27"
    assert len(series2) == len(DEFAULT_SERIES)


def test_ensure_manifest_recovers_corrupt_file(tmp_path):
    features_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (features_dir(tmp_path) / "manifest.json").write_text("{not valid json", "utf-8")
    series = ensure_manifest(tmp_path)
    assert len(series) == len(DEFAULT_SERIES)


def test_default_series_ids_unique_and_wellformed():
    ids = [s["id"] for s in DEFAULT_SERIES]
    assert len(ids) == len(set(ids))
    for s in DEFAULT_SERIES:
        # id 约定 family_code，与 store 长表键重建规则一致
        assert s["id"].startswith(f"{s['family']}_")
        assert s["freq"] in {"D", "W", "M"}
        assert set(s) >= {"id", "family", "market", "code", "name", "freq", "enabled"}
