# -*- coding: utf-8 -*-
"""
@File     :   manage_tags.py
@Desc     :   实体状态 Tag 管理工具：增删去重，返回镜像与 diff（不写库）
@Note     :   动态精神/物理状态（如 ["流血"]、["临时发狂"]）由主 Agent 决策后经本工具落地；
             写库统一走 commit.apply_turn_change，与状态更新工具同一条回档链路
"""

from __future__ import annotations

from typing import List

from ..core.exceptions import EntityNotFoundError, EmptyUpdateError
from ..storage.diff import empty_diff, record_tag_change


def manage_tags(storage, raw_input: dict) -> dict:
    """对实体增删 Tag（去重），返回 old/new 镜像与 state_diff。

    raw_input: {world_id, entity_id, add_tags: [...], remove_tags: [...]}
    add_tags / remove_tags 均为空抛 EmptyUpdateError；纯计算不写库。
    """
    world_id = str(raw_input.get("world_id") or "").strip()
    entity_id = str(raw_input.get("entity_id") or "").strip()
    add = list(raw_input.get("add_tags") or [])
    remove = list(raw_input.get("remove_tags") or [])
    if not world_id or not entity_id:
        raise ValueError("world_id / entity_id 为必填")
    if not add and not remove:
        raise EmptyUpdateError("add_tags / remove_tags 至少提供其一")

    entity = storage.get_entity(world_id, entity_id)
    if entity is None:
        raise EntityNotFoundError(f"实体不存在: {world_id}/{entity_id}")

    old_tags = list(entity["tags"] or [])
    new_tags = list(old_tags)
    added: List[str] = []
    removed: List[str] = []
    for tag in add:
        if tag not in new_tags:
            new_tags.append(tag)
            added.append(tag)
    for tag in remove:
        if tag in new_tags:
            new_tags.remove(tag)
            removed.append(tag)

    diff = empty_diff()
    for tag in added:
        record_tag_change(diff, entity_id, tag, removed=False)
    for tag in removed:
        record_tag_change(diff, entity_id, tag, removed=True)

    parts = [f"打上 {t}" for t in added] + [f"移除 {t}" for t in removed]
    summary = f"实体 {entity_id} " + ("，".join(parts) if parts else "标签无变化") + "。"
    return {
        "ok": True,
        "entity_id": entity_id,
        "tags_changed": {
            "old": old_tags,
            "new": new_tags,
            "added": added,
            "removed": removed,
        },
        "state_diff": diff,
        "summary_for_agent": summary,
    }
