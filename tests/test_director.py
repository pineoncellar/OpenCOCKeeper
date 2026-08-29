# -*- coding: utf-8 -*-
"""
@File     :   test_director.py
@Desc     :   主 Agent（Director）回合编排测试：交卷收敛 / 文本降级 / 统一落库 / 回档 / 轮次自增
@Note     :   全程 fake LLM 零网络；state_changes 由工具执行 diff 程序合并，模型不可见
"""

from __future__ import annotations

import pytest

from src.agent import Director, NarrativeDirective
from src.core.exceptions import AgentLoopError


def _seed_pc(storage, world_id, hp=10, san=58):
    storage.update_world(
        world_id, player_ids=["pc_01"], global_recap="调查员抵达阿卡姆。"
    )
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=hp, hp_max=12, san=san, san_max=70,
        attributes_and_skills={"侦查": 60},
    )


def _step_stats_then_directive(narrative="### 规则裁决\n- 侦查成功（18/60）"):
    """首轮调用 check_and_update_stats（含检定）扣血，回填后调用 present_directive 交卷。"""

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [
                {"id": "c2", "name": "present_directive",
                 "arguments": {"narrative_directive": narrative}}]}
        return {"text": None, "tool_calls": [
            {"id": "c1", "name": "check_and_update_stats",
             "arguments": {"entity_id": "pc_01", "skill_or_attribute": "侦查", "hp_change": -3}}]}

    return step


