#- encoding: utf-8 -#
#
# @File     :   routes.py
# @Desc     :   WebUI REST API + SSE 路由处理器
# @Note     :   SSE Trace 端点在此；数据底座 API 处理器见 db_inspector.py
#              （server.py 统一注册全部路由）
#

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Optional

from aiohttp import web

from src.webui.trace_engine import get_trace_bus
from src.webui.trace_store import get_trace_store


# ====================================================================
# SSE — Trace 实时流
# ====================================================================


async def trace_stream(request: web.Request) -> web.StreamResponse:
    """SSE 端点：实时推送 TraceEvent，支持 ?world_id= 过滤。

    只推连接之后的增量事件——历史由 REST /api/trace/worlds/{id}/turns 提供，
    避免重启后全量历史经 SSE 大推送；客户端断开时自动退出循环，不泄漏任务。
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

    # 状态：持续监听新事件，客户端断开时安全退出
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
# Trace 历史 — REST（重启后从持久化文件读取，供前端世界/轮次树）
# ====================================================================


def _parse_int(raw: Optional[str], default: int, lo: int, hi: int) -> int:
    """解析整型查询参数，非法/越界回退默认值并夹紧到 [lo, hi]。"""
    try:
        v = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _merge_scene_notes(storage, worlds: list) -> list:
    """为世界摘要附上当前场景手记（scene_notes，来自 SQLite world_state）。

    storage 未注入（独立 WebUI 模式）时保持原摘要；世界无手记或单世界读取失败
    附空串，绝不让手记缺失阻断世界列表加载。
    """
    if storage is None:
        return worlds
    for w in worlds:
        try:
            world = storage.get_world(w["world_id"])
        except Exception:  # noqa: BLE001  单世界读取失败不影响列表
            world = None
        w["scene_notes"] = ((world or {}).get("scene_notes") or "").strip()
    return worlds


async def api_trace_worlds(request: web.Request) -> web.Response:
    """GET /api/trace/worlds — 有 trace 记录的世界列表（含轮数/最新轮/当前场景手记）。"""
    worlds = get_trace_store().world_summaries()
    _merge_scene_notes(request.app.get("storage"), worlds)
    return web.json_response({"worlds": worlds})


async def api_trace_turns(request: web.Request) -> web.Response:
    """GET /api/trace/worlds/{id}/turns?limit=&offset= — 轮次分页列表（最新在前）。"""
    world_id = request.match_info["id"]
    limit = _parse_int(request.query.get("limit"), 20, 1, 500)
    offset = _parse_int(request.query.get("offset"), 0, 0, 10**6)
    store = get_trace_store()
    return web.json_response(
        {
            "world_id": world_id,
            "total": store.count_turns(world_id),
            "limit": limit,
            "offset": offset,
            "turns": [asdict(t) for t in store.list_turns(world_id, limit=limit, offset=offset)],
        }
    )


async def api_trace_turn(request: web.Request) -> web.Response:
    """GET /api/trace/worlds/{id}/turns/{num} — 单轮全部事件（按落盘顺序）。"""
    world_id = request.match_info["id"]
    try:
        num = int(request.match_info["num"])
    except ValueError:
        raise web.HTTPBadRequest(text="非法轮次号") from None
    events = get_trace_store().load_turn(world_id, num)
    return web.json_response(
        {
            "world_id": world_id,
            "turn_num": num,
            "events": [asdict(e) for e in events],
        }
    )