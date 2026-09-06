"""LLM 客户端与配置单元测试（ai/llm.py，v1.29）。

覆盖：配置文件读写、环境变量兜底、Provider 预设补齐、api_key 脱敏、
未配置 key 的友好报错、openai/anthropic 两种协议的请求组装与响应解析
（HTTP 层 monkeypatch，零真实网络调用）。
"""

from __future__ import annotations

import asyncio

import pytest

from easy_tdx.ai import llm as llm_mod
from easy_tdx.ai.llm import (
    PROVIDER_PRESETS,
    LlmClient,
    LlmConfig,
    LlmError,
    load_config,
    mask_key,
    resolve_config,
    save_config,
)


@pytest.fixture()
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EASY_TDX_CONFIG_DIR", str(tmp_path))
    # 清掉可能存在的兜底环境变量，保证用例间互不干扰
    for var in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class TestConfigFile:
    def test_default_when_no_file(self, config_dir):
        cfg = load_config()
        assert cfg.provider == "deepseek" and cfg.api_key == ""

    def test_save_and_load_roundtrip(self, config_dir):
        save_config(LlmConfig(provider="zhipu", api_key="sk-test1234567890", model="glm-4.6"))
        cfg = load_config()
        assert cfg.provider == "zhipu"
        assert cfg.api_key == "sk-test1234567890"
        assert cfg.model == "glm-4.6"

    def test_corrupt_file_returns_default(self, config_dir):
        (config_dir / "llm.json").write_text("{not json", encoding="utf-8")
        assert load_config().provider == "deepseek"  # 不抛异常

    def test_env_fills_missing_fields(self, config_dir, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "kimi")
        monkeypatch.setenv("LLM_API_KEY", "sk-env-key-123456")
        cfg = load_config()
        assert cfg.provider == "kimi" and cfg.api_key == "sk-env-key-123456"

    def test_file_overrides_env(self, config_dir, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-env")
        save_config(LlmConfig(provider="deepseek", api_key="sk-file-12345678"))
        assert load_config().api_key == "sk-file-12345678"


class TestResolve:
    def test_preset_fills_url_and_model(self, config_dir):
        save_config(LlmConfig(provider="qwen"))
        r = resolve_config()
        assert r.api_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert r.model == "qwen-plus"

    def test_explicit_values_win(self, config_dir):
        save_config(LlmConfig(provider="deepseek", api_url="http://gw.local/v1", model="my-model"))
        r = resolve_config()
        assert r.api_url == "http://gw.local/v1" and r.model == "my-model"

    def test_custom_requires_url_and_model(self, config_dir):
        with pytest.raises(ValueError, match="不完整"):
            resolve_config(LlmConfig(provider="custom"))


class TestMaskKey:
    def test_mask(self):
        assert mask_key("") == ""
        assert mask_key("short") == "*****"
        assert mask_key("sk-abcdef1234567890") == "sk-***7890"


class TestClient:
    def test_missing_key_friendly_error(self, config_dir):
        client = LlmClient(LlmConfig(provider="deepseek", api_key=""))
        with pytest.raises(LlmError, match="API Key"):
            asyncio.run(client.chat("hi"))

    def test_ollama_needs_no_key(self, config_dir, monkeypatch):
        captured = {}

        def fake_post(url, headers, payload, timeout):
            captured.update(url=url, headers=headers, payload=payload)
            return {"choices": [{"message": {"content": "OK"}}]}

        monkeypatch.setattr(llm_mod, "_post_json", fake_post)
        client = LlmClient(LlmConfig(provider="ollama", timeout=5))
        reply = asyncio.run(client.chat("ping"))
        assert reply == "OK"
        assert captured["url"].startswith("http://localhost:11434/v1/chat/completions")
        assert "Authorization" not in captured["headers"]  # 免 key 不带鉴权头

    def test_openai_style_request_and_parse(self, config_dir, monkeypatch):
        captured = {}

        def fake_post(url, headers, payload, timeout):
            captured.update(url=url, headers=headers, payload=payload)
            return {"choices": [{"message": {"content": "解读完成"}}]}

        monkeypatch.setattr(llm_mod, "_post_json", fake_post)
        client = LlmClient(LlmConfig(provider="zhipu", api_key="sk-zhipu-123456789"))
        reply = asyncio.run(client.chat("报告…", system_prompt="SYS"))
        assert reply == "解读完成"
        assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        assert captured["headers"]["Authorization"] == "Bearer sk-zhipu-123456789"
        msgs = captured["payload"]["messages"]
        assert msgs[0] == {"role": "system", "content": "SYS"}
        assert msgs[1]["content"] == "报告…"
        assert captured["payload"]["model"] == "glm-4-flash"

    def test_anthropic_style_request_and_parse(self, config_dir, monkeypatch):
        captured = {}

        def fake_post(url, headers, payload, timeout):
            captured.update(url=url, headers=headers, payload=payload)
            return {"content": [{"type": "text", "text": "Claude 回复"}]}

        monkeypatch.setattr(llm_mod, "_post_json", fake_post)
        client = LlmClient(LlmConfig(provider="claude", api_key="sk-ant-123456789"))
        reply = asyncio.run(client.chat("hi"))
        assert reply == "Claude 回复"
        assert captured["url"] == "https://api.anthropic.com/v1/messages"
        assert captured["headers"]["x-api-key"] == "sk-ant-123456789"
        assert captured["headers"]["anthropic-version"] == "2023-06-01"
        assert captured["payload"]["system"]  # system 走顶层字段而非 messages

    def test_http_error_wrapped(self, config_dir, monkeypatch):
        """_post_json 把 HTTPError（带响应体）包装成带状态码的 LlmError。"""
        import io
        import urllib.error

        def fake_urlopen(req, timeout):
            body = io.BytesIO(b'{"error":"bad key"}')
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs=None, fp=body)

        monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(LlmError, match="401") as ei:
            llm_mod._post_json("https://x/v1/chat/completions", {}, {"m": 1}, 5.0)
        assert ei.value.status == 401
        assert "bad key" in str(ei.value)

    def test_test_endpoint_reports_failure(self, config_dir):
        client = LlmClient(LlmConfig(provider="deepseek", api_key=""))
        result = asyncio.run(client.test())
        assert result["ok"] is False and "API Key" in result["error"]


