# -*- coding: utf-8 -*-
"""
@File     :   get_pc_background.py
@Desc     :   角色背景查询工具：按需返回 PC 入模组前背景长文段（只读不写库）
@Note     :   背景是静态人物底稿（形象/信念/羁绊/创伤/背景故事），低频需求，
             由主 Agent 在扮演/动机/怀旧决策时主动调用；不随快照每轮注入，省 token。
             快照每轮仍给物理真相（HP/SAN），本工具只按需给静态底稿；
             背景语义——进入模组剧情之前的故事，模组内新事件走记忆库不写回背景
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..agent.assembler import render_background
from ..core.exceptions import EntityNotFoundError


def get_pc_background(storage, raw_input: dict) -> dict:
    """查询指定 PC 或本世界全部 PC 的背景长文段（只读）。

    raw_input: {world_id, entity_id?}
    entity_id 缺省时返回本世界全部调查员（type='PC'）的背景；
    目标实体不存在抛 EntityNotFoundError；无背景的实体 background 为 None。
    纯读取不写库，无 state_diff。
    """
    world_id = str(raw_input.get("world_id") or "").strip()
    entity_id = str(raw_input.get("entity_id") or "").strip()
    if not world_id:
        raise ValueError("world_id 为必填")

    if entity_id:
        entity = storage.get_entity(world_id, entity_id)
        if entity is None:
            raise EntityNotFoundError(f"实体不存在: {world_id}/{entity_id}")
        targets = [entity]  # 状态：显式定位单个 PC
    else:
        targets = storage.get_entities(world_id, entity_type="PC")  # 状态：缺省查全部 PC

    backgrounds: List[Dict[str, Any]] = []
    for e in targets:
        text = render_background(e.get("background"))
        backgrounds.append(
            {
                "entity_id": e["id"],
                "name": e["name"],
                # 状态：职业随背景一并返回，开场 Agent 据此做 PC 身份与切入场景对齐
                "occupation": (e.get("occupation") or "").strip() or None,
                "background": text or None,  # 无背景返回 None，模型可据 name 判断
            }
        )

    if not backgrounds:
        return {
            "ok": True,
            "backgrounds": [],
            "summary_for_agent": "本世界暂无调查员背景数据。",
        }

    parts = [f"{b['name']}（{b['entity_id']}）" for b in backgrounds]
    return {
        "ok": True,
        "backgrounds": backgrounds,
        "summary_for_agent": f"已返回 {len(backgrounds)} 名调查员背景：{'、'.join(parts)}。",
    }
