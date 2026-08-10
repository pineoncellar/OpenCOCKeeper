# -*- coding: utf-8 -*-
"""
@File     :   test_tool_get_pc_background.py
@Desc     :   get_pc_background 工具测试：按需查询 PC 入模组前背景（只读不写库）
@Note     :   背景不随快照注入，改由主 Agent 经本工具按需查询；纯读取无 state_diff
"""

from __future__ import annotations

import pytest

from src.core.exceptions import EntityNotFoundError
from src.tools.get_pc_background import get_pc_background


def _seed_pc(storage, world_id, entity_id="pc_01", name="费莉西蒂", background=None):
    storage.create_entity(
        world_id, entity_id, "PC", name,
        hp=11, hp_max=12, san=59, san_max=99,
        background=background,
    )


def test_query_all_pcs(storage, world_id):
    """缺省 entity_id：返回本世界全部 PC 的背景长文段。"""
    _seed_pc(storage, world_id, "pc_01", "费莉西蒂", {
        "appearance_desc": "略卷的蓝褐色长发",
        "belief": "真正的力量",
    })
    _seed_pc(storage, world_id, "pc_02", "约翰", None)
    result = get_pc_background(storage, {"world_id": world_id})
    assert result["ok"] is True
    bgs = {b["entity_id"]: b for b in result["backgrounds"]}
    assert set(bgs) == {"pc_01", "pc_02"}
    assert "【形象描述】略卷的蓝褐色长发" in bgs["pc_01"]["background"]
    assert "【思想与信念】真正的力量" in bgs["pc_01"]["background"]
    assert bgs["pc_02"]["background"] is None  # 无背景返回 None
    assert "费莉西蒂" in result["summary_for_agent"]


def test_query_specific_entity(storage, world_id):
    """显式 entity_id：只返回该实体背景。"""
    _seed_pc(storage, world_id, "pc_01", "费莉西蒂", {"belief": "信念A"})
    _seed_pc(storage, world_id, "pc_02", "约翰", {"belief": "信念B"})
    result = get_pc_background(storage, {"world_id": world_id, "entity_id": "pc_02"})
    assert len(result["backgrounds"]) == 1
    assert result["backgrounds"][0]["entity_id"] == "pc_02"
    assert "信念B" in result["backgrounds"][0]["background"]
    assert "信念A" not in result["backgrounds"][0]["background"]


def test_query_missing_entity_raises(storage, world_id):
    """目标实体不存在：抛 EntityNotFoundError。"""
    with pytest.raises(EntityNotFoundError):
        get_pc_background(storage, {"world_id": world_id, "entity_id": "ghost"})


def test_placeholder_values_skipped(storage, world_id):
    """占位值（无/暂无）整项跳过，不渲染。"""
    _seed_pc(storage, world_id, "pc_01", "费莉西蒂", {
        "injury_scar": "无",
        "phobias_manias": "无",
        "belief": "信念",
    })
    result = get_pc_background(storage, {"world_id": world_id})
    bg = result["backgrounds"][0]["background"]
    assert "【伤口和疤痕】" not in bg
    assert "【恐惧症和躁狂症】" not in bg
    assert "【思想与信念】信念" in bg


def test_missing_world_id_raises(storage):
    """world_id 必填。"""
    with pytest.raises(ValueError):
        get_pc_background(storage, {})


def test_query_empty_world_no_backgrounds(storage, world_id):
    """无任何 PC 时返回空列表与提示。"""
    result = get_pc_background(storage, {"world_id": world_id})
    assert result["ok"] is True
    assert result["backgrounds"] == []


async def test_runner_executes_get_pc_background(storage, world_id):
    """注册进 ToolRunner 闭环：world_id 注入后正常执行，只读不产生 state_diff。"""
    from src.agent.loop import build_default_runner

    _seed_pc(storage, world_id, "pc_01", "费莉西蒂", {"belief": "信念"})
    runner = build_default_runner(storage)
    assert "get_pc_background" in runner.names()
    result = await runner.execute(
        "get_pc_background", {}, world_id=world_id, turn_num=1
    )
    assert result["ok"] is True
    assert result["backgrounds"][0]["name"] == "费莉西蒂"
    assert result["summary"]
    assert runner.collected_diffs == []  # 只读工具无 state_diff
