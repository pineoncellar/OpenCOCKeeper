# -*- coding: utf-8 -*-
"""
@File     :   adapter/__init__.py
@Desc     :   适配器层：统一消息协议 + 抽象基类 + Web 适配器 + 运行时编排
@Note     :   窗口/会话 id 仅存在于本层，经 parse 映射为 world_id 后下传；
             存储层 / Agent / Tools 只认 world_id（架构红线）；终端不接收输入仅做日志
"""

from src.adapter.protocol import InboundMessage, MessageType, OutboundMessage
from src.adapter.base import AbstractAdapter
from src.adapter.runtime import create_adapter, run_app, main

__all__ = [
    "InboundMessage",
    "MessageType",
    "OutboundMessage",
    "AbstractAdapter",
    "create_adapter",
    "run_app",
    "main",
]
