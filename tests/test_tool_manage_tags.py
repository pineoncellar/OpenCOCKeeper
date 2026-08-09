# -*- coding: utf-8 -*-
"""
@File     :   test_tool_manage_tags.py
@Desc     :   标签管理工具单测：增删去重、空输入与实体缺失、diff 结构
@Note     :   纯计算不写库，写库统一由 commit.apply_turn_change 承担
"""

import pytest

from src.core.exceptions import EmptyUpdateError, EntityNotFoundError
from src.tools import manage_tags


@pytest.fixture
def entity(storage, world_id):
    return storage.create_entity(
        world_id,
        "player_01",
        "PC",
        "玩家一号",
        hp=10,
        hp_max=12,
        tags=["旧标签"],
    )


def _call(storage, entity, **kwargs):
    return manage_tags(
        storage,
        {"world_id": entity["world_id"], "entity_id": entity["id"], **kwargs},
    )


def test_add_tags_deduplicates(storage, entity):
    out = _call(storage, entity, add_tags=["流血", "流血", "临时发狂"])
    assert out["tags_changed"]["added"] == ["流血", "临时发狂"]
    assert "流血" in out["tags_changed"]["new"]
    assert "旧标签" in out["tags_changed"]["new"]  # 原标签保留


def test_remove_tags(storage, entity):
    out = _call(storage, entity, remove_tags=["旧标签"])
    assert out["tags_changed"]["removed"] == ["旧标签"]
    assert "旧标签" not in out["tags_changed"]["new"]


def test_add_and_remove_together(storage, entity):
    out = _call(storage, entity, add_tags=["流血"], remove_tags=["旧标签"])
    assert out["tags_changed"]["added"] == ["流血"]
    assert out["tags_changed"]["removed"] == ["旧标签"]
    diff = out["state_diff"]["tags"]["player_01"]
    assert diff["added"] == ["流血"]
    assert diff["removed"] == ["旧标签"]


def test_remove_nonexistent_is_noop(storage, entity):
    out = _call(storage, entity, remove_tags=["不存在的标签"])
    assert out["tags_changed"]["removed"] == []
    assert out["tags_changed"]["new"] == ["旧标签"]


def test_empty_input_raises(storage, entity):
    with pytest.raises(EmptyUpdateError):
        _call(storage, entity)


def test_missing_entity_raises(storage, world_id):
    with pytest.raises(EntityNotFoundError):
        manage_tags(
            storage,
            {"world_id": world_id, "entity_id": "ghost_99", "add_tags": ["流血"]},
        )


def test_tool_never_writes_db(storage, entity):
    _call(storage, entity, add_tags=["流血"], remove_tags=["旧标签"])
    fresh = storage.get_entity(entity["world_id"], entity["id"])
    assert fresh["tags"] == ["旧标签"]  # 库内未变
