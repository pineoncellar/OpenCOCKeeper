#- encoding: utf-8 -#
#
# @File     :   server.py
# @Desc     :   WebUI 统一启动入口 — aiohttp.web 应用
# @Note     :   支持两种启动方式：独立入口（uv run python -m src.webui.server）与
#              后台嵌入（start_background 被 runtime 调用，与 CLI 同事件循环共存）。
#              config 配置段为 webui.enabled / webui.host / webui.port / webui.static_dir
#

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from aiohttp import WSMsgType, web

from src.core.config import PROJECT_ROOT, get_settings
from src.core.log import get_logger

from .config_manager import (
    api_get_config,
    api_get_config_raw,
    api_save_config,
    api_validate_config,
)
from .db_inspector import (
    api_delete_world,
    api_get_turn,
    api_get_world,
    api_list_entities,
    api_list_turns,
    api_list_worlds,
    api_rollback,
    api_search_memories,
)
from .routes import health, trace_stream

logger = get_logger(__name__)

# 全局 runner 引用，供后台 shutdown 时优雅停止
_runner: Optional[web.AppRunner] = None


# ====================================================================
# WebSocket 游戏数据面
# ====================================================================


async def ws_game(request: web.Request) -> web.WebSocketResponse:
    """WebSocket 游戏端点：浏览器 <-> WebAdapter <-> 核心管线。

    每个连接一个 WebAdapter 实例（窗口 id 仅存在于连接内），逐消息调
    handle_inbound 直驱；落库后经 _on_turn_committed 广播 state_diff 刷新
    角色面板。连接断开即清理适配器。
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    from src.adapter.web.adapter import WebAdapter

    storage = request.app.get("storage")
    if storage is None:
        await ws.send_json({"type": "system_message", "text": "存储未初始化", "level": "error"})
        await ws.close()
        return ws

    session_id = str(request.query.get("session_id", "web-default"))
    adapter = WebAdapter(
        storage=storage,
        memory=request.app.get("memory"),
        worker=request.app.get("worker"),
        llm=request.app.get("llm"),
        session_id=session_id,
        ws=ws,
    )

    # 状态：初始推送会话状态（当前世界 + 角色快照，供前端渲染面板）
    await ws.send_json(
        {"type": "session_info", "data": adapter.world_status(), "session_id": session_id}
    )
    logger.info("Web 玩家接入 session=%s", session_id)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    await adapter.handle_inbound(msg.data)
                except Exception as e:  # noqa: BLE001  单条消息失败不中断连接
                    logger.error(f"Web 消息处理异常: {e}", exc_info=True)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        adapter.close_connection()
        logger.info("Web 玩家断开 session=%s", session_id)
    return ws


# ====================================================================
# 应用工厂
# ====================================================================


def create_app(
    storage: Any = None,
    memory: Any = None,
    worker: Any = None,
) -> web.Application:
    """构造 aiohttp.web 应用，注册路由与静态文件服务。

    storage / memory / worker 注入到 app 状态（app["storage"] 等），
    供 db_inspector 处理器取用；缺省 None 时数据底座 API 返回明确错误。
    """
    app = web.Application()
    # 状态：依赖注入到 app 状态，处理器经 request.app 访问
    app["storage"] = storage
    app["memory"] = memory
    app["worker"] = worker

    # 状态：根路由 — 直接返回 index.html，避免 aiohttp 目录列表
    static_dir = _resolve_static_dir()
    index_path = static_dir / "index.html"
    if index_path.exists():
        async def _index(request: web.Request) -> web.FileResponse:
            return web.FileResponse(index_path)
        app.router.add_get("/", _index)
    # 状态：静态文件服务 — css/js 等资源文件
    if static_dir.exists():
        app.router.add_static("/", static_dir, show_index=False)
        logger.info("WebUI 静态文件目录: %s", static_dir)
    else:
        logger.warning("WebUI 静态文件目录不存在: %s，仅提供 API", static_dir)

    # 状态：健康检查与 SSE Trace
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/trace/stream", trace_stream)

    # 状态：Phase 2 数据底座 API
    app.router.add_get("/api/worlds", api_list_worlds)
    app.router.add_get("/api/worlds/{id}", api_get_world)
    app.router.add_get("/api/worlds/{id}/entities", api_list_entities)
    app.router.add_get("/api/worlds/{id}/turns", api_list_turns)
    app.router.add_get("/api/worlds/{id}/turns/{num}", api_get_turn)
    app.router.add_get("/api/worlds/{id}/memories", api_search_memories)
    app.router.add_post("/api/worlds/{id}/rollback", api_rollback)
    app.router.add_delete("/api/worlds/{id}", api_delete_world)

    # 状态：Phase 3 配置管理 API
    app.router.add_get("/api/config", api_get_config)
    app.router.add_get("/api/config/raw", api_get_config_raw)
    app.router.add_post("/api/config/validate", api_validate_config)
    app.router.add_post("/api/config/save", api_save_config)

    # 状态：Phase 4 游戏数据面 — WebSocket 跑团终端
    app.router.add_get("/ws/game", ws_game)

    return app


def _resolve_static_dir() -> Path:
    """从 config 取静态目录（webui.static_dir），缺省为项目根下的 webui/。"""
    settings = get_settings()
    custom = settings.get("webui.static_dir")
    if custom:
        return Path(custom)
    return PROJECT_ROOT / "webui"


# ====================================================================
# 后台启动（供 runtime.py 调用，与适配器同事件循环共存）
# ====================================================================


async def start_background(
    storage: Any = None,
    memory: Any = None,
    worker: Any = None,
) -> bool:
    """在后台启动 WebUI 服务，与当前事件循环共存。

    先读 config.webui.enabled 判断是否启用，再读 host/port 启动；
    storage / memory / worker 注入数据底座 API（Phase 2 世界剖析/回档）；
    返回 True 表示已启动，False 表示未启用。
    """
    settings = get_settings()
    if not settings.get("webui.enabled", True):
        return False

    host = str(settings.get("webui.host", "127.0.0.1"))
    port = int(settings.get("webui.port", 8080))

    app = create_app(storage=storage, memory=memory, worker=worker)
    global _runner
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, host, port)
    await site.start()

    logger.info("WebUI 后台服务已启动 http://%s:%d", host, port)
    print(f"\n  WebUI 控制台: http://{host}:{port}")
    print(f"  SSE 调试端点: http://{host}:{port}/api/trace/stream\n")
    return True


async def stop_background() -> None:
    """优雅停止后台 WebUI 服务。"""
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
        logger.info("WebUI 后台服务已停止")


# ====================================================================
# 独立启动入口
# ====================================================================


def main() -> None:
    """WebUI 独立入口：解析配置，构建真实存储/记忆门面，启动 aiohttp 服务。"""
    settings = get_settings()
    host = str(settings.get("webui.host", "127.0.0.1"))
    port = int(settings.get("webui.port", 8080))

    # 状态：独立模式构建真实运行时（数据底座剖析/回档需要）
    from src.memory.interface import Memory
    from src.storage.storage import Storage

    app = create_app(storage=Storage(), memory=Memory())
    logger.info("WebUI 启动 %s:%d", host, port)
    print(f"\n  WebUI 启动: http://{host}:{port}")
    print(f"  Trace 面板: http://{host}:{port}/")
    print(f"  SSE 端点:   http://{host}:{port}/api/trace/stream")
    print()

    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()