async def test_run_turn_converges_and_persists(storage, world_id, fake_llm):
    """正常交卷：契约含程序合并的 state_changes，落库后 undo 可回档。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", _step_stats_then_directive())
    director = Director(storage, llm=fake_llm.call)
    directive = await director.run_turn(world_id, "调查员试图撬开暗门")
    assert isinstance(directive, NarrativeDirective)
    assert directive.turn_num == 1
    assert directive.converged is True
    assert directive.narrative_directive.startswith("### 规则裁决")
    # state_changes 程序权威（模型不可见）
    assert directive.state_changes["numeric_changes"] == {"pc_01.hp": -3}
    # 检定结果权威副本：掷骰值 / 成功等级 / 实体标识保留并透传 Narrator
    assert len(directive.checks) == 1
    check = directive.checks[0]
    assert check["entity_id"] == "pc_01"
    assert check["success_level"] in {"CRITICAL", "EXTREME", "HARD", "REGULAR", "FAILURE", "FUMBLE"}
    assert check["success_level_label"]
    assert isinstance(check["roll_value"], int)
    # 落库轮次：对话 + diff + 检定结果（审计/日志留存）
    turn = storage.get_turn(world_id, 1)
    assert turn["context_data"]["user"] == "调查员试图撬开暗门"
    assert turn["context_data"]["assistant"] == directive.narrative_directive
    assert turn["context_data"]["checks"] == directive.checks
    assert storage.get_entity(world_id, "pc_01")["hp"] == 7
    # 回档可逆
    storage.undo_turn(world_id, 1)
    assert storage.get_entity(world_id, "pc_01")["hp"] == 10
    assert storage.get_turn(world_id, 1) is None


def _directive_step(*, scene_notes: str = "", narrative: str = "### 规则裁决\n- 无"):
    """首轮直接 present_directive 交卷；scene_notes 非空才携带（模拟模型漏填）。"""

    def step(messages):
        args = {"narrative_directive": narrative}
        if scene_notes:
            args["scene_notes"] = scene_notes
        return {"text": None, "tool_calls": [
            {"id": "c1", "name": "present_directive", "arguments": args}]}

    return step


async def test_run_turn_writes_scene_notes(storage, world_id, fake_llm):
    """交卷携带场景手记：世界写回 scene_notes，契约带出本轮手记。"""
    _seed_pc(storage, world_id)
    notes = "酒保态度防备，隐瞒密室；暗门线索已侦破"
    fake_llm.set_response("smart", _directive_step(scene_notes=notes))
    director = Director(storage, llm=fake_llm.call)
    directive = await director.run_turn(world_id, "继续盘问酒保")
    assert directive.scene_notes == notes
    assert storage.get_world(world_id)["scene_notes"] == notes


async def test_run_turn_missing_scene_notes_keeps_old(storage, world_id, fake_llm):
    """交卷未携带 scene_notes：已有手记保持不变（缺失不清空防丢）。"""
    _seed_pc(storage, world_id)
    storage.update_world(world_id, scene_notes="旧场景手记")
    fake_llm.set_response("smart", _directive_step(scene_notes=""))
    director = Director(storage, llm=fake_llm.call)
    directive = await director.run_turn(world_id, "行动")
    assert directive.scene_notes == ""
    assert storage.get_world(world_id)["scene_notes"] == "旧场景手记"


async def test_run_turn_scene_notes_truncated(storage, world_id, fake_llm):
    """超长手记被截断到 SCENE_NOTES_MAX_CHARS，写入与契约一致。"""
    from src.agent.directive import SCENE_NOTES_MAX_CHARS

    _seed_pc(storage, world_id)
    long_notes = "字" * (SCENE_NOTES_MAX_CHARS + 100)
    fake_llm.set_response("smart", _directive_step(scene_notes=long_notes))
    director = Director(storage, llm=fake_llm.call)
    directive = await director.run_turn(world_id, "行动")
    assert len(directive.scene_notes) == SCENE_NOTES_MAX_CHARS
    assert len(storage.get_world(world_id)["scene_notes"]) == SCENE_NOTES_MAX_CHARS


async def test_run_turn_scene_notes_text_fallback_keeps_old(storage, world_id, fake_llm):
    """文本降级路径无手记来源：不写回，已有手记保持不变。"""
    _seed_pc(storage, world_id)
    storage.update_world(world_id, scene_notes="旧场景手记")
    fake_llm.set_response("smart", lambda messages: "降级叙事文本")
    director = Director(storage, llm=fake_llm.call)
    directive = await director.run_turn(world_id, "确认伤口")
    assert directive.scene_notes == ""
    assert storage.get_world(world_id)["scene_notes"] == "旧场景手记"


async def test_run_turn_text_fallback(storage, world_id, fake_llm):
    """模型直接文本收敛（未调收尾工具）：以最终文本为导演手记降级，converged=False。"""
    _seed_pc(storage, world_id)

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return "降级叙事文本"
        return {"text": None, "tool_calls": [
            {"id": "c1", "name": "manage_tags",
             "arguments": {"entity_id": "pc_01", "add_tags": ["流血"]}}]}

    fake_llm.set_response("smart", step)
    director = Director(storage, llm=fake_llm.call)
    directive = await director.run_turn(world_id, "确认伤口")
    assert directive.converged is False
    assert directive.narrative_directive == "降级叙事文本"
    assert "流血" in directive.state_changes["tags"]["pc_01"]["added"]
    assert storage.get_entity(world_id, "pc_01")["tags"] == ["流血"]


async def test_run_turn_empty_diff_persists_dialogue(storage, world_id, fake_llm):
    """空 diff 纯叙事轮：仍落库一对话轮，state_changes 为空结构。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", lambda messages: "观察四周，并无异样。")
    director = Director(storage, llm=fake_llm.call)
    directive = await director.run_turn(world_id, "观察四周")
    assert directive.state_changes == {
        "numeric_changes": {}, "tags": {}, "inventory": {},
    }
    turn = storage.get_turn(world_id, 1)
    assert turn is not None
    assert turn["context_data"]["assistant"] == "观察四周，并无异样。"


async def test_run_turn_increments_turn_num(storage, world_id, fake_llm):
    """连续两轮：turn_num 单调自增（1 -> 2）。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", lambda messages: "回应")
    director = Director(storage, llm=fake_llm.call)
    d1 = await director.run_turn(world_id, "第一轮行动")
    d2 = await director.run_turn(world_id, "第二轮行动")
    assert d1.turn_num == 1 and d2.turn_num == 2


async def test_run_turn_llm_failure_raises(storage, world_id, fake_llm):
    """LLM 请求失败：抛 AgentLoopError，回合中断。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", RuntimeError("boom"))
    director = Director(storage, llm=fake_llm.call)
    with pytest.raises(AgentLoopError):
        await director.run_turn(world_id, "行动")
