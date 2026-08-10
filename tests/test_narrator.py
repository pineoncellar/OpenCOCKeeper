# -*- coding: utf-8 -*-
"""
@File     :   test_narrator.py
@Desc     :   润色 Agent（Narrator）与串行管线测试：契约翻译 / checks 透传 / 近程历史注入
             / LLM 失败降级语义 / run_narrated_turn 全链路落库与回档
@Note     :   全程 fake LLM 零网络；Narrator 无状态——不持 storage，输入输出纯转换；
             pipeline 串联 Director(裁决) → Narrator(演播) → 玩家视角叙事落库
"""

from __future__ import annotations

import pytest

from src.agent import (
    Director,
    Narrator,
    NarratedTurn,
    NarrativeDirective,
    build_narrator_messages,
    run_narrated_turn,
)
from src.core.exceptions import NarratorError

_HANDOFF = "### 规则裁决\n- 侦查成功（18/60），SAN -1，挂载 Tag [手臂流血]。"
_CHECK = {
    "entity_id": "pc_01",
    "skill_or_attribute": "侦查",
    "roll_value": 18,
    "threshold": 60,
    "success_level": "REGULAR",
    "success_level_label": "常规成功",
    "is_success": True,
    "tens_rolls": [10],
    "bonus_penalty_dice": 0,
}


def _make_directive(checks=None, handoff=_HANDOFF):
    return NarrativeDirective(
        state_changes={"numeric_changes": {"pc_01.san": -1}, "tags": {}, "inventory": {}},
        narrative_directive=handoff,
        turn_num=1,
        converged=True,
        checks=checks if checks is not None else [],
    )


def _seed_pc(storage, world_id, hp=10, san=58):
    storage.update_world(
        world_id, player_ids=["pc_01"], global_recap="调查员抵达阿卡姆。"
    )
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=hp, hp_max=12, san=san, san_max=70,
        attributes_and_skills={"侦查": 60},
    )


def _step_stats_then_directive(handoff=_HANDOFF):
    """主 Agent 模拟：先检定扣血，回填后 present_directive 交卷。"""
    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [
                {"id": "c2", "name": "present_directive",
                 "arguments": {"narrative_directive": handoff}}]}
        return {"text": None, "tool_calls": [
            {"id": "c1", "name": "check_and_update_stats",
             "arguments": {"entity_id": "pc_01", "skill_or_attribute": "侦查",
                           "san_change": -1}}]}

    return step


# ====================================================================
# Narrator 单元测试
# ====================================================================


async def test_narrate_translates_directive(fake_llm):
    """契约翻译：返回叙事文本，消息含手记与检定权威区，无 tools 参数。"""
    fake_llm.set_response("standard", "昏黄的灯光下，你闻到一股挥发性颜料味……")
    narrator = Narrator(llm=fake_llm.call)
    text = await narrator.narrate(_make_directive(checks=[_CHECK]))
    assert "颜料味" in text
    # 状态：Narrator 调用 tier 为 standard，且不启用 Function Calling
    std_calls = [c for c in fake_llm.calls if c["tier"] == "standard"]
    assert len(std_calls) == 1
    assert std_calls[0]["kwargs"].get("tools") is None
    joined = "\n".join(
        m["content"] for m in std_calls[0]["messages"] if m.get("content")
    )
    assert _HANDOFF in joined          # 手记透传
    assert "pc_01：侦查 18/60 常规成功" in joined  # 检定权威区渲染


async def test_narrate_injects_recent_and_action(fake_llm):
    """近程历史与本轮行动注入：渲染进 user 消息，供 L3 自洽锚点。"""
    fake_llm.set_response("standard", "叙事。")
    narrator = Narrator(llm=fake_llm.call)
    recent = [
        {"turn_num": 1, "context_data": {"user": "调查员敲了敲门", "assistant": "无人应答。"}},
    ]
    await narrator.narrate(_make_directive(), recent=recent, action="调查员撬锁")
    call = [c for c in fake_llm.calls if c["tier"] == "standard"][-1]
    joined = "\n".join(m["content"] for m in call["messages"] if m.get("content"))
    assert "调查员敲了敲门" in joined
    assert "无人应答。" in joined
    assert "调查员撬锁" in joined


