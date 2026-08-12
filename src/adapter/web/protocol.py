#- encoding: utf-8 -#
#
# @File     :   protocol.py
# @Desc     :   Web 适配器消息帧定义 — 浏览器 <-> WebSocket 的 JSON 线协议
# @Note     :   入站帧 {type, text, session_id}，出站帧 {type, text, data, world_id, ...}
#              复用 src.adapter.protocol 的 MessageType 常量；WebAdapter 负责
#              帧 <-> Inbound/OutboundMessage 转换（见 adapter.py）
#

from __future__ import annotations

from typing import Any, Dict

# 出站帧类型：叙事 / 系统消息 / 会话状态 / 状态增量推送
OUT_NARRATIVE = "narrative"
OUT_SYSTEM_MSG = "system_message"
OUT_SESSION_INFO = "session_info"
OUT_STATE_DIFF = "state_diff"


def build_narrative_frame(
    text: str, *, world_id: str = "", session_id: str = "",
) -> Dict[str, Any]:
    """构造叙事出站帧：回合管线的玩家视角文本。"""
    return {"type": OUT_NARRATIVE, "text": text, "world_id": world_id, "session_id": session_id}


def build_system_frame(
    text: str, *, level: str = "info", world_id: str = "", session_id: str = "",
) -> Dict[str, Any]:
    """构造系统消息出站帧：level 取值 info/warn/error。"""
    return {
        "type": OUT_SYSTEM_MSG, "text": text, "level": level,
        "world_id": world_id, "session_id": session_id,
    }


def build_session_frame(data: Dict[str, Any], *, session_id: str = "") -> Dict[str, Any]:
    """构造会话状态出站帧：data 为结构化状态信息（世界/角色/位置等）。"""
    return {"type": OUT_SESSION_INFO, "data": data, "session_id": session_id}


def build_state_diff_frame(
    *, world_id: str, turn_num: int, state_diff: Dict[str, Any],
    narration: str = "", checks: list = None, entities: list = None,
) -> Dict[str, Any]:
    """构造状态增量出站帧：每轮落库后推送，供前端实时刷新角色面板。

    state_diff 为 SQLite 增量；narration/checks 为权威副本；entities 为
    变更后的实体快照（前端据此渲染血条/背包，无需再发 GET）。
    """
    return {
        "type": OUT_STATE_DIFF,
        "world_id": world_id,
        "turn_num": turn_num,
        "state_diff": state_diff,
        "narration": narration,
        "checks": checks or [],
        "entities": entities or [],
    }
