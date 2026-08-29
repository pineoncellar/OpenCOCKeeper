#- encoding: utf-8 -#
#
# @File     :   db_inspector.py
# @Desc     :   数据底座剖析 API 处理器 — 封装 Storage / Memory 门面为 REST 只读接口
# @Note     :   storage / memory / worker 经 app["..."] 注入（见 server.create_app），
#              处理器从 request.app 取用；回档与删除是唯二写操作，复用 worker.rollback_world
#              / delete_world 编排（与后台固化共享世界锁）。所有响应统一 web.json_response
#

from __future__ import annotations

from typing import Any, Optional

from aiohttp import web

from src.core.exceptions import (
    EntityNotFoundError,
    MemoryOperationError,
    OpenCOCKeeperError,
    TurnNotFoundError,
    UndoError,
    WorldNotFoundError,
)
from src.core.log import get_logger

logger = get_logger(__name__)


# ====================================================================
# 依赖取用
# ====================================================================


def _storage(request: web.Request):
    """从 app 状态取 Storage 门面。"""
    return request.app["storage"]


def _memory(request: web.Request):
    """从 app 状态取 Memory 门面（可为 None 时按未配置处理）。"""
    return request.app.get("memory")


def _worker(request: web.Request):
    """从 app 状态取后台固化 Worker（可为 None 时免锁直连）。"""
    return request.app.get("worker")


# ====================================================================
# 世界
# ====================================================================


async def api_list_worlds(request: web.Request) -> web.Response:
    """列出所有世界：world_id / status / module_name / global_recap 摘要 / 实体数。"""
    storage = _storage(request)
    status = request.query.get("status")
    worlds = storage.list_worlds(status=status) if status else storage.list_worlds()
    result = []
    for w in worlds:
        w_id = w["world_id"]
        entities = storage.get_entities(w_id)
        result.append(
            {
                "world_id": w_id,
                "status": w.get("status", "ACTIVE"),
                "module_name": w.get("module_name"),
                "global_recap": (w.get("global_recap") or "")[:120],
                "entity_count": len(entities),
                "player_ids": w.get("player_ids") or [],
            }
        )
    return web.json_response({"worlds": result})


async def api_get_world(request: web.Request) -> web.Response:
    """单个世界详情：全量字段 + 最近一轮摘要。"""
    storage = _storage(request)
    w_id = request.match_info["id"]
    world = storage.get_world(w_id)
    if world is None:
        return _err(404, "WorldNotFound", f"世界不存在: {w_id}")
    recent = storage.get_recent_turns(w_id, limit=1)
    world["recent_summary"] = _turn_summary(recent[-1]) if recent else None
    return web.json_response({"world": world})


# ====================================================================
# 实体
# ====================================================================


async def api_list_entities(request: web.Request) -> web.Response:
    """列出某世界全部实体：物理状态（HP/SAN/MP）、属性技能、背包、动态 Tag。"""
    storage = _storage(request)
    w_id = request.match_info["id"]
    if storage.get_world(w_id) is None:
        return _err(404, "WorldNotFound", f"世界不存在: {w_id}")
    etype = request.query.get("type")
    entities = storage.get_entities(w_id, entity_type=etype)
    return web.json_response({"world_id": w_id, "entities": entities})


# ====================================================================
# 轮次
# ====================================================================


async def api_list_turns(request: web.Request) -> web.Response:
    """列出某世界轮次历史（正序，limit 可配，缺省窗口大小）。"""
    storage = _storage(request)
    w_id = request.match_info["id"]
    if storage.get_world(w_id) is None:
        return _err(404, "WorldNotFound", f"世界不存在: {w_id}")
    try:
        limit = int(request.query.get("limit", ""))
    except ValueError:
        limit = None
    turns = storage.get_recent_turns(w_id, limit=limit)
    # 状态：轮次列表只给摘要，避免把整轮 context_data 全量下发
    return web.json_response(
        {"world_id": w_id, "turns": [_turn_summary(t) for t in turns]}
    )


async def api_get_turn(request: web.Request) -> web.Response:
    """单个轮次详情：完整 context_data + state_diff（供剖析/审计）。"""
    storage = _storage(request)
    w_id = request.match_info["id"]
    try:
        num = int(request.match_info["num"])
    except ValueError:
        return _err(400, "BadRequest", "turn_num 必须为整数")
    turn = storage.get_turn(w_id, num)
    if turn is None:
        return _err(404, "TurnNotFound", f"轮次不存在: {w_id}/turn {num}")
    return web.json_response({"world_id": w_id, "turn": turn})


# ====================================================================
# 记忆
# ====================================================================


