"""公共通知模块测试（无网络：monkeypatch urllib/subprocess）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from easy_tdx import notify as nf


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch) -> Path:
    """隔离 EASY_TDX_CONFIG_DIR：默认 ~/.easy_tdx 的真实 webhook 配置不影响断言。"""
    monkeypatch.setenv("EASY_TDX_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CUSTOM_WEBHOOK_URLS", raising=False)
    return tmp_path


def _write_urls(path: Path, *lines: str) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def test_webhook_urls_env_priority(tmp_config_dir, monkeypatch):
    monkeypatch.setenv("CUSTOM_WEBHOOK_URLS", "https://a.example/1, https://b.example/2")
    _write_urls(tmp_config_dir / "custom_webhook_urls", "https://file.example/3")
    assert nf.webhook_urls() == ["https://a.example/1", "https://b.example/2"]


def test_webhook_urls_file(tmp_config_dir):
    _write_urls(
        tmp_config_dir / "custom_webhook_urls",
        "# 注释",
        "https://a.example/1",
        "",
        "  https://b.example/2  ",
    )
    assert nf.webhook_urls() == ["https://a.example/1", "https://b.example/2"]


def test_webhook_urls_none(tmp_config_dir):
    assert nf.webhook_urls() == []


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_notify_success(monkeypatch, tmp_config_dir, capsys):
    monkeypatch.setenv("CUSTOM_WEBHOOK_URLS", "https://ding.example/robot")
    requests: list = []

    def fake_urlopen(req, timeout=10):
        requests.append(req)
        return _FakeResp(b'{"errcode": 0, "errmsg": "ok"}')

    monkeypatch.setattr(nf.urllib.request, "urlopen", fake_urlopen)
    nf.notify("盘中预警", "测试消息")
    assert len(requests) == 1
    req = requests[0]
    assert req.full_url == "https://ding.example/robot"
    assert req.get_header("Content-type") == "application/json"
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["msgtype"] == "text"
    assert "盘中预警" in payload["text"]["content"] and "测试消息" in payload["text"]["content"]
    assert "盘中预警" in capsys.readouterr().out  # [通知] 打印


def test_notify_errcode_failure(monkeypatch, tmp_config_dir, capsys):
    monkeypatch.setenv("CUSTOM_WEBHOOK_URLS", "https://ding.example/robot")
    monkeypatch.setattr(
        nf.urllib.request,
        "urlopen",
        lambda req, timeout=10: _FakeResp(b'{"errcode": 310000, "errmsg": "bad keyword"}'),
    )
    nf.notify("盘中预警", "x")  # 不抛异常
    assert "[钉钉发送失败] errcode=310000" in capsys.readouterr().out


def test_notify_network_error(monkeypatch, tmp_config_dir, capsys):
    monkeypatch.setenv("CUSTOM_WEBHOOK_URLS", "https://ding.example/robot")

    def boom(req, timeout=10):
        raise OSError("连接超时")

    monkeypatch.setattr(nf.urllib.request, "urlopen", boom)
    nf.notify("盘中预警", "x")  # 不抛异常
    assert "[钉钉发送失败]" in capsys.readouterr().out


def test_notify_fallback_osascript(monkeypatch, tmp_config_dir, capsys):
    calls: list[list[str]] = []
    monkeypatch.setattr(nf.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or None)
    nf.notify("盘中预警", "测试消息")
    assert len(calls) == 1
    assert calls[0][0] == "osascript"
    assert "display notification" in " ".join(calls[0])
    assert "测试消息" in " ".join(calls[0])
