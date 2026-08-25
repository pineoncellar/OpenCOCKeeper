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
from ..core.prompts import get_prompt


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
            "world_id": {"type": "string", "description": get_prompt("params.check_and_update_stats.world_id")},
            "entity_id": {"type": "string", "description": get_prompt("params.check_and_update_stats.entity_id")},
            "turn_num": {"type": "integer", "description": get_prompt("params.check_and_update_stats.turn_num")},
            "skill_or_attribute": {"type": "string", "description": get_prompt("params.check_and_update_stats.skill_or_attribute")},
            "target_value": {"type": "integer", "description": get_prompt("params.check_and_update_stats.target_value")},
            "difficulty": {"type": "string", "enum": ["regular", "hard", "extreme"], "description": get_prompt("params.check_and_update_stats.difficulty")},
            "bonus_penalty_dice": {"type": "integer", "description": get_prompt("params.check_and_update_stats.bonus_penalty_dice")},
            "hp_change": {"type": "integer", "description": get_prompt("params.check_and_update_stats.hp_change")},
            "mp_change": {"type": "integer", "description": get_prompt("params.check_and_update_stats.mp_change")},
            "san_change": {"type": "integer", "description": get_prompt("params.check_and_update_stats.san_change")},
            "san_sc_expression": {"type": "string", "description": get_prompt("params.check_and_update_stats.san_sc_expression")},
            "items_to_add": {"type": "array", "items": {"type": "object"}, "description": get_prompt("params.check_and_update_stats.items_to_add")},
            "items_to_remove": {"type": "array", "items": {"type": "string"}, "description": get_prompt("params.check_and_update_stats.items_to_remove")},
        },
        "required": ["world_id", "entity_id"],
    }
