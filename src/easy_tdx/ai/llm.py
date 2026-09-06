"""多 Provider LLM 客户端：配置解析 + HTTP 调用。

零第三方依赖：HTTP 走标准库 urllib（经 ``asyncio.to_thread`` 异步化），
FastAPI 路由可直接 ``await``。

Provider 预设（``api_style``）：

===========  ========  ==============================================  ==================
provider     协议      base_url                                        默认模型
===========  ========  ==============================================  ==================
deepseek     openai    https://api.deepseek.com/v1                     deepseek-chat
qwen         openai    https://dashscope.aliyuncs.com/compatible-mode  qwen-plus
                       /v1
zhipu        openai    https://open.bigmodel.cn/api/paas/v4            glm-4-flash
kimi         openai    https://api.moonshot.cn/v1                      moonshot-v1-8k
minimax      openai    https://api.minimaxi.chat/v1                    MiniMax-Text-01
openai       openai    https://api.openai.com/v1                       gpt-4o-mini
claude       anthropic https://api.anthropic.com/v1                     claude-sonnet-4-5
ollama       openai    http://localhost:11434/v1                       qwen2.5:7b
custom       openai    （用户填写）                                     （用户填写）
===========  ========  ==============================================  ==================

预设的 base_url/默认模型只是初始填充值——WebUI 或 JSON 文件里均可覆盖
（自定义网关/代理场景直接改 url 即可）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

__all__ = [
    "PROVIDER_PRESETS",
    "LlmClient",
    "LlmConfig",
    "load_config",
    "mask_key",
    "resolve_config",
    "save_config",
    "validate_api_url",
]

logger = logging.getLogger(__name__)

#: 配置文件名（落在 EASY_TDX_CONFIG_DIR，与 watchlist/strategies 同目录）。
LLM_CONFIG_FILENAME = "llm.json"


@dataclass
class ProviderPreset:
    """单个 Provider 的展示信息与默认填充值。"""

    id: str
    label: str
    base_url: str
    default_model: str
    api_style: str = "openai"  # "openai" | "anthropic"
    needs_key: bool = True  # ollama 本地服务无需 key

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "api_style": self.api_style,
            "needs_key": self.needs_key,
        }


#: Provider 预设表（WebUI 下拉框数据源 + 未配置字段的兜底默认值）。
PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    p.id: p
    for p in (
        ProviderPreset("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
        ProviderPreset(
            "qwen",
            "通义千问 Qwen",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen-plus",
        ),
        ProviderPreset("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
        ProviderPreset("kimi", "Kimi (月之暗面)", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
        ProviderPreset("minimax", "MiniMax", "https://api.minimaxi.chat/v1", "MiniMax-Text-01"),
        ProviderPreset("openai", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
        ProviderPreset(
            "claude",
            "Claude (Anthropic)",
            "https://api.anthropic.com/v1",
            "claude-sonnet-4-5",
            api_style="anthropic",
        ),
        ProviderPreset(
            "ollama",
            "Ollama（本地）",
            "http://localhost:11434/v1",
            "qwen2.5:7b",
            needs_key=False,
        ),
        ProviderPreset("custom", "自定义（OpenAI 兼容）", "", ""),
    )
}


@dataclass
class LlmConfig:
    """LLM 调用配置（WebUI 表单与 llm.json 的公共结构）。"""

    provider: str = "deepseek"
    api_url: str = ""  # 留空 = 用预设 base_url
    api_key: str = ""
    model: str = ""  # 留空 = 用预设默认模型
    temperature: float = 0.3
    # max_tokens 是"上限"而非目标（按实际生成计费）：思考型模型的思考链
    # 计入该预算，4000 会被整份报告的思考轻易耗尽导致正文空白，默认给足
    timeout: float = 180.0
    max_tokens: int = 16000
    system_prompt: str = field(
        default="你是一位严谨的 A 股量化投研分析师，基于给定的数据客观分析，"
        "不确定的内容明确说明，不构成投资建议。"
    )

    def to_dict(self, *, mask_api_key: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if mask_api_key:
            d["api_key"] = mask_key(self.api_key)
        return d


# ── 配置读写（文件 > 环境变量 > 预设） ────────────────────────────────────────


def config_path() -> Path:
    """配置文件路径（``$EASY_TDX_CONFIG_DIR/llm.json``，默认 ``~/.easy_tdx``）。"""
    base = Path(os.environ.get("EASY_TDX_CONFIG_DIR", str(Path.home() / ".easy_tdx")))
    return base / LLM_CONFIG_FILENAME


def _read_config_file() -> dict[str, Any]:
    """直读 llm.json（无缓存——手工编辑即时生效）。损坏/不存在返回空 dict。"""
    p = config_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取 LLM 配置失败 %s: %s", p, exc)
        return {}


def load_config() -> LlmConfig:
    """加载配置：llm.json 显式字段 > 环境变量兜底（未填字段仍为空，调用时再取预设）。

    字段级防御：llm.json 常被手工编辑，单个字段类型不对（``"temperature":
    null`` / ``"abc"`` 等）只记 warning 并回退默认值，绝不让 load_config
    抛异常打挂全部 /llm/* 端点。
    """
    data = _read_config_file()
    env_url = os.environ.get("LLM_BASE_URL", "")
    return LlmConfig(
        provider=_clean_str(data.get("provider") or os.environ.get("LLM_PROVIDER", ""), "provider")
        or "deepseek",
        api_url=_clean_str(data.get("api_url") or env_url, "api_url"),
        api_key=_clean_str(data.get("api_key") or os.environ.get("LLM_API_KEY", ""), "api_key"),
        model=_clean_str(data.get("model") or os.environ.get("LLM_MODEL", ""), "model"),
        temperature=_clean_float(data.get("temperature"), "temperature", 0.3, minimum=0.0),
        max_tokens=_clean_int(data.get("max_tokens"), "max_tokens", 16000, minimum=1),
        timeout=_clean_float(data.get("timeout"), "timeout", 180.0, minimum=0.1),
        system_prompt=_clean_str(
            data.get("system_prompt") or LlmConfig.system_prompt, "system_prompt"
        )
        or LlmConfig.system_prompt,
    )


def _clean_str(value: Any, field_name: str) -> str:
    """字符串字段清洗：非 None 的非字符串（数字/列表等）记 warning 后回退空串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        logger.warning(
            "LLM 配置字段 %s 应为字符串，收到 %s(%r)，已忽略并回退默认值",
            field_name,
            type(value).__name__,
            value,
        )
        return ""
    return value


def _clean_float(
    value: Any, field_name: str, default: float, *, minimum: float | None = None
) -> float:
    """数值字段清洗：非法/非有限/低于下限均 warning 后回退默认。"""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "LLM 配置字段 %s 应为数字，收到 %r，回退默认值 %s", field_name, value, default
        )
        return default
    if not math.isfinite(f) or (minimum is not None and f < minimum):
        logger.warning(
            "LLM 配置字段 %s 超出合理范围（%r），回退默认值 %s", field_name, value, default
        )
        return default
    return f


def _clean_int(value: Any, field_name: str, default: int, *, minimum: int | None = None) -> int:
    """整数字段清洗：接受 "8192.9" 这类字符串的宽松矫正（截断到 int）。"""
    if value is None:
        return default
    try:
        i = int(value)
    except (TypeError, ValueError):
        try:
            i = int(float(value))
        except (TypeError, ValueError):
            logger.warning(
                "LLM 配置字段 %s 应为整数，收到 %r，回退默认值 %s", field_name, value, default
            )
            return default
    if not math.isfinite(i) or (minimum is not None and i < minimum):
        logger.warning(
            "LLM 配置字段 %s 超出合理范围（%r），回退默认值 %s", field_name, value, default
        )
        return default
    return i


def save_config(cfg: LlmConfig) -> Path:
    """写入 llm.json（WebUI 保存入口；目录惰性创建；原子写）。

    先写同目录临时文件再 ``os.replace``——写一半崩溃/断电不会留下损坏的
    llm.json（损坏的后果是下次 load 静默回空配置，用户要重填 key）。
    """
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".llm-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p


def resolve_config(cfg: LlmConfig | None = None) -> LlmConfig:
    """把配置的空字段用 Provider 预设补齐，得到可直接调用的完整配置。

    - ``api_url`` 空 → 预设 ``base_url``；
    - ``model`` 空 → 预设 ``default_model``；
    - provider 无预设（拼错）→ 按 custom 处理，url/model 必须已填；
    - ``api_url`` 强制 http/https 且禁 userinfo（SSRF/本地文件读取防线）。

    Raises:
        ValueError: 补齐后仍缺 api_url 或 model（custom 未填全），
            或 api_url 非法（非 http/https、携带 user:pass@）。
    """
    c = replace(cfg or load_config())
    preset = PROVIDER_PRESETS.get(c.provider, PROVIDER_PRESETS["custom"])
    if not c.api_url:
        c.api_url = preset.base_url
    if not c.model:
        c.model = preset.default_model
    if not c.api_url or not c.model:
        raise ValueError(
            f"LLM 配置不完整：provider={c.provider} 缺少 api_url 或 model，请在 AI 设置中补全"
        )
    validate_api_url(c.api_url)
    return c


def mask_key(key: str) -> str:
    """API Key 脱敏展示：保头 3 尾 4，中间打码（短 key 全打码）。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:3]}***{key[-4:]}"


# ── HTTP 客户端（标准库实现） ─────────────────────────────────────────────────


class LlmError(RuntimeError):
    """LLM 调用失败（网络/鉴权/响应格式）。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


#: 响应体大小上限：正常 chat 响应远小于此（max_tokens 128k 的纯文本约几百 KB），
#: 超限说明对端异常（如把 api_url 配成了下载地址），及时中止防内存被撑爆。
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_READ_CHUNK = 64 * 1024

#: 允许的 URL scheme（SSRF / 本地文件读取防线：file:// 会读本地文件、
#: ftp:// 与内网 http 可被当跳板——resolve_config 里强制校验）。
_ALLOWED_URL_SCHEMES = ("http", "https")


def validate_api_url(url: str) -> None:
    """api_url 安全校验：仅 http/https、禁止携带 userinfo（user:pass@）。

    Raises:
        ValueError: scheme 非法/缺失，或 URL 携带用户凭据（web 层已有
            ValueError→错误响应通道，CLI 场景同样可直接展示）。
    """
    if not url:
        return
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"LLM api_url 非法：仅允许 http/https 地址（收到 scheme={scheme!r}）——"
            "file/ftp 等协议已禁用；缺前缀时请补 http:// 或 https://"
        )
    if parts.username or parts.password:
        raise ValueError(
            "LLM api_url 非法：不允许携带用户凭据（user:pass@host 形式）——"
            "请把鉴权放到 API Key 字段（请求头），而不是 URL 里"
        )


def _read_capped(fp: Any, cap: int = _MAX_RESPONSE_BYTES) -> bytes:
    """分块读响应体，超过 cap 字节即中止（防异常网关撑爆内存）。"""
    buf = bytearray()
    while True:
        chunk = fp.read(_READ_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap:
            raise LlmError(
                f"LLM API 响应超过 {cap // (1024 * 1024)}MB 上限——对端不是正常的 chat 接口"
                "（请检查 api_url 是否填错），已中止"
            )
    return bytes(buf)


def _extract_error_message(body: str) -> str | None:
    """从 provider 错误响应中提取可读 message（不回显原始 body）。

    OpenAI/Anthropic 兼容网关的惯例是 ``{"error": {"message": ...}}``；
    部分网关 error 直接是字符串，或把 message 放顶层。提取结果截到 300 字符。
    """
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    err = obj.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("msg") or err.get("code")
        if msg:
            return str(msg)[:300]
    elif isinstance(err, str) and err:
        return err[:300]
    msg = obj.get("message")
    if msg:
        return str(msg)[:300]
    return None


def _post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    """同步 POST JSON（在线程池里跑），返回解析后的 JSON。

    urllib 默认带 ``User-Agent: Python-urllib``，部分网关拒绝——显式带 UA。
    超时单独成类报错：非流式 chat 接口要等模型**整段回复生成完**才回包，
    大 Prompt（如整份回测报告解读）生成 1-3 分钟很正常，读超时≠网络故障，
    报错必须把「调大超时」这个动作说清楚（v1.29.1 实测踩坑）。
    HTTP 错误只回显 provider 的 error.message（≤300 字符），不透传原始 body。
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"User-Agent": "easy-tdx/llm", "Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _read_capped(resp)
            data: dict[str, Any] = json.loads(raw.decode("utf-8", errors="replace"))
            return data
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(_READ_CHUNK).decode("utf-8", errors="replace")
        except OSError:
            body = ""
        detail = _extract_error_message(body) or "接口返回错误响应"
        raise LlmError(f"LLM API HTTP {exc.code}: {detail}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise LlmError(_timeout_message(timeout)) from exc
        raise LlmError(f"LLM API 网络错误: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LlmError(_timeout_message(timeout)) from exc
    except json.JSONDecodeError as exc:
        raise LlmError(
            "LLM API 响应不是合法 JSON——api_url 可能不是 chat 接口端点，请检查 AI 设置"
        ) from exc


def _timeout_message(timeout: float) -> str:
    return (
        f"请求超时（{timeout:.0f}s 内无响应）——非流式接口需等模型生成完整段回复，"
        "大报告解读 1-3 分钟属正常。可在「AI 设置」调大「超时（秒）」，"
        "或换生成更快的模型后重试"
    )


class LlmClient:
    """单次配置快照的 LLM 调用客户端（无连接状态，可随时重建）。"""

    def __init__(self, cfg: LlmConfig | None = None) -> None:
        self._cfg = resolve_config(cfg)

    @property
    def config(self) -> LlmConfig:
        return self._cfg

    async def chat(self, prompt: str, system_prompt: str | None = None) -> str:
        """发一轮对话，返回模型回复文本。

        Args:
            prompt: 用户消息（如回测报告组装成的解读 Prompt）。
            system_prompt: 系统提示，None = 用配置里的默认。

        Raises:
            LlmError: 网络/鉴权/格式错误（含未配置 api_key 的场景）。
        """
        cfg = self._cfg
        preset = PROVIDER_PRESETS.get(cfg.provider, PROVIDER_PRESETS["custom"])
        if preset.needs_key and not cfg.api_key:
            raise LlmError(
                f"未配置 {preset.label} 的 API Key——请在 WebUI「AI 设置」页"
                "或 ~/.easy_tdx/llm.json 中填写（或设置 LLM_API_KEY 环境变量）"
            )
        system = system_prompt if system_prompt is not None else cfg.system_prompt
        return await asyncio.to_thread(self._chat_sync, prompt, system, preset.api_style)

    # -- 同步实现（to_thread 里跑） --------------------------------------------

    def _chat_sync(self, prompt: str, system: str, api_style: str) -> str:
        if api_style == "anthropic":
            return self._chat_anthropic(prompt, system)
        return self._chat_openai(prompt, system)

    def _chat_openai(self, prompt: str, system: str) -> str:
        cfg = self._cfg
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        payload = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
        }
        url = f"{cfg.api_url.rstrip('/')}/chat/completions"
        data = _post_json(url, headers, payload, cfg.timeout)
        try:
            message = data["choices"][0]["message"]
            finish = str(data["choices"][0].get("finish_reason") or "")
            return self._extract_reply_openai(message, finish)
        except LlmError:
            raise
        except (KeyError, IndexError, TypeError) as exc:
            # 不回显原始响应体：内容可能包含网关内部信息，且 historical 上
            # 曾被当作任意 URL 响应的回读通道。只描述缺什么 + 顶层键名。
            hint = sorted(data.keys()) if isinstance(data, dict) else type(data).__name__
            raise LlmError(
                "LLM 响应格式异常：未找到 choices[0].message 字段"
                f"（响应顶层字段: {hint}）——请检查 api_url 是否为正确的"
                " chat/completions 端点、模型名是否正确"
            ) from exc

    def _extract_reply_openai(self, message: dict[str, Any], finish: str) -> str:
        """从 OpenAI 兼容响应的 message 里提取正文，处理思考型模型的空白正文。

        思考型模型（GLM-5.x / DeepSeek-R1 / o 系列等）的 ``reasoning_content``
        计入 max_tokens：预算被思考链耗尽时 ``content`` 为空白——truthy 但
        渲染为空（v1.29.1 实测：状态条报成功、正文空白）。这里显式拦截：
        空白正文一律报可操作的错误（提示调大 max_tokens），绝不返回空串。
        """
        content = message.get("content")
        text = str(content) if content is not None else ""
        if text.strip():
            return text
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            raise LlmError(
                f"模型只返回了思考链（reasoning_content {len(str(reasoning))} 字），"
                f"未生成正文——max_tokens={self._cfg.max_tokens} 大概率被思考耗尽"
                f"（finish_reason={finish or 'unknown'}）。"
                "请在「AI 设置」把 Max Tokens 调大（思考型模型建议 ≥16000）后重试"
            )
        if finish == "length":
            raise LlmError(
                "模型输出被 max_tokens 截断且无正文，请在「AI 设置」调大 Max Tokens 后重试"
            )
        keys = sorted(message.keys()) if isinstance(message, dict) else type(message).__name__
        raise LlmError(
            f"LLM 响应 message.content 为空（message 字段: {keys}，"
            f"finish_reason={finish or 'unknown'}）——请检查模型名与 api_url 是否匹配"
        )

    def _chat_anthropic(self, prompt: str, system: str) -> str:
        cfg = self._cfg
        headers = {
            "x-api-key": cfg.api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        url = f"{cfg.api_url.rstrip('/')}/messages"
        data = _post_json(url, headers, payload, cfg.timeout)
        try:
            return self._extract_reply_anthropic(data)
        except LlmError:
            raise
        except (KeyError, TypeError, AttributeError) as exc:
            # 与 openai 路径同口径：不回显原始响应体，只描述问题
            hint = sorted(data.keys()) if isinstance(data, dict) else type(data).__name__
            raise LlmError(
                f"LLM 响应格式异常：content 字段不可解析（响应顶层字段: {hint}）——"
                "请确认 api_url 指向 Anthropic /messages 端点、模型名正确"
            ) from exc

    def _extract_reply_anthropic(self, data: dict[str, Any]) -> str:
        """从 Anthropic 响应提取正文，与 openai 路径同口径：绝不返回空串。

        - content 是块列表：拼接 text 块，统计 thinking 块字数（思考耗尽
          max_tokens 时报可操作错误，而非静默成功空串）；
        - content 是字符串（部分网关）：直接作为正文；
        - 空白正文：按 stop_reason 给「调大 Max Tokens」指引。
        """
        content = data["content"]
        if isinstance(content, str):
            text = content
            thinking_len = 0
            block_types: list[str] = []
        elif isinstance(content, list):
            parts: list[str] = []
            thinking_len = 0
            block_types = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                b_type = str(b.get("type") or "")
                block_types.append(b_type)
                if b_type == "text":
                    parts.append(str(b.get("text") or ""))
                elif b_type in ("thinking", "redacted_thinking"):
                    thinking_len += len(str(b.get("thinking") or b.get("data") or ""))
            text = "".join(parts)
        else:
            raise TypeError(f"content 应为块列表或字符串，收到 {type(content).__name__}")
        if text.strip():
            return text
        finish = str(data.get("stop_reason") or "")
        if thinking_len:
            raise LlmError(
                f"模型只返回了思考链（thinking {thinking_len} 字），未生成正文——"
                f"max_tokens={self._cfg.max_tokens} 大概率被思考耗尽"
                f"（stop_reason={finish or 'unknown'}）。"
                "请在「AI 设置」把 Max Tokens 调大（思考型模型建议 ≥16000）后重试"
            )
        if finish == "max_tokens":
            raise LlmError(
                "模型输出被 max_tokens 截断且无正文，请在「AI 设置」调大 Max Tokens 后重试"
            )
        raise LlmError(
            f"LLM 响应 content 为空（block 类型: {block_types or '无'}，"
            f"stop_reason={finish or 'unknown'}）——请检查模型名与 api_url 是否匹配"
        )

    async def test(self) -> dict[str, Any]:
        """连通性测试：发一句极短 ping，返回 ok/延迟/样例回复。"""
        t0 = time.perf_counter()
        try:
            reply = await self.chat(
                "请只回复两个字：OK", system_prompt="You are a connectivity probe."
            )
            return {
                "ok": True,
                "latency_ms": round((time.perf_counter() - t0) * 1000),
                "model": self._cfg.model,
                "provider": self._cfg.provider,
                "reply": reply.strip()[:100],
            }
        except LlmError as exc:
            return {
                "ok": False,
                "latency_ms": round((time.perf_counter() - t0) * 1000),
                "model": self._cfg.model,
                "provider": self._cfg.provider,
                "error": str(exc),
            }
