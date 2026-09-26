# -*- coding: utf-8 -*-
"""
@File     :   test_combat_start_tool.py
@Desc     :   主 Agent start_combat 工具测试：补注册未登记怪物、建立战斗软状态、
             战斗开场公告（分界线 + 先攻顺序）生成与 Narrator 演播兜底、pipeline 后续轮分流
@Note     :   全程 fake LLM 零网络；验证"主 Agent 调 start_combat → combat_runtime 建立 →
             Narrator 输出战斗开始分界线 → 下一轮自动进 Combat Agent"
"""

from __future__ import annotations

import pytest

from src.agent import Director, NarrativeDirective
from src.agent.narrator import Narrator, build_narrator_messages
from src.agent.pipeline import run_narrated_turn

BANNER = "————战斗开始————"


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)


def _seed_player(storage, world_id):
    """建一个 DEX 65 的 PC，模拟 ttk 世界（怪物未登记）。"""
    storage.create_entity(
        world_id, "player_01", "PC", "洪亭",
        hp=11, hp_max=11,
        attributes_and_skills={"DEX": 65, "格斗": 80, "闪避": 60, "手枪": 50},
    )


def _start_combat_payload(monster_id="缝合胚蜕", with_spec=True, ready=None):
    args = {"participant_ids": ["player_01", monster_id]}
    if with_spec:
        args["new_entities"] = [
            {"entity_id": monster_id, "name": monster_id, "hp": 13, "dex": 40, "fight_skill": 45}
        ]
    if ready:
        args["ready_gun_ids"] = ready
    return {"id": "sc", "name": "start_combat", "arguments": args}


def _present_directive_payload(narrative="### 规则裁决\n- 战斗建立，先攻由玩家先行。"):
    return {"id": "pd", "name": "present_directive", "arguments": {"narrative_directive": narrative}}


def _combat_resolve_payload(attacker="player_01", target="缝合胚蜕"):
    return {
        "id": "cr", "name": "combat_resolve",
        "arguments": {
            "action": "strike", "attacker_id": attacker, "target_id": target,
            "attack_skill": "格斗", "defense": "none", "damage_expression": "1D8",
        },
    }


def _present_combat_payload(narrative="战斗裁决完成，交卷。", in_combat=True):
    return {"id": "cp", "name": "present_combat",
            "arguments": {"narrative_directive": narrative, "in_combat": in_combat}}


def _step_start_then_directive():
    """首轮调 start_combat（补注册怪物），回填后 present_directive 交卷。"""

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [_present_directive_payload()]}
        return {"text": None, "tool_calls": [_start_combat_payload()]}

    return step


# ============================================
# start_combat 工具：补注册 + 建立战斗 + 开场公告
# ============================================

async def test_start_combat_registers_monster_and_establishes_combat(storage, world_id, fake_llm):
    """主 Agent 调 start_combat：未登记怪物被补注册为 NPC，战斗软状态建立，生成开场公告。"""
    _seed_player(storage, world_id)
    fake_llm.set_response("smart", _step_start_then_directive())
    director = Director(storage, llm=fake_llm.call)
    directive = await director.run_turn(world_id, "我拔剑冲向怪物")

    # 怪物补注册为 NPC 实体
    monster = storage.get_entity(world_id, "缝合胚蜕")
    assert monster is not None
    assert monster["type"] == "NPC"
    assert monster["hp"] == 13
    assert monster["attributes_and_skills"]["DEX"] == 40

    # 战斗软状态建立（后续轮 pipeline 将分流 Combat Agent）
    cc = storage.get_combat_runtime(world_id)
    assert cc["in_combat"] is True
    order = [e["entity_id"] for e in cc["turn_order"]]
    assert order == ["player_01", "缝合胚蜕"]  # DEX 65 先于 DEX 40

    # 开场公告：分界线 + 先攻顺序（以实体名渲染）
    assert BANNER in directive.combat_intro
    assert "先攻" in directive.combat_intro
    assert "洪亭" in directive.combat_intro and "缝合胚蜕" in directive.combat_intro


async def test_start_combat_missing_spec_returns_error(storage, world_id, fake_llm):
    """参战者未注册且未提供面板：工具返回错误镜像，不建立战斗，怪物不落库。"""
    _seed_player(storage, world_id)

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [_present_directive_payload("### 规则裁决\n- 未建立战斗。")]}
        return {"text": None, "tool_calls": [_start_combat_payload(with_spec=False)]}

    fake_llm.set_response("smart", step)
    director = Director(storage, llm=fake_llm.call)
    directive = await director.run_turn(world_id, "我拔剑冲向怪物")

    assert storage.get_combat_runtime(world_id).get("in_combat") is not True
    assert storage.get_entity(world_id, "缝合胚蜕") is None
    assert directive.combat_intro == ""


