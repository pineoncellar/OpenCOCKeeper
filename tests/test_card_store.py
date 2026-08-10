# -*- coding: utf-8 -*-
"""
@File     :   test_card_store.py
@Desc     :   角色卡种子库测试：保存/列出/读取/拷贝到世界
@Note     :   SEED_DIR monkeypatch 到 tmp_path，不碰真实 data/cards/imported；
             拷贝语义=读种子 entity -> create_entity 到世界 + 绑定 player_ids，种子文件不动
"""

from __future__ import annotations

import pytest

from src.tools import card_store


@pytest.fixture(autouse=True)
def _seed_tmp(tmp_path, monkeypatch):
    """每个测试独立的种子目录。"""
    monkeypatch.setattr(card_store, "SEED_DIR", tmp_path / "imported")
    return tmp_path


def _sample_entity():
    return {
        "entity_type": "PC",
        "name": "费莉西蒂",
        "hp": 11, "hp_max": 11,
        "mp": 14, "mp_max": 14,
        "san": 70, "san_max": 70,
        "attributes_and_skills": {"STR": 50, "侦查": 60},
        "inventory": [{"name": "左轮手枪"}],
        "background": {"belief": "真正的力量"},
        "tags": [],
    }


def test_save_and_list(storage, _seed_tmp):
    """save_seed 落 JSON 文件，list_seed_cards 返回 meta 摘要。"""
    meta = {"name": "费莉西蒂", "gender": "女", "age": 24, "occupation": "私家侦探", "luck": 55}
    seed_id = card_store.save_seed(_sample_entity(), meta, source="卡.xlsx")
    assert seed_id.startswith("card_")
    rows = card_store.list_seed_cards()
    assert len(rows) == 1
    assert rows[0]["seed_id"] == seed_id
    assert rows[0]["name"] == "费莉西蒂"
    assert rows[0]["occupation"] == "私家侦探"
    assert rows[0]["luck"] == 55
    assert rows[0]["source"] == "卡.xlsx"


def test_load_seed_roundtrip(storage, _seed_tmp):
    """load_seed 还原 {meta, entity}。"""
    seed_id = card_store.save_seed(_sample_entity(), {"name": "约翰"}, source="x")
    data = card_store.load_seed(seed_id)
    assert data["entity"]["name"] == "费莉西蒂"
    assert data["entity"]["background"]["belief"] == "真正的力量"
    assert data["meta"]["name"] == "约翰"


def test_load_seed_missing_raises(_seed_tmp):
    """种子不存在抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        card_store.load_seed("card_nope")


def test_list_empty(_seed_tmp):
    """种子目录为空返回空列表。"""
    assert card_store.list_seed_cards() == []


def test_copy_seed_to_world(storage, world_id, _seed_tmp):
    """拷贝到世界：create_entity + 绑定 player_ids，种子文件保持不动。"""
    seed_id = card_store.save_seed(_sample_entity(), {"name": "费莉西蒂"}, source="x")
    eid = card_store.copy_seed_to_world(storage, seed_id, world_id)
    assert eid.startswith("player_")
    entity = storage.get_entity(world_id, eid)
    assert entity is not None
    assert entity["name"] == "费莉西蒂"
    assert entity["hp"] == 11 and entity["san_max"] == 70
    assert entity["background"]["belief"] == "真正的力量"
    # player_ids 绑定
    world = storage.get_world(world_id)
    assert eid in world["player_ids"]
    # 种子文件仍在（拷贝语义）
    assert card_store.list_seed_cards()  # 非空


def test_copy_seed_assigns_unique_ids(storage, world_id, _seed_tmp):
    """连续拷贝两次分配不同 entity_id，不覆盖、player_ids 含两者。"""
    seed_id = card_store.save_seed(_sample_entity(), {"name": "费莉西蒂"}, source="x")
    eid1 = card_store.copy_seed_to_world(storage, seed_id, world_id)
    eid2 = card_store.copy_seed_to_world(storage, seed_id, world_id)
    assert eid1 != eid2
    assert storage.get_entity(world_id, eid1) is not None
    assert storage.get_entity(world_id, eid2) is not None
    pids = storage.get_world(world_id)["player_ids"]
    assert eid1 in pids and eid2 in pids
