# -*- coding: utf-8 -*-
"""
@File     :   test_tool_commit.py
@Desc     :   合并提交协调器单测：多 diff 合并为单轮、空 diff 零变更落库、undo 全量还原
@Note     :   验证「一轮玩家输入 = 一条 turn = 一次撤销单元」的端到端闭环
"""

import pytest

from src.tools.check_and_update_stats import check_and_update_stats as check_stats
from src.tools.commit import apply_turn_change as commit
from src.tools.manage_tags import manage_tags as tags_tool


@pytest.fixture
def entity(storage, world_id):
    return storage.create_entity(
        world_id,
        "player_01",
        "PC",
        "玩家一号",
        hp=10,
        hp_max=12,
        san=58,
        san_max=70,
        attributes_and_skills={"侦查": 60},
        inventory=[{"name": "手电筒", "quantity": 1}],
        tags=["旧标签"],
    )


def test_commit_merges_multi_tool_diffs_into_one_turn(storage, world_id, entity):
    # 一轮内多次工具调用：扣血、加物品、打 tag
    d1 = check_stats(storage, {"world_id": world_id, "entity_id": entity["id"], "hp_change": -3})["state_diff"]
    d2 = check_stats(storage, {"world_id": world_id, "entity_id": entity["id"], "items_to_add": [{"name": "钥匙"}]})["state_diff"]
    d3 = tags_tool(storage, {"world_id": world_id, "entity_id": entity["id"], "add_tags": ["流血"]})["state_diff"]

    turn = commit(storage, world_id, 1, [d1, d2, d3])
    assert turn["turn_num"] == 1

    fresh = storage.get_entity(world_id, entity["id"])
    assert fresh["hp"] == 7
    assert any(i["name"] == "钥匙" for i in fresh["inventory"])
    assert "流血" in fresh["tags"]
    # 单轮单 diff
    assert len(storage.get_recent_turns(world_id)) == 1


def test_undo_restores_everything(storage, world_id, entity):
    d1 = check_stats(storage, {"world_id": world_id, "entity_id": entity["id"], "hp_change": -3})["state_diff"]
    d2 = check_stats(storage, {"world_id": world_id, "entity_id": entity["id"], "items_to_add": [{"name": "钥匙"}]})["state_diff"]
    d3 = tags_tool(storage, {"world_id": world_id, "entity_id": entity["id"], "add_tags": ["流血"]})["state_diff"]
    commit(storage, world_id, 1, [d1, d2, d3])

    storage.undo_turn(world_id, 1)
    fresh = storage.get_entity(world_id, entity["id"])
    assert fresh["hp"] == 10
    assert fresh["inventory"] == [{"name": "手电筒", "quantity": 1}]
    assert fresh["tags"] == ["旧标签"]


def test_empty_diff_still_writes_turn(storage, world_id, entity):
    # 空 diff 也安全落库（主 Agent 统一写入入口），零变更但轮次连续
    turn = commit(storage, world_id, 1, [])
    assert turn is not None
    assert turn["state_diff"] == {"numeric_changes": {}, "tags": {}, "inventory": {}}
    assert storage.get_turn(world_id, 1) is not None


def test_same_turn_numeric_changes_accumulate(storage, world_id, entity):
    # 同一轮两次扣血：-3 再 -2 → 合并为 -5 单条 diff
    d1 = check_stats(storage, {"world_id": world_id, "entity_id": entity["id"], "hp_change": -3})["state_diff"]
    d2 = check_stats(storage, {"world_id": world_id, "entity_id": entity["id"], "hp_change": -2})["state_diff"]
    commit(storage, world_id, 1, [d1, d2])
    turn = storage.get_turn(world_id, 1)
    assert turn["state_diff"]["numeric_changes"] == {"player_01.hp": -5}
    assert storage.get_entity(world_id, entity["id"])["hp"] == 5


def test_commit_then_undo_is_idempotent_snapshot(storage, world_id, entity):
    # 提交后 undo，再提交空 diff：状态保持原始
    d1 = check_stats(storage, {"world_id": world_id, "entity_id": entity["id"], "hp_change": -3})["state_diff"]
    commit(storage, world_id, 1, [d1])
    storage.undo_turn(world_id, 1)
    assert storage.get_entity(world_id, entity["id"])["hp"] == 10
