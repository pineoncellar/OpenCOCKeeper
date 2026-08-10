# -*- coding: utf-8 -*-
"""
@File     :   protocol.py
@Desc     :   统一消息协议 — 所有 adapter（CLI / OneBot / Web）共用的入站/出站消息格式
@Note     :   消息流 External Input -> InboundMessage -> Adapter.handle() -> OutboundMessage -> External Output；
             session_id（窗口 id）仅在本层存在，经路由映射为 world_id 后下传，下游只认 world_id
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


# ============================================
# 消息类型
# ============================================


class MessageType:
    """统一消息类型常量：入站 PLAYER_INPUT / SYSTEM_CMD，出站 NARRATIVE / SYSTEM_MSG / SESSION_INFO。"""

    PLAYER_INPUT = "player_input"
    SYSTEM_CMD = "system_cmd"
    NARRATIVE = "narrative"
    SYSTEM_MSG = "system_message"
    SESSION_INFO = "session_info"


# ============================================
# 入站消息
# ============================================


@dataclass
class InboundMessage:
    """统一入站消息 — 来自外部（玩家/客户端）的消息。

    字段: type 消息类型 / text 消息正文 / session_id 会话 id（窗口 id，仅本层存在）/
          data 附加结构化数据 / platform 来源平台 / channel_id 群号或频道 /
          user_id 用户标识 / world_id 世界标识（由适配器映射后填充）
    """

    type: str
    text: str = ""
    session_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    platform: str = "cli"
    channel_id: str = ""
    user_id: str = ""
    world_id: str = ""

    # ---- 便捷工厂 ----

    @classmethod
    def player_input(cls, text: str, session_id: str = "", **routing) -> "InboundMessage":
        """创建玩家输入消息。"""
        return cls(type=MessageType.PLAYER_INPUT, text=text, session_id=session_id, **routing)

    @classmethod
    def system_cmd(cls, command: str, session_id: str = "", **routing) -> "InboundMessage":
        """创建系统命令消息。"""
        return cls(type=MessageType.SYSTEM_CMD, text=command, session_id=session_id, **routing)

    def to_dict(self) -> dict:
        """序列化为字典，供日志/转发使用。"""
        return asdict(self)


# ============================================
# 出站消息
# ============================================


@dataclass
class OutboundMessage:
    """统一出站消息 — 发往外部（玩家/客户端）的消息。

    字段: type 消息类型 / text 消息正文 / session_id 目标会话 / data 附加数据 /
          timestamp 时间戳 / platform 目标平台 / channel_id 群号或频道 /
          user_id 目标用户 / world_id 世界标识
    """

    type: str
    text: str = ""
    session_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    platform: str = "cli"
    channel_id: str = ""
    user_id: str = ""
    world_id: str = ""

    # ---- 便捷工厂 ----

    @classmethod
    def narrative(cls, text: str, session_id: str = "", **routing) -> "OutboundMessage":
        """创建叙事消息（回合管线输出）。"""
        return cls(type=MessageType.NARRATIVE, text=text, session_id=session_id, **routing)

    @classmethod
    def system_msg(cls, text: str, level: str = "info", session_id: str = "", **routing) -> "OutboundMessage":
        """创建系统消息（通知/错误/状态），level 取值 info/warn/error。"""
        return cls(
            type=MessageType.SYSTEM_MSG,
            text=text,
            session_id=session_id,
            data={"level": level},
            **routing,
        )

    @classmethod
    def session_info(cls, data: dict, session_id: str = "", **routing) -> "OutboundMessage":
        """创建会话状态消息，data 为结构化状态信息。"""
        return cls(
            type=MessageType.SESSION_INFO,
            text=data.get("text", ""),
            session_id=session_id,
            data=data,
            **routing,
        )

    def to_dict(self) -> dict:
        """序列化为字典，供日志/转发使用。"""
        return asdict(self)
