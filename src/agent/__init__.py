# -*- coding: utf-8 -*-
"""
@File     :   agent/__init__.py
@Desc     :   Agent 层：工具 schema + Function Calling 闭环 + Context Assembler + Director 编排
             + Narrator 润色 + 串行管线
@Note     :   四件套——工具清单（5 原子 + present_directive 收尾）、ToolRunner 执行、
             run_tool_loop 闭环、ContextBundle 装配、Director.run_turn 回合编排；
             下游——Narrator 无状态演播器、run_narrated_turn 串行管线（裁决→演播→落库）；
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
    DEFAULT_ENDING_TYPE,
    ENDING_TYPE_BD,
    ENDING_TYPE_HD,
    ENDING_TYPE_TD,
    ENDING_TYPES,
    NarrativeDirective,
    PRESENT_DIRECTIVE_NAME,
    PRESENT_DIRECTIVE_SCHEMA,
    extract_ending,
    extract_narrative_directive,
)
from src.agent.director import Director
from src.agent.narrator import (
    Narrator,
    NARRATOR_ENDING_SYSTEM,
    NARRATOR_SYSTEM,
    build_narrator_messages,
    ending_label,
)
from src.agent.pipeline import (
    EndedTurn,
    NarratedTurn,
    prepare_manual_ending,
    run_ending_wrapup,
    run_narrated_turn,
)
from src.agent.opening import (
    NarratedOpening,
    OpeningSetupResult,
    PRESENT_OPENING_NAME,
    PRESENT_OPENING_SCHEMA,
    build_opening_runner,
    run_opening_narration,
    run_opening_setup,
)

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
    "extract_ending",
    "ENDING_TYPES",
    "ENDING_TYPE_HD",
    "ENDING_TYPE_TD",
    "ENDING_TYPE_BD",
    "DEFAULT_ENDING_TYPE",
    "Director",
    "Narrator",
    "NARRATOR_SYSTEM",
    "NARRATOR_ENDING_SYSTEM",
    "ending_label",
    "build_narrator_messages",
    "NarratedTurn",
    "EndedTurn",
    "run_narrated_turn",
    "run_ending_wrapup",
    "prepare_manual_ending",
    "OpeningSetupResult",
    "NarratedOpening",
    "PRESENT_OPENING_NAME",
    "PRESENT_OPENING_SCHEMA",
    "build_opening_runner",
    "run_opening_setup",
    "run_opening_narration",
]