async def test_narrate_llm_failure_raises(fake_llm):
    """LLM 请求失败：抛 NarratorError，由上层管线决定降级。"""
    fake_llm.set_response("standard", RuntimeError("boom"))
    narrator = Narrator(llm=fake_llm.call)
    with pytest.raises(NarratorError):
        await narrator.narrate(_make_directive())


async def test_narrate_empty_text_raises(fake_llm):
    """LLM 返回空文本：抛 NarratorError，避免向玩家输出空串。"""
    fake_llm.set_response("standard", "   ")
    narrator = Narrator(llm=fake_llm.call)
    with pytest.raises(NarratorError):
        await narrator.narrate(_make_directive())


def test_build_narrator_messages_structure():
    """消息结构：system 含演播契约要点，user 含四个区块标题。"""
    messages = build_narrator_messages(
        _make_directive(checks=[_CHECK]),
        recent=[{"turn_num": 1, "context_data": {"user": "u", "assistant": "a"}}],
        action="行动",
    )
    assert messages[0]["role"] == "system"
    assert "绝对忠实大纲" in messages[0]["content"]
    assert "检定结果透明化" in messages[0]["content"]
    assert "检定名称：掷骰值/阈值 成功等级标签" in messages[0]["content"]
    user = messages[1]["content"]
    assert "【叙事决策大纲】" in user
    assert "【检定结果权威区】" in user
    assert "【近程对话历史】" in user
    assert "【本轮玩家行动】" in user


# ====================================================================
# 串行管线测试（Director → Narrator → 落库）
# ====================================================================


async def test_run_narrated_turn_pipeline(storage, world_id, fake_llm):
    """全链路：裁决落库 → 演播 → assistant 覆盖为叙事、手记转存 directive、回档可逆。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", _step_stats_then_directive())
    fake_llm.set_response("standard", "[阿卡姆旅馆 - 一楼大厅 - 深夜] 你推门而入……")
    director = Director(storage, llm=fake_llm.call, tier="smart")
    narrator = Narrator(llm=fake_llm.call)
    turn = await run_narrated_turn(
        storage, world_id, "调查员推门进入旅馆大厅",
        director=director, narrator=narrator,
    )
    assert isinstance(turn, NarratedTurn)
    assert turn.directive.turn_num == 1
    assert turn.directive.converged is True
    assert turn.directive.state_changes["numeric_changes"] == {"pc_01.san": -1}
    assert turn.directive.checks[0]["entity_id"] == "pc_01"
    assert "大厅" in turn.narration
    # 状态：落库 assistant=玩家视角叙事，directive=权威手记，checks 保留
    rec = storage.get_turn(world_id, 1)
    assert rec["context_data"]["assistant"] == turn.narration
    assert rec["context_data"]["directive"] == _HANDOFF
    assert rec["context_data"]["checks"] == turn.directive.checks
    assert storage.get_entity(world_id, "pc_01")["san"] == 57
    # 状态：Narrator 收到的近程历史剔除本轮（不含 turn 1）
    std_call = [c for c in fake_llm.calls if c["tier"] == "standard"][-1]
    joined = "\n".join(
        m["content"] for m in std_call["messages"] if m.get("content")
    )
    assert "第 1 轮" not in joined
    # 状态：回档仍可逆（叙事写入不影响 state_diff 撤销）
    storage.undo_turn(world_id, 1)
    assert storage.get_entity(world_id, "pc_01")["san"] == 58
    assert storage.get_turn(world_id, 1) is None


async def test_run_narrated_turn_narrator_failure_keeps_state(storage, world_id, fake_llm):
    """Narrator 失败：抛 NarratorError，但物理状态与手记已落库，可降级对外输出。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", _step_stats_then_directive())
    fake_llm.set_response("standard", RuntimeError("narrator down"))
    director = Director(storage, llm=fake_llm.call, tier="smart")
    narrator = Narrator(llm=fake_llm.call)
    with pytest.raises(NarratorError):
        await run_narrated_turn(
            storage, world_id, "调查员推门进入旅馆大厅",
            director=director, narrator=narrator,
        )
    # 状态：裁决与手记已落库（assistant 仍为手记），物理状态可回档
    rec = storage.get_turn(world_id, 1)
    assert rec is not None
    assert rec["context_data"]["assistant"] == _HANDOFF
    assert storage.get_entity(world_id, "pc_01")["san"] == 57
    storage.undo_turn(world_id, 1)
    assert storage.get_entity(world_id, "pc_01")["san"] == 58
