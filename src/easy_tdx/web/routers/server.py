"""服务器设置路由：列出/测速/切换标准 TDX 行情服务器。

让用户在 web UI 上看到候选 host 列表、一键测速、点选切换——解决"有些 IP
能连通有些不能"的问题（不同地区/运营商对通达信各服务器连通性不同）。
切换是热重连（``reconnect_to``），无需重启服务。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, StringConstraints

from easy_tdx.config import get_best_host, get_known_hosts, get_port, save_best_host
from easy_tdx.transport.sync import ping_all

router = APIRouter(tags=["server"])

# 单项 host 长度上限（域名合法最大 253 字符）；列表项数上限防被当作
# 无限制的内网扫描跳板。
HostStr = Annotated[str, StringConstraints(max_length=253)]


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class HostInfo(BaseModel):
    """单个 host 的状态信息。"""

    host: str
    latency_ms: int | None = None  # None = 未测速或不可达
    reachable: bool = False
    is_current: bool = False


class HostListResponse(BaseModel):
    """GET /server/hosts 的响应。"""

    hosts: list[HostInfo]
    current_host: str
    total: int


class ServerTestRequest(BaseModel):
    """POST /server/test 的请求体。

    ``timeout`` 限 0.5~30s（to_thread 内的同步 ping 无中断手段，无上界的
    超时会长期占住线程池线程）；``hosts`` 限 50 项、单项 ≤253 字符。
    """

    hosts: list[HostStr] | None = Field(
        default=None, max_length=50, description="待测主机列表；None = 测全部候选"
    )
    timeout: float = Field(default=5.0, ge=0.5, le=30.0, description="单主机连接超时（秒）")


class ServerSwitchRequest(BaseModel):
    """POST /server/switch 的请求。"""

    host: str


class SwitchResponse(BaseModel):
    """POST /server/switch 的响应。"""

    ok: bool
    host: str
    message: str


class MetaResponse(BaseModel):
    """GET /meta 的响应。"""

    version: str


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/meta", response_model=MetaResponse)
async def get_meta() -> MetaResponse:
    """应用元信息：版本号（WebUI 左上角展示）。

    源码运行未装包元数据（importlib.metadata 不可用）时留空，前端不显示。
    """
    try:
        from importlib.metadata import version

        v: str = version("easy-tdx")
    except Exception:  # noqa: BLE001 — 元数据缺失属正常场景，留空即可
        v = ""
    return MetaResponse(version=v)


@router.get("/server/hosts", response_model=HostListResponse)
async def list_hosts(request: Request) -> HostListResponse:
    """列出所有候选 host + 当前正在使用的 host。

    不做测速（避免 50+ host 全 ping 让首屏卡几秒）。前端点"测试全部"按钮
    后调 ``POST /server/test`` 获取延迟。
    """
    candidates = get_known_hosts()
    current = _get_current_host(request)

    host_infos = [HostInfo(host=h, is_current=(h == current)) for h in candidates]
    return HostListResponse(hosts=host_infos, current_host=current, total=len(host_infos))


@router.post("/server/test", response_model=list[HostInfo])
async def test_hosts(req: ServerTestRequest, request: Request) -> list[HostInfo]:
    """并发 ping 测试 host 列表，返回延迟和可达性。

    用 ``asyncio.to_thread`` 包装同步的 ``ping_all``（它内部用
    ThreadPoolExecutor 并发），避免阻塞事件循环。
    """
    hosts = req.hosts if req.hosts else get_known_hosts()
    port = get_port()
    current = _get_current_host(request)

    # ping_all 是同步阻塞函数，放到线程池跑
    ranked = await asyncio.to_thread(ping_all, hosts, port, req.timeout)

    # ranked 是 [(host, latency_sec)]，已按延迟升序排列，只含可达的
    reachable_map = {h: round(s * 1000) for h, s in ranked}

    # 按原始 hosts 顺序返回（保持列表稳定），但把可达的排前面
    result = []
    for h in hosts:
        latency_ms = reachable_map.get(h)
        result.append(
            HostInfo(
                host=h,
                latency_ms=latency_ms,
                reachable=latency_ms is not None,
                is_current=(h == current),
            )
        )

    # 可达的排前面（按延迟升序），不可达的排后面
    result.sort(key=lambda x: (x.reachable is False, x.latency_ms or 999999))
    return result


@router.post("/server/switch", response_model=SwitchResponse)
async def switch_host(req: ServerSwitchRequest, request: Request) -> SwitchResponse:
    """切换到指定 host（热重连，无需重启服务）。

    顺序：先 reconnect_to 成功 → 再 save_best_host 持久化。
    如果 reconnect 失败，不 save（避免污染 config，用户可再选别的）。
    """
    candidates = get_known_hosts()
    if req.host not in candidates:
        return SwitchResponse(
            ok=False,
            host=req.host,
            message=f"主机 {req.host} 不在候选列表里，无法切换",
        )

    client = request.app.state.tdx_client
    if client is None:
        return SwitchResponse(ok=False, host=req.host, message="TDX 客户端未初始化")

    try:
        await client.reconnect_to(req.host)
    except Exception as e:
        return SwitchResponse(
            ok=False,
            host=req.host,
            message=f"连接 {req.host} 失败：{e}。请选其他服务器。",
        )

    # 连接成功后才持久化
    save_best_host(req.host)
    return SwitchResponse(ok=True, host=req.host, message=f"已切换到 {req.host}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _get_current_host(request: Request) -> str:
    """获取当前 TDX 客户端实际连接的 host。"""
    client = getattr(request.app.state, "tdx_client", None)
    if client is not None:
        # AsyncTdxClient._host 是实际连接的 host（reconnect_to 会更新它）
        return getattr(client, "_host", get_best_host())
    return get_best_host()
