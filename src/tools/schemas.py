# -*- coding: utf-8 -*-
"""
@File     :   schemas.py
@Desc     :   状态更新工具输入结构 + 校验 + OpenAI Function Calling JSON Schema
@Note     :   三段式（检定/数值/背包）至少一段非空；纯 dict 校验，不引入 pydantic
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..core.exceptions import ConflictingInputError, EmptyUpdateError


@dataclass
class StatsUpdateInput:
    """规范化后的状态更新输入（解析自工具调用原始参数）。"""

    world_id: str
    entity_id: str
    turn_num: Optional[int] = None
    skill_or_attribute: Optional[str] = None
    target_value: Optional[int] = None
    difficulty: str = "regular"
    bonus_penalty_dice: int = 0
    hp_change: Optional[int] = None
    mp_change: Optional[int] = None
    san_change: Optional[int] = None
    san_sc_expression: Optional[str] = None
    items_to_add: Optional[List[dict]] = None
    items_to_remove: Optional[List[str]] = None

    def has_update(self) -> bool:
        """三段（检定/数值/背包）任一非空即视为有操作。"""
        return bool(
            self.skill_or_attribute
            or self.hp_change is not None
            or self.mp_change is not None
            or self.san_change is not None
            or self.san_sc_expression
            or self.items_to_add
            or self.items_to_remove
        )


_DIFFICULTY_VALUES = frozenset({"regular", "hard", "extreme"})


def parse_stats_update(raw: dict) -> StatsUpdateInput:
    """校验并归一化工具输入。

    三段全空抛 EmptyUpdateError；san_change 与 san_sc_expression 互斥抛
    ConflictingInputError；必填缺失 / 非法难度抛 ValueError。
    """
    world_id = str(raw.get("world_id") or "").strip()
    entity_id = str(raw.get("entity_id") or "").strip()
    if not world_id or not entity_id:
        raise ValueError("world_id / entity_id 为必填")

    difficulty = str(raw.get("difficulty") or "regular").strip().lower()
    if difficulty not in _DIFFICULTY_VALUES:
        raise ValueError(f"非法 difficulty: {difficulty}，可选 regular/hard/extreme")

    san_change = raw.get("san_change")
    san_sc = (raw.get("san_sc_expression") or "").strip() or None
    if san_change is not None and san_sc:
        raise ConflictingInputError("san_change 与 san_sc_expression 互斥，只能选其一")

    result = StatsUpdateInput(
        world_id=world_id,
        entity_id=entity_id,
        turn_num=raw.get("turn_num"),
        skill_or_attribute=(raw.get("skill_or_attribute") or "").strip() or None,
        target_value=raw.get("target_value"),
        difficulty=difficulty,
        bonus_penalty_dice=int(raw.get("bonus_penalty_dice") or 0),
        hp_change=raw.get("hp_change"),
        mp_change=raw.get("mp_change"),
        san_change=san_change,
        san_sc_expression=san_sc,
        items_to_add=raw.get("items_to_add"),
        items_to_remove=raw.get("items_to_remove"),
    )
    if not result.has_update():
        raise EmptyUpdateError("检定/数值/背包三段至少需提供一段")
    return result


def to_openai_function_schema() -> Dict[str, Any]:
    """导出 OpenAI Function Calling 的 parameters JSON Schema（供后续挂接主 Agent）。"""
    return {
        "type": "object",
        "properties": {
            "world_id": {"type": "string", "description": "世界隔离 ID"},
            "entity_id": {"type": "string", "description": "目标实体 ID（player_01 等）"},
            "turn_num": {"type": "integer", "description": "本轮次号（协调器提交时绑定，工具本身不依赖）"},
            "skill_or_attribute": {"type": "string", "description": "检定项：技能名/八属性/理智"},
            "target_value": {"type": "integer", "description": "显式目标值；缺省自动从属性表解析"},
            "difficulty": {"type": "string", "enum": ["regular", "hard", "extreme"], "description": "检定难度"},
            "bonus_penalty_dice": {"type": "integer", "description": "奖惩骰：正=奖励骰数，负=惩罚骰数"},
            "hp_change": {"type": "integer", "description": "HP 增量（支持负值）"},
            "mp_change": {"type": "integer", "description": "MP 增量（支持负值）"},
            "san_change": {"type": "integer", "description": "SAN 增量（支持负值），与 san_sc_expression 互斥"},
            "san_sc_expression": {"type": "string", "description": "SC 表达式如 1/1d3（成功/失败两档理智损失）"},
            "items_to_add": {"type": "array", "items": {"type": "object"}, "description": "新增背包物品列表（须含 name）"},
            "items_to_remove": {"type": "array", "items": {"type": "string"}, "description": "按名称移除的背包物品"},
        },
        "required": ["world_id", "entity_id"],
    }
