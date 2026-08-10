# -*- coding: utf-8 -*-
"""
@File     :   test_opening.py
@Desc     :   Opening Agent 开场测试：前置校验（无静默降级）/ 契约提取 / Turn 0 三件套落库
             / 幂等保护 / 演播失败零残留
@Note     :   FakeLLM 模拟工具闭环（search + get_pc_background -> present_opening 交卷）；
             FakeMemory 内存后端验证 seed 记忆直写；turn 0 固化标记防二次提炼
"""

from __future__ import annotations

import pytest

from src.agent.opening import (
    build_opening_runner,
    run_opening_narration,
    run_opening_setup,
)
from src.core.exceptions import NarratorError, OpeningError
from src.memory.fake import FakeMemory


def _seed_pc(storage, world_id, entity_id="pc_01", occupation="私家侦探"):
    """在世界内创建一名带职业与背景的调查员。"""
    storage.create_entity(
        world_id, entity_id, "PC", "费莉西蒂",
        occupation=occupation,
        background={"belief": "真正的力量", "significant_person": "失踪的马瑟斯先生"},
    )


def _opening_step(
    scene_tag="阿诺兹堡 - 调查员事务所 - 雨后下午",
    narrative="雨后的清晨，委托人叩响事务所的门，递上一张名片与一份书单。",
    memories=("道格拉斯·金博尔一年前失踪未留踪迹。", "失窃的只有五本旧书。"),
):
    """构造 fake 响应：首轮返回检索类工具调用，回填后调 present_opening 交卷。"""

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {
                "text": None,
                "tool_calls": [
                    {
                        "id": "c_open",
                        "name": "present_opening",
                        "arguments": {
                            "scene_tag": scene_tag,
                            "opening_summary": "受托委托：委托人求查叔叔失踪与旧书失窃。",
                            "narrative_directive": narrative,
                            "seeded_memories": list(memories),
                        },
                    }
                ],
            }
        return {
            "text": None,
            "tool_calls": [
                {"id": "c1", "name": "search_module", "arguments": {"query": "引言"}},
                {"id": "c2", "name": "get_pc_background", "arguments": {}},
            ],
        }

    return step


# ====================================================================
# Opening Agent 专属 runner
# ====================================================================


def test_opening_runner_exposes_three_tools(storage):
    """开场 runner 只暴露 3 工具：search / get_pc_background / present_opening。"""
    runner = build_opening_runner(storage)
    assert set(runner.names()) == {
        "search_module",
        "get_pc_background",
        "present_opening",
    }


# ====================================================================
# 前置校验（无静默降级）
# ====================================================================


async def test_opening_setup_no_module_raises(storage, world_id):
    """未绑定模组：抛 OpeningError 拦截开场。"""
    storage.update_world(world_id, module_name="")  # 状态：解绑模组
    with pytest.raises(OpeningError):
        await run_opening_setup(storage, world_id)


async def test_opening_setup_no_pc_raises(storage, world_id):
    """无有效 PC 角色卡：抛 OpeningError 拦截开场。"""
    with pytest.raises(OpeningError):
        await run_opening_setup(storage, world_id)


async def test_opening_setup_missing_world_raises(storage):
    """世界不存在：抛 OpeningError。"""
    with pytest.raises(OpeningError):
        await run_opening_setup(storage, "world_no_such")


# ====================================================================
# 开场决策契约提取
# ====================================================================


async def test_opening_setup_extracts_contract(storage, world_id, fake_llm):
    """present_opening 交卷：契约四要素（报幕/大纲/手记/前情记忆）完整提取。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", _opening_step())
    setup = await run_opening_setup(storage, world_id, llm=fake_llm.call)
    assert setup.scene_tag == "阿诺兹堡 - 调查员事务所 - 雨后下午"
    assert "叩响事务所" in setup.narrative_directive
    assert setup.summary == "受托委托：委托人求查叔叔失踪与旧书失窃。"
    assert setup.seeded_memories == [
        "道格拉斯·金博尔一年前失踪未留踪迹。",
        "失窃的只有五本旧书。",
    ]
    assert setup.converged is True


# ====================================================================
# Turn 0 三件套落库
# ====================================================================


async def test_opening_narration_commits_turn0_three_part(storage, world_id, fake_llm):
    """开场演播三件套齐全：落 turn 0 + seed 记忆 + 标记已固化防二次提炼。"""
    _seed_pc(storage, world_id)
    mem = FakeMemory(storage=storage)
    fake_llm.set_response("smart", _opening_step())
    fake_llm.set_response(
        "standard",
        "【阿诺兹堡 - 调查员事务所 - 雨后下午】\n雨后的清晨，委托人叩响了事务所的门……",
    )
    opened = await run_opening_narration(storage, world_id, memory=mem, llm=fake_llm.call)
    assert opened.narration
    # ① turn 0 落库：directive 手记 + scene_tag + assistant 叙事
    turn = storage.get_turn(world_id, 0)
    assert turn is not None
    cd = turn["context_data"]
    assert cd["directive"] and "叩响事务所" in cd["directive"]
    assert cd["scene_tag"] == "阿诺兹堡 - 调查员事务所 - 雨后下午"
    assert cd["assistant"] == opened.narration
    # ② seed 记忆直写 RAG（内存后端桶内绑 turn 0）
    hits = await mem.search("道格拉斯", world_id)
    assert any("道格拉斯" in h.text for h in hits)
    # ③ 标记已固化：无未固化轮次，后台 Worker 不会对开场二次提炼
    assert storage.get_unsolidified_turns(world_id) == []


async def test_opening_narration_already_opened_raises(storage, world_id):
    """已开场（turn 0 存在）再初始化：幂等保护抛 OpeningError。"""
    _seed_pc(storage, world_id)
    storage.commit_turn(world_id, 0, state_diff={}, context_data={"directive": "已开场"})
    with pytest.raises(OpeningError):
        await run_opening_narration(storage, world_id)


# ====================================================================
# 演播失败：副作用后置，零残留可重试
# ====================================================================


class _BadNarrator:
    """演播必失败的假 Narrator，验证开场失败不残留半开场状态。"""

    async def narrate(self, directive, **kwargs):
        raise NarratorError("演播崩溃")


async def test_opening_narration_narrator_failure_no_side_effect(storage, world_id, fake_llm):
    """Narrator 演播失败：统一转 OpeningError，且未落 turn 0、未 seed 记忆。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", _opening_step())
    with pytest.raises(OpeningError):
        await run_opening_narration(
            storage, world_id, narrator=_BadNarrator(), llm=fake_llm.call,
        )
    assert storage.get_turn(world_id, 0) is None
    assert storage.get_unsolidified_turns(world_id) == []
