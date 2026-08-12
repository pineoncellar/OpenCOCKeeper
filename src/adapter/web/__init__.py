#- encoding: utf-8 -#
#
# @File     :   web/__init__.py
# @Desc     :   Web 适配器（游戏数据面）：WebSocket 桥接浏览器与核心管线
# @Note     :   与 src/webui/（控制面）分离——本层继承 AbstractAdapter 参与游戏回合，
#              WebUI 控制面只做调试/管理；/ws/game 端点在 server.py 中挂载
#

from .adapter import WebAdapter, _entity_brief
from .protocol import (
    OUT_NARRATIVE,
    OUT_SESSION_INFO,
    OUT_STATE_DIFF,
    OUT_SYSTEM_MSG,
    build_narrative_frame,
    build_session_frame,
    build_state_diff_frame,
    build_system_frame,
)

__all__ = [
    "WebAdapter",
    "_entity_brief",
    "OUT_NARRATIVE",
    "OUT_SYSTEM_MSG",
    "OUT_SESSION_INFO",
    "OUT_STATE_DIFF",
    "build_narrative_frame",
    "build_system_frame",
    "build_session_frame",
    "build_state_diff_frame",
]