async def api_search_memories(request: web.Request) -> web.Response:
    """按 query 语义召回某世界记忆；?query= 缺省列出最近记忆（空 query 走 top_k 兜底）。"""
    memory = _memory(request)
    if memory is None:
        return _err(503, "NoMemoryBackend", "未配置记忆后端")
    storage = _storage(request)
    w_id = request.match_info["id"]
    if storage.get_world(w_id) is None:
        return _err(404, "WorldNotFound", f"世界不存在: {w_id}")
    query = request.query.get("query", "").strip()
    top_k = _int_or(request.query.get("top_k"), 5)
    try:
        if query:
            hits = await memory.search(query, w_id, top_k=top_k)
        else:
            # 状态：无 query 时用通用引导 query 取最近记忆，等价"列出该世界记忆"
            hits = await memory.search("最近发生了什么事件、当前处境与掌握的线索", w_id, top_k=top_k)
    except MemoryOperationError as e:
        return _err(500, "MemoryError", str(e))
    return web.json_response(
        {
            "world_id": w_id,
            "query": query,
            "hits": [hit.__dict__ for hit in hits],
        }
    )


# ====================================================================
# 写操作：回档 / 删除
# ====================================================================


async def api_rollback(request: web.Request) -> web.Response:
    """回档到第 N 轮之前：先撤销 SQLite 物理状态，再擦除 RAG 语义记忆。

    请求体 JSON：{"turn_num": N}；返回撤销的轮次数与 RAG 删除条数。
    """
    storage = _storage(request)
    memory = _memory(request)
    w_id = request.match_info["id"]
    if storage.get_world(w_id) is None:
        return _err(404, "WorldNotFound", f"世界不存在: {w_id}")
    body = await _read_json(request)
    turn_num = body.get("turn_num") if isinstance(body, dict) else None
    if not isinstance(turn_num, int) or turn_num < 0:
        return _err(400, "BadRequest", "请求体需含 int 字段 turn_num >= 0")
    try:
        if memory is not None:
            from src.memory.worker import rollback_world

            deleted = await rollback_world(
                storage, memory, w_id, turn_num, worker=_worker(request)
            )
        else:
            # 状态：无记忆后端时仅物理回档（RAG 段跳过）
            undone = storage.undo_from(w_id, turn_num)
            deleted = len(undone)
    except (WorldNotFoundError, TurnNotFoundError, UndoError) as e:
        return _err(409, "RollbackError", str(e))
    # 状态：回档后刷新世界状态（撤销终局轮时自动恢复 ACTIVE）
    world = storage.get_world(w_id)
    return web.json_response(
        {
            "status": "ok",
            "world_id": w_id,
            "turn_num": turn_num,
            "world_status": world.get("status") if world else None,
            "rag_deleted": deleted,
        }
    )


async def api_delete_world(request: web.Request) -> web.Response:
    """删除世界：先清 RAG 语义记忆，再删 SQLite 行（级联实体/轮次/历史）。"""
    storage = _storage(request)
    memory = _memory(request)
    w_id = request.match_info["id"]
    if storage.get_world(w_id) is None:
        return _err(404, "WorldNotFound", f"世界不存在: {w_id}")
    try:
        if memory is not None:
            from src.memory.worker import delete_world

            deleted = await delete_world(storage, memory, w_id, worker=_worker(request))
        else:
            storage.delete_world(w_id)
            deleted = 0
    except OpenCOCKeeperError as e:
        return _err(409, "DeleteError", str(e))
    return web.json_response(
        {"status": "ok", "world_id": w_id, "rag_deleted": deleted}
    )


# ====================================================================
# 内部工具
# ====================================================================


def _turn_summary(turn: dict) -> dict:
    """轮次摘要：轮次号 / 用户输入 / 叙事 / 检定数 / 状态变更 / 固化标记。"""
    ctx = turn.get("context_data") or {}
    checks = ctx.get("checks") or []
    return {
        "turn_num": turn.get("turn_num"),
        "user": (ctx.get("user") or "")[:200],
        "assistant": (ctx.get("assistant") or "")[:300],
        "directive": (ctx.get("directive") or "")[:200],
        "check_count": len(checks) if isinstance(checks, list) else 0,
        "state_diff": turn.get("state_diff") or {},
        "solidified": bool(turn.get("solidified")),
        "is_ending": bool(ctx.get("is_ending")),
        "ending_type": ctx.get("ending_type"),
    }


def _err(status: int, code: str, message: str) -> web.Response:
    """统一错误响应：{error: {code, message}}。"""
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def _int_or(value: Optional[str], default: int) -> int:
    """字符串安全转 int，失败回退默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _read_json(request: web.Request) -> Any:
    """读取请求体 JSON，失败返回 None（调用方按空 body 处理）。"""
    try:
        return await request.json()
    except Exception:  # noqa: BLE001  非 JSON body 按空处理
        return None
