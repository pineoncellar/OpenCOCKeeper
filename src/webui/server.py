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
from typing import Optional

from aiohttp import web

from src.core.config import PROJECT_ROOT, get_settings
from src.core.log import get_logger

from .routes import health, list_worlds, get_world_detail, trace_stream

logger = get_logger(__name__)

# 全局 runner 引用，供后台 shutdown 时优雅停止
_runner: Optional[web.AppRunner] = None


# ====================================================================
# 应用工厂
# ====================================================================


def create_app() -> web.Application:
    """构造 aiohttp.web 应用，注册路由与静态文件服务。"""
    app = web.Application()

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

    # 状态：API 路由
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/trace/stream", trace_stream)

    # 状态：Phase 2 预留端点（占位）
    app.router.add_get("/api/worlds", list_worlds)
    app.router.add_get("/api/worlds/{id}", get_world_detail)

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


async def start_background() -> bool:
    """在后台启动 WebUI 服务，与当前事件循环共存。

    先读 config.webui.enabled 判断是否启用，再读 host/port 启动；
    返回 True 表示已启动，False 表示未启用。
    """
    settings = get_settings()
    if not settings.get("webui.enabled", True):
        return False

    host = str(settings.get("webui.host", "127.0.0.1"))
    port = int(settings.get("webui.port", 8080))

    app = create_app()
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
    """WebUI 独立入口：解析配置，启动 aiohttp 服务。"""
    settings = get_settings()
    host = str(settings.get("webui.host", "127.0.0.1"))
    port = int(settings.get("webui.port", 8080))

    app = create_app()
    logger.info("WebUI 启动 %s:%d", host, port)
    print(f"\n  WebUI 启动: http://{host}:{port}")
    print(f"  Trace 面板: http://{host}:{port}/")
    print(f"  SSE 端点:   http://{host}:{port}/api/trace/stream")
    print()

    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()