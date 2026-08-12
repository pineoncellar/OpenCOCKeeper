#- encoding: utf-8 -#
#
# @File     :   routes.py
# @Desc     :   WebUI REST API + SSE 路由处理器
# @Note     :   Phase 1 仅含 SSE Trace 端点；Phase 2 追加数据底座 API
#              所有路由注册在 server.py 中完成
#

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Optional

from aiohttp import web

from src.webui.trace_engine import get_trace_bus


# ====================================================================
# SSE — Trace 实时流
# ====================================================================


async def trace_stream(request: web.Request) -> web.StreamResponse:
    """SSE 端点：实时推送 TraceEvent，支持 ?world_id= 过滤。

    客户端断开时自动退出循环，不泄漏后台任务；初始先推送快照历史，
    再持续监听新事件。
    """
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await resp.prepare(request)

    bus = get_trace_bus()
    world_filter: Optional[str] = request.query.get("world_id")

    # 状态：先推送快照历史（初始加载已有事件）
    for event in bus.get_snapshot(limit=200):
        if world_filter and event.world_id != world_filter:
            continue
        payload = json.dumps(asdict(event), ensure_ascii=False)
        await resp.write(f"data: {payload}\n\n".encode("utf-8"))

    # 状态：持续监听新事件，客户端断开时 StopAsyncIteration 安全退出
    async for event in bus.subscribe():
        if world_filter and event.world_id != world_filter:
            continue
        payload = json.dumps(asdict(event), ensure_ascii=False)
        try:
            await resp.write(f"data: {payload}\n\n".encode("utf-8"))
        except ConnectionResetError:
            break
        except Exception:
            break

    return resp


# ====================================================================
# Health 检查
# ====================================================================


async def health(request: web.Request) -> web.Response:
    """健康检查端点，供前端/反向代理探测服务状态。"""
    return web.json_response({"status": "ok", "service": "opencockeeper-webui"})


# ====================================================================
# Phase 2 预留 — 数据底座 API（占位）
# ====================================================================


async def list_worlds(request: web.Request) -> web.Response:
    """Phase 2: 列出所有世界 — 当前占位返回空列表。"""
    return web.json_response({"worlds": [], "note": "Phase 2 实现"})


async def get_world_detail(request: web.Request) -> web.Response:
    """Phase 2: 世界详情 — 当前占位。"""
    return web.json_response({"note": "Phase 2 实现", "world_id": request.match_info.get("id", "")})