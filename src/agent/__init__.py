# -*- coding: utf-8 -*-
"""
@File     :   agent/__init__.py
@Desc     :   主 Agent 层：工具 schema + Function Calling 闭环 + Context Assembler + Director 编排
@Note     :   四件套——工具清单（4 原子 + present_directive 收尾）、ToolRunner 执行、
             run_tool_loop 闭环、ContextBundle 装配、Director.run_turn 回合编排；
             world_id / turn_num 经 run_tool_loop 注入，模型不可见
"""

from src.agent.loop import (
    ToolLoopResult,
    ToolRunner,
    build_default_runner,
    run_tool_loop,
)
from src.agent.schemas import build_tool_schemas, build_main_agent_schemas, tool_names
from src.agent.assembler import ContextBundle, DEFAULT_SYSTEM, assemble
from src.agent.directive import (
    NarrativeDirective,
    PRESENT_DIRECTIVE_NAME,
    PRESENT_DIRECTIVE_SCHEMA,
    extract_narrative_directive,
)
from src.agent.director import Director

__all__ = [
    "build_tool_schemas",
    "build_main_agent_schemas",
    "tool_names",
    "ToolRunner",
    "build_default_runner",
    "run_tool_loop",
    "ToolLoopResult",
    "ContextBundle",
    "DEFAULT_SYSTEM",
    "assemble",
    "NarrativeDirective",
    "PRESENT_DIRECTIVE_NAME",
    "PRESENT_DIRECTIVE_SCHEMA",
    "extract_narrative_directive",
    "Director",
]
