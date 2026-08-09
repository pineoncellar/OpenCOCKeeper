# -*- coding: utf-8 -*-
"""
@File     :   commit.py
@Desc     :   合并提交协调器：把一轮多次工具调用产生的 diff 合并为一条轮次记录
@Note     :   决策 D1 唯一落库路径——一轮玩家输入 = 一条 turn = 一条 diff = 一次可撤销单元；
             空 diff 不产生轮次，避免 recent_turns 膨胀
"""

from __future__ import annotations

from typing import List, Optional

from ..storage.diff import empty_diff, merge_diff


def apply_turn_change(
    storage,
    world_id: str,
    turn_num: int,
    diffs: Optional[List[dict]] = None,
    *,
    context_data: Optional[dict] = None,
) -> dict:
    """合并一轮所有工具 diff 并原子落库，返回该轮记录；空 diff 也安全落库。

    这是主 Agent 每轮唯一的落库入口——统一调用，无论本轮是否有物理变更，
    都写入一条轮次记录（空 diff 为零变更），保证近程对话与轮次连续完整。
    diffs 为本轮各工具返回的 state_diff 列表；合并走 merge_diff（同值增删抵消），
    落库走 storage.commit_turn（单事务应用 + 写轮次）。
    """
    merged = empty_diff()
    for d in diffs or []:
        merge_diff(merged, d)
    return storage.commit_turn(
        world_id, turn_num, state_diff=merged, context_data=context_data
    )
