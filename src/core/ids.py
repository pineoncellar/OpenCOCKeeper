# -*- coding: utf-8 -*-
"""
@File     :   ids.py
@Desc     :   世界/实体/轮次 ID 的生成与校验约定，保证复合主键 world_id+id 可对齐
@Note     :   硬性边界：窗口/会话 id 仅存在于适配器层，存储层统一使用 world_id；
             本模块只负责 world/entity/turn 的 ID 约定，不引入窗口 id
"""

import re
from typing import Optional


# 实体类型 -> ID 前缀映射（与 新存储架构.md 的 type 枚举一致）
ENTITY_TYPE_PREFIX = {
    "PC": "player",
    "NPC": "npc",
    "SCENE": "scene",
    "ITEM": "item",
}

_RE_WORLD = re.compile(r"^[a-z0-9]+_[0-9]{3}_[a-z0-9_]+$")
_RE_ENTITY = re.compile(r"^(player|npc|scene|item)_[0-9]+$")


# ============================================
# ID 生成
# ============================================


def make_world_id(seq: int, slug: str, prefix: str = "world") -> str:
    """生成世界 ID：<prefix>_<序号3位>_<slug>，如 world_001_the_tomb。

    prefix 可换成 group/class 等，方便 OneBot 适配层按群聊生成 group_123_evening。
    """
    return f"{prefix}_{int(seq):03d}_{slug}"


def make_entity_id(entity_type: str, seq: int) -> str:
    """按实体类型生成实体 ID：player_01 等；未知类型抛 ValueError。

    seq 由世界导入/存储层按同类型递增分配，保证类型内唯一。
    """
    prefix = ENTITY_TYPE_PREFIX.get(str(entity_type).upper())
    if prefix is None:
        raise ValueError(f"未知实体类型 '{entity_type}'，可选: {list(ENTITY_TYPE_PREFIX)}")
    return f"{prefix}_{int(seq):02d}"


def make_turn_id(world_id: str, turn_num: int) -> str:
    """生成轮次 ID：<world_id>_t<轮次>，如 world_001_the_tomb_t3，可读且世界内唯一。"""
    return f"{world_id}_t{int(turn_num)}"


# ============================================
# ID 校验
# ============================================


def is_world_id(value: Optional[str]) -> bool:
    """校验 world_id 是否符合 <prefix>_<3位序号>_<slug> 约定。"""
    return bool(_RE_WORLD.match(value or ""))


def is_entity_id(value: Optional[str]) -> bool:
    """校验 entity_id 是否带已知类型前缀。"""
    return bool(_RE_ENTITY.match(value or ""))
