# -*- coding: utf-8 -*-
"""
@File     :   schemas.py
@Desc     :   主 Agent 5 个原子工具的 OpenAI Function Calling schema 集中注册
@Note     :   注入参数剥离——world_id / turn_num 由调度器运行时注入，不暴露给模型填写，
             杜绝跨世界/错轮次；check_and_update_stats 复用 src.tools.schemas 的
             parameters 但剔除注入字段；build_tool_schemas 产出可直接喂 call_llm(tools=)
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.agent.directive import build_present_directive_schema
from src.core.prompts import get_prompt, get_tool_desc
from src.tools.schemas import to_openai_function_schema as _stats_parameters

# 注入参数：由 ToolRunner 在 execute 时强制注入，模型不可见、schema 中剔除
INJECTED_KEYS = frozenset({"world_id", "turn_num"})

# 各工具展示给模型的简短说明（决定模型何时/如何选用）；
# 正文外置 prompts.yaml（tools.*），_function 每次动态 get_tool_desc 支持热重载


def _drop_keys(parameters: Dict[str, Any], keys: frozenset) -> Dict[str, Any]:
    """从 parameters JSON Schema 剔除注入字段（properties 与 required 同步剔除）。"""
    props = dict(parameters.get("properties") or {})
    for k in keys:
        props.pop(k, None)
    required = [r for r in (parameters.get("required") or []) if r not in keys]
    out: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def _function(name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    """组装 OpenAI function 定义（description 动态读配置，热重载生效）。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": get_tool_desc(name),
            "parameters": parameters,
        },
    }


def _search_module_schema() -> Dict[str, Any]:
    """search_module 的 parameters：模型只填检索意图。"""
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": get_prompt("params.search_module.query")},
            "top_k": {"type": "integer", "description": get_prompt("params.search_module.top_k")},
        },
        "required": ["query"],
    }


def _query_memory_schema() -> Dict[str, Any]:
    """query_memory 的 parameters：模型只填语义化查询变体。"""
    return {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": get_prompt("params.query_memory.queries"),
            }
        },
        "required": ["queries"],
    }


def _manage_tags_schema() -> Dict[str, Any]:
    """manage_tags 的 parameters：模型只填目标实体与标签增删。"""
    return {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": get_prompt("params.manage_tags.entity_id")},
            "add_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": get_prompt("params.manage_tags.add_tags"),
            },
            "remove_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": get_prompt("params.manage_tags.remove_tags"),
            },
        },
        "required": ["entity_id"],
    }


def _get_pc_background_schema() -> Dict[str, Any]:
    """get_pc_background 的 parameters：模型可只定位目标 PC，缺省查全部。"""
    return {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": get_prompt("params.get_pc_background.entity_id"),
            },
        },
    }


def _search_rule_schema() -> Dict[str, Any]:
    """search_rule 的 parameters：模型只填规则检索意图（只读工具，无注入参数）。"""
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": get_prompt("params.search_rule.query"),
            },
            "top_k": {"type": "integer", "description": get_prompt("params.search_rule.top_k")},
        },
        "required": ["query"],
    }


def build_tool_schemas() -> List[Dict[str, Any]]:
    """返回 5 个原子工具的 OpenAI function 定义数组，供 call_llm(tools=...) 使用。

    check_and_update_stats 复用 tools.schemas 的 parameters 但剔除注入字段，
    其余四工具 schema 各自独立定义；字段名与工具实现完全一致。
    """
    stats_params = _drop_keys(_stats_parameters(), INJECTED_KEYS)
    return [
        _function("search_module", _search_module_schema()),
        _function("query_memory", _query_memory_schema()),
        _function("check_and_update_stats", stats_params),
        _function("manage_tags", _manage_tags_schema()),
        _function("get_pc_background", _get_pc_background_schema()),
        _function("search_rule", _search_rule_schema()),
    ]


def tool_names() -> List[str]:
    """返回 6 个原子工具的名字清单，供注册与日志使用。"""
    return [
        "search_module",
        "query_memory",
        "check_and_update_stats",
        "manage_tags",
        "get_pc_background",
        "search_rule",
    ]


def build_main_agent_schemas() -> List[Dict[str, Any]]:
    """主 Agent 完整工具清单：5 原子工具 + present_directive 收尾工具。

    供 Director.run_turn 喂给 call_llm(tools=...)；收尾工具由闭环 stop 语义拦截，
    不参与普通工具执行（runner 侧另有兜底 handler 防失效）。
    """
    return [*build_tool_schemas(), build_present_directive_schema()]
