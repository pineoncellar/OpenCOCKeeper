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

from src.agent.directive import PRESENT_DIRECTIVE_SCHEMA
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
        "是否知晓某信息时调用；queries 建议给 1~3 条描述性查询变体。"
        "注意：调查员的【角色背景】小节已在上下文中给出其进入模组剧情之前的故事"
        "（人物底色/动机/羁绊/创伤），属人物底稿而非检索目标；"
        "本工具检索的是模组剧情内发生的事件记忆"
    ),
    "check_and_update_stats": (
        "执行规则检定、扣减 HP/MP/SAN、增删背包物品。当玩家行动涉及"
        "属性检定或状态数值变化时调用；纯计算，落库由协调器统一提交"
    ),
    "manage_tags": (
        "对实体或场景增删动态状态标签（如'流血'、'昏暗'）。当环境或角色"
        "物理/精神状态发生变化时调用，供后续上下文引用"
    ),
    "get_pc_background": (
        "查询调查员进入模组剧情之前的故事（形象描述/思想与信念/重要之人/"
        "意义非凡之地/宝贵之物/特质/伤口疤痕/恐惧症躁狂症/背景故事），"
        "属人物底稿而非剧情记忆；当扮演或叙事决策需要人物动机、羁绊或过往时调用；"
        "entity_id 缺省返回本世界全部 PC"
    ),
    "search_rule": (
        "检索《克苏鲁的呼唤》基础规则库原文（data/rules 规则库）。当需要确认具体"
        "规则条文、判定难度或数值流程（如某技能检定难度、理智损失、伤害与贯穿、"
        "奖励/惩罚骰）时调用；返回未加工的规则原文切片"
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


def _get_pc_background_schema() -> Dict[str, Any]:
    """get_pc_background 的 parameters：模型可只定位目标 PC，缺省查全部。"""
    return {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "目标 PC 实体 ID（如快照【调查员状态】所示），缺省返回本世界全部 PC",
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
                "description": "要查阅的规则内容描述，如'理智检定失败的后果'、'贯穿伤害如何结算'",
            },
            "top_k": {"type": "integer", "description": "返回规则段落数，缺省 3"},
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
    return [*build_tool_schemas(), PRESENT_DIRECTIVE_SCHEMA]