async def test_start_combat_ready_gun_bonus(storage, world_id, fake_llm):
    """拔枪待击者 DEX+50 生效：敏捷更低的怪物可先行动。"""
    _seed_player(storage, world_id)

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [_present_directive_payload()]}
        return {"text": None, "tool_calls": [
            _start_combat_payload(ready=["缝合胚蜕"])]}

    fake_llm.set_response("smart", step)
    director = Director(storage, llm=fake_llm.call)
    await director.run_turn(world_id, "怪物率先扑来")
    cc = storage.get_combat_runtime(world_id)
    # 怪物拔枪待击生效 90 压过敏捷 65 的玩家
    assert [e["entity_id"] for e in cc["turn_order"]] == ["缝合胚蜕", "player_01"]


# ============================================
# 战斗横幅隔离原则：纯程序拼接，不进 Narrator 上下文
# ============================================

def test_narrator_prompt_excludes_combat_banner():
    """隔离原则：开战公告绝不进入 Narrator 上下文（防排版文本污染演播格式）。"""
    d = NarrativeDirective(
        state_changes={}, narrative_directive="战斗爆发。", turn_num=1,
        combat_intro=f"{BANNER}\n\n【先攻顺序】\n1. 洪亭（DEX 65）",
    )
    msgs = build_narrator_messages(d, combat=True)
    joined = "\n".join(m["content"] for m in msgs)
    assert "【战斗开场公告】" not in joined
    assert BANNER not in joined


async def test_narrator_does_not_emit_banner(storage, world_id, fake_llm):
    """Narrator 只产出文学演播，分界线与先攻表不由其输出（交程序横幅）。"""
    d = NarrativeDirective(
        state_changes={}, narrative_directive="战斗爆发。", turn_num=1,
        combat_intro=f"{BANNER}\n\n【先攻顺序】\n1. 洪亭（DEX 65）",
    )
    fake_llm.set_response("standard", lambda messages: "怪物嘶吼着扑来，利爪撕碎空气。")
    narrator = Narrator(llm=fake_llm.call)
    text = await narrator.narrate(d, world_id=world_id, combat=True)
    assert BANNER not in text
    assert "先攻顺序" not in text


async def test_pipeline_prepends_combat_intro_on_start_turn(storage, world_id, fake_llm):
    """开战轮：主 Agent 调 start_combat 后，开战公告由 pipeline 程序拼接在叙事之前。"""
    _seed_player(storage, world_id)
    fake_llm.set_response("smart", _step_start_then_directive())
    fake_llm.set_response("standard", lambda messages: "铁门洞开，怪物直立而起。")
    result = await run_narrated_turn(storage, world_id, "我推开铁门")
    assert BANNER in result.narration
    assert "先攻顺序" in result.narration
    # 状态：横幅在叙事之前，程序固定位置不依赖模型配合
    assert result.narration.index(BANNER) < result.narration.index("铁门洞开")


# ============================================
# pipeline：start_combat 后下一轮自动分流 Combat Agent
# ============================================

class SpyDirector:
    """若被调用即抛错的 Director 间谍——验证战斗分流不触主 Agent。"""

    async def run_turn(self, *args, **kwargs):
        raise AssertionError("战斗状态不应进入 Director")


async def test_pipeline_enters_combat_after_start_combat(storage, world_id, fake_llm):
    """第一轮主 Agent 调 start_combat，第二轮 pipeline 自动分流 Combat Agent，不触 Director。"""
    _seed_player(storage, world_id)
    # 第一轮：主 Agent 调 start_combat（补注册怪物 + 建立战斗）
    fake_llm.set_response("smart", _step_start_then_directive())
    director = Director(storage, llm=fake_llm.call)
    directive = await director.run_turn(world_id, "我拔剑冲向怪物")
    assert BANNER in directive.combat_intro
    assert storage.get_combat_runtime(world_id)["in_combat"] is True

    # 第二轮：pipeline 分流进 Combat Agent（smart 裁决 + standard 演播）
    def combat_step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [_present_combat_payload()]}
        return {"text": None, "tool_calls": [_combat_resolve_payload()]}

    fake_llm.set_response("smart", combat_step)
    fake_llm.set_response("standard", lambda messages: "剑锋与触须在冷库中绞杀。")
    result = await run_narrated_turn(
        storage, world_id, "我挥剑劈向怪物",
        director=SpyDirector(),
        rng=SeqRng([50, 50, 50, 50, 50, 50, 50, 50]),
    )
    assert result.narration
    assert storage.get_turn(world_id, 2) is not None
