# -*- coding: utf-8 -*-
"""
@File     :   agent/__init__.py
@Desc     :   主 Agent 工具层：函数 schema 注册 + Function Calling 调度闭环
@Note     :   只提供"工具清单 + 工具执行 + 闭环循环"三件套，主 Agent 的
             上下文装配（Context Assembler）与叙事决策大纲输出由上层编排；
             world_id / turn_num 经 run_tool_loop 注入，模型不可见
"""

from src.agent.loop import (
    ToolLoopResult,
    ToolRunner,
    build_default_runner,
    run_tool_loop,
)
from src.agent.schemas import build_tool_schemas, tool_names

__all__ = [
    "build_tool_schemas",
    "tool_names",
    "ToolRunner",
    "build_default_runner",
    "run_tool_loop",
    "ToolLoopResult",
]