class TestApiUrlSchemeGuard:
    """api_url SSRF 防线：仅允许 http/https、禁止携带 userinfo。

    背景：_post_json 用 urllib 直连用户可配的 api_url，无 scheme 白名单时
    ``file:///...`` 可读本地文件（llm.json 内含明文 key，且格式异常分支会
    回显响应前 300 字节）、``ftp://`` 与内网 http 可被当跳板。
    """

    def test_file_scheme_rejected(self):
        with pytest.raises(ValueError, match="http"):
            resolve_config(
                LlmConfig(provider="custom", api_url="file:///C:/Users/x/llm.json", model="m")
            )

    def test_ftp_scheme_rejected(self):
        with pytest.raises(ValueError, match="http"):
            resolve_config(LlmConfig(provider="custom", api_url="ftp://internal-host/x", model="m"))

    def test_missing_scheme_rejected(self):
        with pytest.raises(ValueError, match="http"):
            resolve_config(LlmConfig(provider="custom", api_url="api.deepseek.com/v1", model="m"))

    def test_userinfo_rejected(self):
        with pytest.raises(ValueError, match="user:pass"):
            resolve_config(
                LlmConfig(provider="custom", api_url="https://user:pass@api.x.com/v1", model="m")
            )

    def test_http_https_case_insensitive_allowed(self):
        r = resolve_config(LlmConfig(provider="custom", api_url="HTTPS://Api.X.com/v1", model="m"))
        assert r.api_url == "HTTPS://Api.X.com/v1"
        r2 = resolve_config(
            LlmConfig(provider="custom", api_url="http://gw.local:8000/v1", model="m")
        )
        assert r2.api_url == "http://gw.local:8000/v1"

    def test_preset_urls_still_resolve(self, config_dir):
        save_config(LlmConfig(provider="deepseek", api_key="sk-x-1234567890"))
        r = resolve_config()
        assert r.api_url == "https://api.deepseek.com/v1"


