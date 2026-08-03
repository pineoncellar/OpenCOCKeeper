# -*- coding: utf-8 -*-
"""
@File     :   diff.py
@Desc     :   state_diff 原子增量 Diff 结构的构建、合并与取反（供 undo 反向撤销）
@Note     :   结构见 docs/新存储架构.md 决策 2：
             numeric_changes{点号路径: delta} + tags/inventory{实体: {added, removed}}
"""

from typing import Any, Dict, List


# ============================================
# 结构与基础判断
# ============================================


def empty_diff() -> dict:
    """返回一份空的 state_diff 结构。"""
    return {"numeric_changes": {}, "tags": {}, "inventory": {}}


def is_empty(diff: dict) -> bool:
    """判断 diff 是否没有任何变更（三个分区全空）。"""
    return not diff["numeric_changes"] and not diff["tags"] and not diff["inventory"]


def _list_change(holder: dict, value: Any, removed: bool) -> None:
    """向 holder 的 added/removed 写入一个值；同值增删互相抵消，保证 diff 最小。

    抵消的意义：同一轮内先加后删等于没变，反向撤销时不会出现先删再加的冗余操作。
    """
    if removed:
        if value in holder["added"]:
            holder["added"].remove(value)
        else:
            holder["removed"].append(value)
    else:
        if value in holder["removed"]:
            holder["removed"].remove(value)
        else:
            holder["added"].append(value)


# ============================================
# 变更记录
# ============================================


def record_numeric_change(diff: dict, path: str, delta: int) -> None:
    """累计数值增量：同一路径多次修改按 delta 求和。

    例：record_numeric_change(d, "player_01.hp", -5) 后 player_01.hp 增量记为 -5。
    """
    diff["numeric_changes"][path] = diff["numeric_changes"].get(path, 0) + delta


def record_tag_change(diff: dict, entity_id: str, tag: str, removed: bool = False) -> None:
    """记录 Tag 增删；相同 Tag 在同一 diff 内的增删自动抵消。"""
    holder = diff["tags"].setdefault(entity_id, {"added": [], "removed": []})
    _list_change(holder, tag, removed)


def record_inventory_change(diff: dict, entity_id: str, item: dict, removed: bool = False) -> None:
    """记录背包物品增删；按字典相等判断同物抵消。"""
    holder = diff["inventory"].setdefault(entity_id, {"added": [], "removed": []})
    _list_change(holder, item, removed)


def merge_diff(base: dict, other: dict) -> dict:
    """把 other 并入 base，返回 base（同一轮多次工具修改时聚合）。

    数值按增量累加，tags/inventory 走带抵消的合并，最终结构仍为最小 diff。
    """
    for path, delta in other["numeric_changes"].items():
        record_numeric_change(base, path, delta)
    for entity_id, holder in other["tags"].items():
        for tag in holder["added"]:
            record_tag_change(base, entity_id, tag, removed=False)
        for tag in holder["removed"]:
            record_tag_change(base, entity_id, tag, removed=True)
    for entity_id, holder in other["inventory"].items():
        for item in holder["added"]:
            record_inventory_change(base, entity_id, item, removed=False)
        for item in holder["removed"]:
            record_inventory_change(base, entity_id, item, removed=True)
    return base


# ============================================
# 取反（撤销用）
# ============================================


def negate_diff(diff: dict) -> dict:
    """生成反向 diff 供撤销：数值取反，tags/inventory 的 added/removed 互换。

    返回结构与普通 diff 完全一致，可直接交给 apply 逻辑当作正向 diff 处理。
    """
    reverse = empty_diff()
    for path, delta in diff["numeric_changes"].items():
        reverse["numeric_changes"][path] = -delta
    for entity_id, holder in diff["tags"].items():
        for tag in holder["added"]:
            record_tag_change(reverse, entity_id, tag, removed=True)
        for tag in holder["removed"]:
            record_tag_change(reverse, entity_id, tag, removed=False)
    for entity_id, holder in diff["inventory"].items():
        for item in holder["added"]:
            record_inventory_change(reverse, entity_id, item, removed=True)
        for item in holder["removed"]:
            record_inventory_change(reverse, entity_id, item, removed=False)
    return reverse
