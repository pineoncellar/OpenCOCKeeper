# -*- coding: utf-8 -*-
"""
@File     :   test_insanity_pipeline.py
@Desc     :   疯狂链路管线测试：loop 层透传 insanity/suggested_tags 与 extra_checks 权威区、
             Narrator 渲染发作时长、Director 全链路交卷后 directive.checks 含疯狂检定链
@Note     :   全程 fake LLM 零网络；check_and_update_stats 走真实实现，注入固定 rng 保证骰序
"""

from __future__ import annotations

import pytest

from src.agent import (
    Director,
    build_default_runner,
    build_narrator_messages,
)
from src.agent.directive import PRESENT_DIRECTIVE_NAME


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)


def _seed_int_pc(storage, world_id):
    """建一个带 INT 的 PC，触发疯狂级联所需。"""
    storage.create_entity(
        world_id, "pc_01", "PC", "测试调查员",
        hp=10, hp_max=12, san=58, san_max=70,
        attributes_and_skills={"INT": 70},
        tags=[],
    )


# ====================================================================
# loop 层：透传与权威区收集
# ====================================================================

async def test_loop_passes_insanity_and_collects_extra_checks(storage, world_id):
    """SAN 损失触发疯狂：工具结果透传 insanity/suggested_tags，extra_checks 并入权威区。"""
    _seed_int_pc(storage, world_id)
    runner = build_default_runner(storage, rng=SeqRng([3, 0, 3, 4]))
    runner.reset_diffs()
    runner.reset_checks()
    out = await runner.execute(
        "check_and_update_stats",
        {"entity_id": "pc_01", "san_change": -12},
        world_id=world_id, turn_num=1,
    )
    # 状态：insanity/suggested_tags 对模型可见
    assert out["insanity"]["triggered"] is True
    assert out["suggested_tags"] == ["临时性疯狂", "状态:遍体鳞伤"]
    assert "state_diff" not in out
    # 状态：疯狂链路追加检定（智力 + 总结发作）已收集进权威区
    kinds = [c["kind"] for c in runner.collected_checks]
    assert kinds == ["int", "bout"]
    assert runner.collected_checks[0]["entity_id"] == "pc_01"


async def test_loop_insanity_collected_checks_are_check_list(storage, world_id):
    """权威区 check 条目具备 Narrator 渲染所需字段（含时长）。"""
    _seed_int_pc(storage, world_id)
    runner = build_default_runner(storage, rng=SeqRng([3, 0, 3, 4]))
    runner.reset_diffs()
    runner.reset_checks()
    await runner.execute(
        "check_and_update_stats",
        {"entity_id": "pc_01", "san_change": -12},
        world_id=world_id, turn_num=1,
    )
    bout_check = runner.collected_checks[1]
    assert bout_check["skill_or_attribute"] == "总结发作"
    assert bout_check["success_level_label"] == "抽中：遍体鳞伤"
    assert bout_check["duration_hours"] == 4


# ====================================================================
# Narrator 层：权威区渲染（含发作时长）
# ====================================================================

def test_narrator_renders_madness_duration():
    """Narrator 消息渲染：疯狂发作权威区带"持续 X 小时"，供蒙太奇时间跳跃演播。"""
    check = {
        "entity_id": "pc_01",
        "skill_or_attribute": "总结发作",
        "roll_value": 3,
        "threshold": 10,
        "success_level_label": "抽中：遍体鳞伤",
        "duration_hours": 4,
        "kind": "bout",
    }
    messages = build_narrator_messages(
        _make_directive(checks=[check]), recent_text="", checks_text=None
    )
    joined = "\n".join(
        m["content"] for m in messages if m.get("content")
    )
    assert "【检定结果权威区】" in joined
    assert "pc_01：总结发作 3/10 抽中：遍体鳞伤 （持续 4 小时）" in joined
    assert "疯狂发作演播" in messages[0]["content"]  # 演播守则已注入


def _make_directive(checks=None):
    from src.agent.directive import NarrativeDirective

    return NarrativeDirective(
        state_changes={},
        narrative_directive="### 规则裁决\n- 触发临时性疯狂（遍体鳞伤，持续 4 小时）。",
        turn_num=1,
        converged=True,
        checks=checks if checks is not None else [],
    )


# ====================================================================
# Director 全链路：交卷后 directive.checks 含疯狂检定链
# ====================================================================

async def test_director_pipeline_checks_include_madness(storage, world_id, fake_llm):
    """主 Agent 闭环：调 check_and_update_stats 触发疯狂，交卷后 directive.checks 含 INT+发作链。"""
    _seed_int_pc(storage, world_id)

    def step(messages):
        # 状态：第一轮发起大额 SAN 损失；工具回填后交卷
        if any(m["role"] == "tool" for m in messages):
            return {
                "text": None,
                "tool_calls": [
                    {"id": "c2", "name": PRESENT_DIRECTIVE_NAME,
                     "arguments": {"narrative_directive": "### 规则裁决\n- 临时疯狂发作。"}}
                ],
            }
        return {
            "text": None,
            "tool_calls": [
                {"id": "c1", "name": "check_and_update_stats",
                 "arguments": {"entity_id": "pc_01", "san_change": -12}}
            ],
        }

    fake_llm.set_response("smart", step)
    director = Director(
        storage, llm=fake_llm.call, tier="smart", temperature=0.2,
        rng=SeqRng([3, 0, 3, 4]),
    )
    directive = await director.run_turn(world_id, action="调查员直面可怖景象", turn_num=1)
    kinds = [c["kind"] for c in directive.checks]
    assert kinds == ["int", "bout"]
    assert directive.checks[1]["duration_hours"] == 4


# ====================================================================
# Phase 4：回档集成验证——疯狂轮三态（SAN/HP/Tag）可无损撤销
# ====================================================================

def test_insanity_turn_rollback_restores_all(storage, world_id):
    """疯狂轮：SAN 扣减 + 遍体鳞伤 HP 折半 + 疯狂 Tag 落库，undo 后三态完整还原。"""
    from src.tools.check_and_update_stats import check_and_update_stats as check_stats
    from src.tools.commit import apply_turn_change as commit
    from src.tools.manage_tags import manage_tags as tags_tool

    _seed_int_pc(storage, world_id)
    d1 = check_stats(
        storage,
        {"world_id": world_id, "entity_id": "pc_01", "san_change": -12},
        rng=SeqRng([3, 0, 3, 4]),
    )["state_diff"]
    d2 = tags_tool(
        storage,
        {"world_id": world_id, "entity_id": "pc_01",
         "add_tags": ["临时性疯狂", "状态:遍体鳞伤"]},
    )["state_diff"]
    commit(storage, world_id, 1, [d1, d2])

    fresh = storage.get_entity(world_id, "pc_01")
    assert fresh["san"] == 46  # 58 - 12
    assert fresh["hp"] == 5    # 10 // 2 遍体鳞伤折半
    assert "临时性疯狂" in fresh["tags"]

    storage.undo_turn(world_id, 1)
    restored = storage.get_entity(world_id, "pc_01")
    assert restored["san"] == 58
    assert restored["hp"] == 10
    assert restored["tags"] == []
