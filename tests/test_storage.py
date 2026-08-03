# -*- coding: utf-8 -*-
"""
@File     :   test_storage.py
@Desc     :   存储层测试：世界/实体/轮次/历史 CRUD、回档、窗口裁剪、世界隔离、级联删除、schema 迁移
@Note     :   所有用例走 conftest 的独立临时库 + 900 测试段 world_id，不影响真实 data/app.db
"""

from __future__ import annotations

import pytest

from src.core.db import Database
from src.core.exceptions import (
    EntityNotFoundError,
    StorageError,
    TurnNotFoundError,
    WorldNotFoundError,
)
from src.core.ids import make_entity_id, make_world_id
from src.storage.schema import MIGRATIONS
from src.storage.storage import Storage


# ====================================================================
# 世界
# ====================================================================


def test_ensure_world_defaults(storage):
    wid = make_world_id(900, "alpha")
    world = storage.ensure_world(wid)
    assert world["world_id"] == wid
    assert world["player_ids"] == []
    assert world["game_phase"] == "EXPLORATION"
    assert world["global_flags"] == {}
    assert world["global_recap"] == ""  # 迁移 1 补列后默认空串


def test_ensure_world_idempotent(storage):
    wid = make_world_id(900, "beta")
    first = storage.ensure_world(wid, player_ids=["player_01"])
    second = storage.ensure_world(wid, player_ids=["player_99"])  # 已存在则忽略
    assert first["player_ids"] == ["player_01"]
    assert second["player_ids"] == ["player_01"]


def test_update_world_global_recap(storage, world_id):
    storage.update_world(world_id, global_recap="队伍在耳室发现卡纳的日记")
    assert storage.get_world(world_id)["global_recap"] == "队伍在耳室发现卡纳的日记"
    storage.update_world(world_id, global_recap="")  # 传空串主动清空
    assert storage.get_world(world_id)["global_recap"] == ""


def test_update_world_partial_keeps_others(storage, world_id):
    storage.update_world(world_id, game_phase="COMBAT")
    world = storage.get_world(world_id)
    assert world["game_phase"] == "COMBAT"
    assert world["global_flags"] == {}
    assert world["global_recap"] == ""


def test_get_world_missing_returns_none(storage):
    assert storage.get_world(make_world_id(900, "nope")) is None


# ====================================================================
# 世界隔离：不同世界数据互不可见
# ====================================================================


def test_world_isolation(storage):
    a = make_world_id(900, "world_a")
    b = make_world_id(900, "world_b")
    storage.ensure_world(a, global_recap="A 的提要")
    storage.ensure_world(b, global_recap="B 的提要")
    storage.create_entity(a, "player_01", "PC", "费莉西蒂", hp=10)
    storage.append_turn(a, turn_num=1, state_diff={"numeric_changes": {"player_01.hp": -5}})
    storage.append_history(a, "user", "A 的对话")

    assert storage.get_entities(b) == []
    assert storage.get_world(b)["global_recap"] != storage.get_world(a)["global_recap"]
    assert storage.get_turn(b, 1) is None
    assert storage.query_history(b) == []


# ====================================================================
# 实体
# ====================================================================


def test_entity_crud_and_stats(storage, world_id):
    storage.create_entity(world_id, "player_01", "PC", "费莉西蒂", hp=10, hp_max=12,
                          attributes_and_skills={"侦查": 60}, inventory=[{"name": "手电筒"}])
    storage.adjust_stat(world_id, "player_01", "hp", -4)
    assert storage.get_entity(world_id, "player_01")["hp"] == 6
    storage.update_entity(world_id, "player_01", tags=["手臂流血"])
    assert storage.get_entity(world_id, "player_01")["tags"] == ["手臂流血"]
    storage.delete_entity(world_id, "player_01")
    assert storage.get_entity(world_id, "player_01") is None


def test_entity_requires_world(storage):
    with pytest.raises(WorldNotFoundError):
        storage.create_entity(make_world_id(900, "ghost"), "player_01", "PC", "x")


def test_entity_unknown_field_rejected(storage, world_id):
    storage.create_entity(world_id, "player_01", "PC", "费莉西蒂")
    with pytest.raises(ValueError):
        storage.update_entity(world_id, "player_01", hp="not-int")


def test_tags_add_remove(storage, world_id):
    storage.create_entity(world_id, "player_01", "PC", "费莉西蒂")
    storage.add_tag(world_id, "player_01", "临时发狂:纵火狂")
    storage.add_tag(world_id, "player_01", "临时发狂:纵火狂")  # 去重
    assert storage.get_entity(world_id, "player_01")["tags"] == ["临时发狂:纵火狂"]
    storage.remove_tag(world_id, "player_01", "临时发狂:纵火狂")
    assert storage.get_entity(world_id, "player_01")["tags"] == []


# ====================================================================
# 轮次 / 回档
# ====================================================================


