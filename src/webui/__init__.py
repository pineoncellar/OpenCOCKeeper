#- encoding: utf-8 -#
#
# @File     :   webui/__init__.py
# @Desc     :   WebUI 控制面：TraceBus 事件总线 + SSE 端点 + 数据底座剖析 API
# @Note     :   与 src/adapter/web/（数据面）分离，控制面仅提供调试/管理接口，
#              不参与游戏核心管线；Phase 1 为 Trace，Phase 2 为 db_inspector
#
#              注意：此处只导出轻量模块（trace_engine），不急切导入 server——
#              server 依赖 aiohttp 且会被 python -m 单独加载，急切导入会触发
#              "found in sys.modules" 警告并影响独立启动入口的 __main__ 判定
#

from src.webui.trace_engine import TraceBus, TraceEvent, get_trace_bus

__all__ = [
    "TraceBus",
    "TraceEvent",
    "get_trace_bus",
]