class TestHttpPostHardening:
    """HTTP 层加固：错误不回显原始 body、响应体大小上限。"""

    def _raise_http_error(self, body: bytes, code: int = 401):
        import io
        import urllib.error

        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, code, "Unauthorized", hdrs=None, fp=io.BytesIO(body)
            )

        return fake_urlopen

    def test_http_error_extracts_provider_message_only(self, monkeypatch):
        """错误响应只回显 provider 的 error.message，不回显原始 body 其他内容。"""
        import json as _json

        body = _json.dumps(
            {"error": {"message": "Invalid API key", "internal_hint": "SECRET-STACK"}}
        ).encode()
        monkeypatch.setattr(llm_mod.urllib.request, "urlopen", self._raise_http_error(body))
        with pytest.raises(LlmError) as ei:
            llm_mod._post_json("https://x/v1/chat/completions", {}, {"m": 1}, 5.0)
        assert ei.value.status == 401
        assert "Invalid API key" in str(ei.value)
        assert "SECRET-STACK" not in str(ei.value)

    def test_http_error_non_json_body_is_generic(self, monkeypatch):
        """非 JSON 错误页不给原始内容，只给通用 HTTP 状态描述。"""
        body = b"<html><h1>gateway exploded with internal detail</h1></html>"
        monkeypatch.setattr(llm_mod.urllib.request, "urlopen", self._raise_http_error(body))
        with pytest.raises(LlmError) as ei:
            llm_mod._post_json("https://x/v1/chat/completions", {}, {"m": 1}, 5.0)
        assert "gateway exploded" not in str(ei.value)
        assert "401" in str(ei.value)

    def test_http_error_string_error_field_still_shown(self, monkeypatch):
        """error 为字符串的网关（如 {"error":"bad key"}）仍展示该消息。"""
        monkeypatch.setattr(
            llm_mod.urllib.request, "urlopen", self._raise_http_error(b'{"error":"bad key"}')
        )
        with pytest.raises(LlmError, match="bad key") as ei:
            llm_mod._post_json("https://x/v1/chat/completions", {}, {"m": 1}, 5.0)
        assert ei.value.status == 401

    def test_response_body_size_capped(self, config_dir, monkeypatch):
        """超过 2MB 的响应体中止解析（防异常网关撑爆内存），报可操作错误。"""

        class _FakeResp:
            def __init__(self, payload: bytes) -> None:
                self._buf = payload

            def read(self, n: int = -1) -> bytes:
                if n < 0:
                    data, self._buf = self._buf, b""
                    return data
                data, self._buf = self._buf[:n], self._buf[n:]
                return data

            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

        big = b"x" * (llm_mod._MAX_RESPONSE_BYTES + 1)
        monkeypatch.setattr(llm_mod.urllib.request, "urlopen", lambda req, timeout: _FakeResp(big))
        with pytest.raises(LlmError, match="过大|上限"):
            llm_mod._post_json("https://x/v1/chat/completions", {}, {"m": 1}, 5.0)

    def test_normal_response_within_cap_parses(self, config_dir, monkeypatch):
        class _FakeResp:
            def __init__(self, payload: bytes) -> None:
                self._buf = payload

            def read(self, n: int = -1) -> bytes:
                if n < 0:
                    data, self._buf = self._buf, b""
                    return data
                data, self._buf = self._buf[:n], self._buf[n:]
                return data

            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

        payload = b'{"choices": [{"message": {"content": "OK"}}]}'
        monkeypatch.setattr(
            llm_mod.urllib.request, "urlopen", lambda req, timeout: _FakeResp(payload)
        )
        data = llm_mod._post_json("https://x/v1/chat/completions", {}, {"m": 1}, 5.0)
        assert data["choices"][0]["message"]["content"] == "OK"