def test_append_and_get_turn(storage, world_id):
    storage.append_turn(world_id, turn_num=1, context_data={"user": "hi"},
                        state_diff={"numeric_changes": {"player_01.hp": -5}})
    turn = storage.get_turn(world_id, 1)
    assert turn["turn_num"] == 1
    assert turn["state_diff"]["numeric_changes"] == {"player_01.hp": -5}


def test_recent_window_prunes_old_turns(storage, world_id):
    storage.create_entity(world_id, "player_01", "PC", "费莉西蒂")
    storage = Storage(db=storage.db, turns_window=3)  # 用更小窗口便于断言
    for n in range(1, 6):
        storage.append_turn(world_id, turn_num=n, state_diff={"numeric_changes": {}})
    turns = storage.get_recent_turns(world_id)
    assert [t["turn_num"] for t in turns] == [3, 4, 5]
    assert storage.get_turn(world_id, 1) is None  # 超窗不可回档


def test_undo_turn_restores_state(storage, world_id):
    storage.create_entity(world_id, "player_01", "PC", "费莉西蒂", hp=10)
    storage.add_tag(world_id, "player_01", "手臂流血")
    storage.append_turn(
        world_id, turn_num=1,
        state_diff={"numeric_changes": {"player_01.hp": -6},
                    "tags": {"player_01": {"added": ["手臂流血"], "removed": []}},
                    "inventory": {"player_01": {"added": [{"name": "火柴"}], "removed": []}}},
    )
    # 本轮内把 hp 扣到 4
    storage.adjust_stat(world_id, "player_01", "hp", -6)
    assert storage.get_entity(world_id, "player_01")["hp"] == 4
    storage.undo_turn(world_id, 1)
    entity = storage.get_entity(world_id, "player_01")
    assert entity["hp"] == 10
    assert entity["tags"] == []
    assert entity["inventory"] == []
    assert storage.get_turn(world_id, 1) is None  # 撤销后轮次记录被删除


def test_undo_inventory_removes_by_name(storage, world_id):
    storage.create_entity(world_id, "player_01", "PC", "费莉西蒂")
    storage.append_turn(
        world_id, turn_num=1,
        state_diff={"numeric_changes": {},
                    "tags": {}, "inventory": {"player_01": {"added": [{"name": "火柴"}], "removed": []}}},
    )
    storage.update_entity(world_id, "player_01", inventory=[{"name": "火柴"}, {"name": "手电筒"}])
    storage.undo_turn(world_id, 1)
    assert storage.get_entity(world_id, "player_01")["inventory"] == [{"name": "手电筒"}]


def test_undo_missing_turn_raises(storage, world_id):
    with pytest.raises(TurnNotFoundError):
        storage.undo_turn(world_id, 99)


# ====================================================================
# 历史冷备
# ====================================================================


def test_history_append_and_query(storage, world_id):
    storage.append_history(world_id, "user", "第一句")
    storage.append_history(world_id, "assistant", "第二句")
    rows = storage.query_history(world_id)
    assert [r["content"] for r in rows] == ["第一句", "第二句"]
    after = storage.query_history(world_id, since_id=1)
    assert [r["content"] for r in after] == ["第二句"]
    assert storage.query_history(world_id, limit=1)[0]["content"] == "第一句"


# ====================================================================
# 级联删除
# ====================================================================


def test_delete_world_cascades(storage):
    wid = make_world_id(900, "cascade")
    storage.ensure_world(wid)
    storage.create_entity(wid, "player_01", "PC", "费莉西蒂")
    storage.append_turn(wid, turn_num=1, state_diff={"numeric_changes": {}})
    storage.append_history(wid, "user", "内容")
    assert storage.delete_world(wid) is True
    assert storage.get_world(wid) is None
    assert storage.get_entities(wid) == []
    assert storage.get_turn(wid, 1) is None
    assert storage.query_history(wid) == []


# ====================================================================
# schema 迁移：旧库补 global_recap 列
# ====================================================================


def test_migration_adds_global_recap_to_old_db(tmp_path):
    # 模拟存量库：仅应用迁移 0，再走 Storage 自动迁移补齐
    db = Database(tmp_path / "old.db")
    db.migrate(MIGRATIONS[:1])
    assert db.user_version() == 1
    s = Storage(db=db)  # 触发迁移 1 + 迁移 2
    assert db.user_version() == len(MIGRATIONS)
    conn = db.connect()
    world_cols = [r[1] for r in conn.execute("PRAGMA table_info(world_state)")]
    turn_cols = [r[1] for r in conn.execute("PRAGMA table_info(recent_turns)")]
    conn.close()
    assert "global_recap" in world_cols  # 迁移 1：宏观前情提要列
    assert "solidified" in turn_cols  # 迁移 2：固化进度标记列
    wid = make_world_id(900, "migrated")
    s.ensure_world(wid)
    assert s.get_world(wid)["global_recap"] == ""
