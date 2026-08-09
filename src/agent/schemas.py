# -*- coding: utf-8 -*-
"""
@File     :   schemas.py
@Desc     :   主 Agent 4 个原子工具的 OpenAI Function Calling schema 集中注册
@Note     :   注入参数剥离——world_id / turn_num 由调度器运行时注入，不暴露给模型填写，
             杜绝跨世界/错轮次；check_and_update_stats 复用 src.tools.schemas 的
             parameters 但剔除注入字段；build_tool_schemas 产出可直接喂 call_llm(tools=)
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.tools.schemas import to_openai_function_schema as _stats_parameters

# 注入参数：由 ToolRunner 在 execute 时强制注入，模型不可见、schema 中剔除
INJECTED_KEYS = frozenset({"world_id", "turn_num"})

# 各工具展示给模型的简短说明（决定模型何时/如何选用）
_TOOL_DESC = {
    "search_module": (
        "查阅当前世界绑定模组的原文章节。当剧情需要场景描写、设定细节、"
        "线索或规则提示时调用；返回未加工的原文切片"
    ),
    "query_memory": (
        "检索世界内长程剧情记忆。当需要回顾过往伏笔、历史事件或确认玩家"
        "是否知晓某信息时调用；queries 建议给 1~3 条描述性查询变体"
    ),
    "check_and_update_stats": (
        "执行规则检定、扣减 HP/MP/SAN、增删背包物品。当玩家行动涉及"
        "属性检定或状态数值变化时调用；纯计算，落库由协调器统一提交"
    ),
    "manage_tags": (
        "对实体或场景增删动态状态标签（如'流血'、'昏暗'）。当环境或角色"
        "物理/精神状态发生变化时调用，供后续上下文引用"
    ),
}


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
    """组装 OpenAI function 定义（description 从 _TOOL_DESC 取）。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": _TOOL_DESC[name],
            "parameters": parameters,
        },
    }


def _search_module_schema() -> Dict[str, Any]:
    """search_module 的 parameters：模型只填检索意图。"""
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要查阅的模组内容描述，如'耳室壁画上的符号含义'"},
            "top_k": {"type": "integer", "description": "返回章节数，缺省 2"},
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
                "description": "1~3 条描述性查询变体（描述性表述优于单感官关键词）",
            }
        },
        "required": ["queries"],
    }


def _manage_tags_schema() -> Dict[str, Any]:
    """manage_tags 的 parameters：模型只填目标实体与标签增删。"""
    return {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "目标实体 ID（PC/NPC/SCENE/ITEM）"},
            "add_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要添加的状态标签，如'流血'、'昏暗'",
            },
            "remove_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要移除的状态标签",
            },
        },
        "required": ["entity_id"],
    }


def build_tool_schemas() -> List[Dict[str, Any]]:
    """返回 4 个原子工具的 OpenAI function 定义数组，供 call_llm(tools=...) 使用。

    check_and_update_stats 复用 tools.schemas 的 parameters 但剔除注入字段，
    其余三工具 schema 各自独立定义；字段名与工具实现完全一致。
    """
    stats_params = _drop_keys(_stats_parameters(), INJECTED_KEYS)
    return [
        _function("search_module", _search_module_schema()),
        _function("query_memory", _query_memory_schema()),
        _function("check_and_update_stats", stats_params),
        _function("manage_tags", _manage_tags_schema()),
    ]


def tool_names() -> List[str]:
    """返回 4 个原子工具的名字清单，供注册与日志使用。"""
    return ["search_module", "query_memory", "check_and_update_stats", "manage_tags"]