class TestSaveConfigAtomic:
    def test_replace_failure_preserves_old_file(self, config_dir, monkeypatch):
        """os.replace 失败（磁盘满等）时旧配置原样保留，不留临时文件。"""
        save_config(LlmConfig(provider="deepseek", api_key="sk-old-1234567890"))

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(llm_mod.os, "replace", boom)
        with pytest.raises(OSError):
            save_config(LlmConfig(provider="kimi", api_key="sk-new-9999999999"))

        assert load_config().api_key == "sk-old-1234567890"  # 旧配置未被破坏
        leftovers = [p.name for p in config_dir.iterdir() if p.name != "llm.json"]
        assert leftovers == []  # 失败的临时文件已清理


class TestLoadConfigFieldDefense:
    """手工编辑 llm.json 的脏字段不得打挂 load_config（全部 /llm/* 依赖它）。"""

    def test_null_fields_fall_back_to_defaults(self, config_dir):
        import json as _json

        (config_dir / "llm.json").write_text(
            _json.dumps(
                {
                    "provider": None,
                    "api_url": None,
                    "api_key": None,
                    "model": None,
                    "temperature": None,
                    "max_tokens": None,
                    "timeout": None,
                    "system_prompt": None,
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config()  # 旧码：float(None) TypeError
        assert cfg.provider == "deepseek"
        assert cfg.api_url == "" and cfg.api_key == "" and cfg.model == ""
        assert cfg.temperature == 0.3
        assert cfg.max_tokens == 16000
        assert cfg.timeout == 180.0
        assert cfg.system_prompt == LlmConfig.system_prompt

    def test_wrong_types_fall_back_with_warning(self, config_dir, caplog):
        import json as _json

        (config_dir / "llm.json").write_text(
            _json.dumps(
                {
                    "temperature": "abc",
                    "max_tokens": "fast",
                    "timeout": [],
                    "provider": 123,
                    "system_prompt": 456,
                }
            ),
            encoding="utf-8",
        )
        with caplog.at_level("WARNING", logger="easy_tdx.ai.llm"):
            cfg = load_config()  # 旧码：float("abc") ValueError
        assert cfg.temperature == 0.3
        assert cfg.max_tokens == 16000
        assert cfg.timeout == 180.0
        assert cfg.provider == "deepseek"  # 非字符串 provider 回退默认
        assert cfg.system_prompt == LlmConfig.system_prompt
        assert any("temperature" in r.message for r in caplog.records)

    def test_non_finite_and_out_of_range_fall_back(self, config_dir):
        import json as _json

        (config_dir / "llm.json").write_text(
            _json.dumps({"temperature": 1e999, "timeout": -5, "max_tokens": 0}),  # 1e999→inf
            encoding="utf-8",
        )
        cfg = load_config()  # 旧码：inf temperature 会一路写进请求 payload
        assert cfg.temperature == 0.3
        assert cfg.timeout == 180.0
        assert cfg.max_tokens == 16000

    def test_string_numbers_leniently_coerced(self, config_dir):
        import json as _json

        (config_dir / "llm.json").write_text(
            _json.dumps({"temperature": "0.7", "max_tokens": "8192.9", "timeout": "60"}),
            encoding="utf-8",
        )
        cfg = load_config()
        assert cfg.temperature == 0.7
        assert cfg.max_tokens == 8192
        assert cfg.timeout == 60.0


class TestAnthropicRobustness:
    """anthropic 协议与 openai 口径对齐：绝不静默返回空正文。"""

    def _client(self) -> LlmClient:
        return LlmClient(LlmConfig(provider="claude", api_key="sk-ant-123456789"))

    def test_thinking_only_blocks_raise_actionable(self, config_dir, monkeypatch):
        """仅 thinking 块（max_tokens 被思考耗尽）→ 可操作错误，而非空串成功。"""

        def fake_post(url, headers, payload, timeout):
            return {
                "content": [{"type": "thinking", "thinking": "思考" * 200}],
                "stop_reason": "max_tokens",
            }

        monkeypatch.setattr(llm_mod, "_post_json", fake_post)
        with pytest.raises(LlmError, match="思考链"):
            asyncio.run(self._client().chat("hi"))

    def test_content_as_plain_string_accepted(self, config_dir, monkeypatch):
        """部分网关把 content 放字符串而非块列表——正常取正文。"""

        def fake_post(url, headers, payload, timeout):
            return {"content": "纯字符串回复"}

        monkeypatch.setattr(llm_mod, "_post_json", fake_post)
        assert asyncio.run(self._client().chat("hi")) == "纯字符串回复"

    def test_mixed_blocks_text_extracted(self, config_dir, monkeypatch):
        def fake_post(url, headers, payload, timeout):
            return {
                "content": [
                    {"type": "thinking", "thinking": "思考"},
                    {"type": "text", "text": "正文"},
                ],
                "stop_reason": "end_turn",
            }

        monkeypatch.setattr(llm_mod, "_post_json", fake_post)
        assert asyncio.run(self._client().chat("hi")) == "正文"

    def test_missing_content_raises_llm_error(self, config_dir, monkeypatch):
        """content 缺失 → LlmError（旧码 AttributeError 裸 500）。"""

        def fake_post(url, headers, payload, timeout):
            return {"stop_reason": "end_turn"}

        monkeypatch.setattr(llm_mod, "_post_json", fake_post)
        with pytest.raises(LlmError, match="格式异常"):
            asyncio.run(self._client().chat("hi"))

    def test_empty_blocks_generic_error_without_raw_echo(self, config_dir, monkeypatch):
        def fake_post(url, headers, payload, timeout):
            return {"content": [{"type": "tool_use", "id": "tool_1", "secret": "S3CR3T"}]}

        monkeypatch.setattr(llm_mod, "_post_json", fake_post)
        with pytest.raises(LlmError, match="content 为空") as ei:
            asyncio.run(self._client().chat("hi"))
        assert "S3CR3T" not in str(ei.value)  # 不回显原始响应体

    def test_empty_string_content_with_max_tokens_stop(self, config_dir, monkeypatch):
        def fake_post(url, headers, payload, timeout):
            return {"content": "", "stop_reason": "max_tokens"}

        monkeypatch.setattr(llm_mod, "_post_json", fake_post)
        with pytest.raises(LlmError, match="截断"):
            asyncio.run(self._client().chat("hi"))


def test_provider_presets_cover_major_vendors():
    vendors = [
        "deepseek",
        "qwen",
        "zhipu",
        "kimi",
        "minimax",
        "openai",
        "claude",
        "ollama",
        "custom",
    ]
    for pid in vendors:
        assert pid in PROVIDER_PRESETS, pid
    assert PROVIDER_PRESETS["claude"].api_style == "anthropic"
    assert PROVIDER_PRESETS["ollama"].needs_key is False
    assert PROVIDER_PRESETS["zhipu"].base_url.startswith("https://open.bigmodel.cn")


class TestTimeoutSemantics:
    def test_default_timeout_is_generous(self):
        """默认超时 ≥120s：非流式接口需等模型生成完整段回复（大报告 1-3 分钟）。"""
        assert LlmConfig().timeout >= 120

    def test_read_timeout_actionable_message(self, monkeypatch):
        """读超时单独成类报错，文案给出「调大超时」动作而非裸异常。"""

        def fake_urlopen(req, timeout):
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(LlmError, match="请求超时（180s"):
            llm_mod._post_json("https://x/v1/chat/completions", {}, {"m": 1}, 180.0)

    def test_connect_timeout_via_urlerror(self, monkeypatch):
        """连接期超时（URLError.reason=TimeoutError）同样走超时文案。"""
        import urllib.error

        def fake_urlopen(req, timeout):
            raise urllib.error.URLError(TimeoutError("timed out"))

        monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(LlmError, match="请求超时"):
            llm_mod._post_json("https://x/v1/chat/completions", {}, {"m": 1}, 30.0)


class TestAsyncChatTask:
    """POST /llm/chat/async + GET /llm/chat/tasks/{id} 的提交-轮询闭环。"""

    @pytest.fixture(autouse=True)
    def _fresh_history_store(self, config_dir):
        """每个用例用独立的 llm_history.db（模块级单例绑定了首个用例的临时目录）。"""
        import easy_tdx.web.llm_history_store as hs

        hs._store = None
        yield
        hs._store = None

    def test_submit_and_poll_done(self, config_dir, monkeypatch):
        import time

        from fastapi.testclient import TestClient

        from easy_tdx.web.app import _create_app

        def fake_chat(self, prompt, system_prompt=None):
            async def _slow():
                await asyncio.sleep(0.05)
                return f"解读:{prompt[:8]}"

            return _slow()

        monkeypatch.setattr(LlmClient, "chat", fake_chat)
        app = _create_app(enable_mac=False, enable_ui=False)
        with TestClient(app) as c:
            r = c.post("/api/v1/llm/chat/async", json={"prompt": "整份回测报告…" * 10})
            assert r.status_code == 202, r.text
            task_id = r.json()["task_id"]
            assert r.json()["status"] in ("pending", "running")

            state = None
            for _ in range(50):
                state = c.get(f"/api/v1/llm/chat/tasks/{task_id}").json()
                if state["status"] in ("done", "failed"):
                    break
                time.sleep(0.05)
            assert state["status"] == "done", state
            assert state["result"]["reply"].startswith("解读:")
            assert state["result"]["elapsed"] >= 0.0

    def test_task_failure_surfaces_error(self, config_dir, monkeypatch):
        import time

        from fastapi.testclient import TestClient

        from easy_tdx.web.app import _create_app

        def fake_chat(self, prompt, system_prompt=None):
            async def _boom():
                raise LlmError("请求超时（180s 内无响应）")

            return _boom()

        monkeypatch.setattr(LlmClient, "chat", fake_chat)
        app = _create_app(enable_mac=False, enable_ui=False)
        with TestClient(app) as c:
            task_id = c.post("/api/v1/llm/chat/async", json={"prompt": "x"}).json()["task_id"]
            state = None
            for _ in range(50):
                state = c.get(f"/api/v1/llm/chat/tasks/{task_id}").json()
                if state["status"] in ("done", "failed"):
                    break
                time.sleep(0.05)
            assert state["status"] == "failed"
            assert "请求超时" in state["error"]

    def test_unknown_task_rejected(self, config_dir):
        """未知 task → 400（与 GET /backtest/tasks/{id} 的 ValueError 约定一致）。"""
        from fastapi.testclient import TestClient

        from easy_tdx.web.app import _create_app

        app = _create_app(enable_mac=False, enable_ui=False)
        with TestClient(app) as c:
            r = c.get("/api/v1/llm/chat/tasks/nonexistent")
            assert r.status_code == 400
            assert "未知任务" in r.json()["detail"]

    def test_async_success_records_history(self, config_dir, monkeypatch):
        """异步解读成功 → 自动落历史库（含策略上下文），供历史页查询。"""
        import time as _time

        from fastapi.testclient import TestClient

        from easy_tdx.web.app import _create_app

        async def fake_chat(self, prompt, system_prompt=None):
            return "解读正文"

        monkeypatch.setattr(LlmClient, "chat", fake_chat)
        app = _create_app(enable_mac=False, enable_ui=False)
        with TestClient(app) as c:
            ctx = {
                "strategy": "ma_cross",
                "strategy_label": "双均线交叉",
                "symbol": "600519",
                "category": "DAY",
                "params": {"fast": 5, "slow": 20},
                "start_date": "2024-01-01",
                "end_date": "2025-01-01",
            }
            tid = c.post("/api/v1/llm/chat/async", json={"prompt": "报告", "context": ctx}).json()[
                "task_id"
            ]
            for _ in range(50):
                st = c.get(f"/api/v1/llm/chat/tasks/{tid}").json()
                if st["status"] in ("done", "failed"):
                    break
                _time.sleep(0.05)
            assert st["status"] == "done", st

            hist = c.get("/api/v1/llm/history").json()
            assert hist["count"] >= 1
            item = hist["items"][0]
            assert item["reply"] == "解读正文"
            assert item["strategy"] == "ma_cross" and item["symbol"] == "600519"
            assert item["params"] == {"fast": 5, "slow": 20}

            # 删除一条
            r = c.delete(f"/api/v1/llm/history/{item['id']}")
            assert r.json()["ok"] is True
            assert c.get("/api/v1/llm/history").json()["count"] == hist["count"] - 1

    def test_async_failure_not_recorded(self, config_dir, monkeypatch):
        """解读失败 → 不落历史（历史只归档成功解读）。"""
        import time as _time

        from fastapi.testclient import TestClient

        from easy_tdx.web.app import _create_app

        async def fake_chat(self, prompt, system_prompt=None):
            raise LlmError("boom")

        monkeypatch.setattr(LlmClient, "chat", fake_chat)
        app = _create_app(enable_mac=False, enable_ui=False)
        with TestClient(app) as c:
            tid = c.post("/api/v1/llm/chat/async", json={"prompt": "x"}).json()["task_id"]
            for _ in range(50):
                st = c.get(f"/api/v1/llm/chat/tasks/{tid}").json()
                if st["status"] in ("done", "failed"):
                    break
                _time.sleep(0.05)
            assert st["status"] == "failed"
            assert c.get("/api/v1/llm/history").json()["count"] == 0

    def test_submit_rejects_incomplete_config(self, config_dir):
        """custom 未填 url/model：提交期即 400（不等任务跑起来才失败）。"""
        from fastapi.testclient import TestClient

        from easy_tdx.web.app import _create_app

        app = _create_app(enable_mac=False, enable_ui=False)
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/llm/chat/async",
                json={"prompt": "x", "override": {"provider": "custom"}},
            )
            assert r.status_code == 400
            assert "不完整" in r.json()["detail"]


class TestThinkingModelBlankContent:
    """思考型模型正文空白（reasoning_content 耗尽 max_tokens）的防御。

    v1.29.1 实测：GLM-5.x 思考链计入 max_tokens，预算耗尽时 content 为
    空白——truthy 但渲染为空（状态条报成功、正文空白）。解析层必须把
    这类响应转成可操作的错误，绝不返回空白字符串。
    """

    def _client(self, max_tokens: int = 4000) -> LlmClient:
        return LlmClient(
            LlmConfig(
                provider="zhipu",
                api_key="sk-x-1234567890",
                model="glm-5.3-flash",
                max_tokens=max_tokens,
            )
        )

    def test_normal_content_wins_over_reasoning(self):
        msg = {"content": "正文", "reasoning_content": "思考…", "role": "assistant"}
        assert self._client()._extract_reply_openai(msg, "stop") == "正文"

    def test_blank_content_with_reasoning_raises_actionable(self):
        msg = {"content": "   ", "reasoning_content": "思考" * 500, "role": "assistant"}
        with pytest.raises(LlmError, match="思考链.*4000.*16000"):
            self._client()._extract_reply_openai(msg, "length")

    def test_null_content_with_reasoning(self):
        msg = {"content": None, "reasoning_content": "思考", "role": "assistant"}
        with pytest.raises(LlmError, match="思考链"):
            self._client()._extract_reply_openai(msg, "length")

    def test_blank_content_without_reasoning(self):
        with pytest.raises(LlmError, match="content 为空"):
            self._client()._extract_reply_openai({"content": ""}, "stop")

    def test_length_finish_without_content(self):
        with pytest.raises(LlmError, match="截断"):
            self._client()._extract_reply_openai({"content": ""}, "length")

    def test_whitespace_reply_rejected_end_to_end(self, config_dir, monkeypatch):
        """端到端：伪 HTTP 返回空白正文 → chat() 抛错（任务态 failed 而非 done 空回复）。"""

        def fake_post(url, headers, payload, timeout):
            blank = chr(10) + "  " + chr(10)
            return {
                "choices": [
                    {
                        "message": {"content": blank, "reasoning_content": "r"},
                        "finish_reason": "length",
                    }
                ]
            }

        monkeypatch.setattr(llm_mod, "_post_json", fake_post)
        with pytest.raises(LlmError, match="思考链"):
            asyncio.run(self._client().chat("报告"))

    def test_default_max_tokens_generous_for_thinking(self):
        assert LlmConfig().max_tokens >= 16000
