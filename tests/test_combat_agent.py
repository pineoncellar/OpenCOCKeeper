# -*- coding: utf-8 -*-
"""
@File     :   test_combat_agent.py
@Desc     :   Combat Agent 与战斗管线集成测试：软状态生命周期、战斗轮编排、pipeline 分流
@Note     :   覆盖决策要点——start_combat 先攻排序与临时标记、advance 跳过丧失行动者与跨轮、
             end_combat 清理临时标记保留伤情、FakeLLM 多步模拟战斗交卷、全灭强制脱战、
             pending_reaction 挂起与续接、run_narrated_turn 战斗分流不触主 Agent
"""

import pytest

from src.agent.combat import (
    advance_battlefield,
    build_combat_messages,
    current_actor,
    end_combat,
    read_combat,
    start_combat,
)
from src.agent.pipeline import run_narrated_turn


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)


def _seed(world_id, storage):
    """建一 PC 与两 NPC 的战斗测试舞台。"""
    pc_skills = {"DEX": 60, "斗殴": 70, "闪避": 60, "手枪": 50, "STR": 50, "CON": 50}
    npc_skills = {"DEX": 40, "斗殴": 50, "闪避": 40, "手枪": 40, "STR": 50, "CON": 50}
    storage.create_entity(
        world_id, "player_01", "PC", "调查员甲",
        hp=12, hp_max=12, attributes_and_skills=pc_skills,
    )
    storage.create_entity(
        world_id, "npc_01", "NPC", "教徒甲",
        hp=12, hp_max=12, attributes_and_skills=npc_skills,
    )
    storage.create_entity(
        world_id, "npc_02", "NPC", "教徒乙",
        hp=12, hp_max=12, attributes_and_skills={**npc_skills, "斗殴": 65},
    )
    return storage.get_entity(world_id, "player_01"), storage.get_entity(world_id, "npc_01")


def _combat_resolve_payload(attacker, target, defense="none"):
    """构造 FakeLLM 的 combat_resolve 工具调用载荷。"""
    return {
        "id": "c_resolve",
        "name": "combat_resolve",
        "arguments": {
            "action": "strike",
            "attacker_id": attacker,
            "target_id": target,
            "attack_skill": "斗殴",
            "defense": defense,
            "damage_expression": "1D6",
        },
    }


def _present_payload(narrative="战斗裁决完成，交卷。", in_combat=True, pending=None):
    """构造 FakeLLM 的 present_combat 收尾调用载荷。"""
    args = {"narrative_directive": narrative, "in_combat": in_combat}
    if pending:
        args["pending_reaction"] = pending
    return {"id": "c_present", "name": "present_combat", "arguments": args}


# ============================================
# 软状态生命周期
# ============================================

def test_start_combat_order_and_battle_tag(storage, world_id):
    _seed(world_id, storage)
    cc = start_combat(storage, world_id, ["player_01", "npc_01", "npc_02"])
    assert cc["in_combat"] is True and cc["round_num"] == 1
    order = [e["entity_id"] for e in cc["turn_order"]]
    # DEX 60 的玩家居首；两 NPC DEX 相同按格斗定序（65 的 npc_02 在前）
    assert order == ["player_01", "npc_02", "npc_01"]
    for eid in ("player_01", "npc_01", "npc_02"):
        assert "战斗中" in storage.get_entity(world_id, eid)["tags"]


def test_start_combat_ready_gun_bonus(storage, world_id):
    _seed(world_id, storage)
    cc = start_combat(
        storage, world_id, ["player_01", "npc_01"], ready_gun_ids=["npc_01"]
    )
    order = [e["entity_id"] for e in cc["turn_order"]]
    # 敏捷 40 的教徒拔枪待击（生效 90）压过敏捷 60 的玩家
    assert order == ["npc_01", "player_01"]


def test_current_actor_peeks_without_stepping(storage, world_id):
    """current_actor 只取当前行动者，不步进游标。"""
    _seed(world_id, storage)
    cc = start_combat(storage, world_id, ["player_01", "npc_01"])
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    # 首行动者 = 玩家；连续取不推进
    assert current_actor(cc, entities) == "player_01"
    assert current_actor(cc, entities) == "player_01"
    assert current_actor(cc, entities) == "player_01"
    assert cc["cursor"] == 0


def test_advance_skips_incapacitated_and_advances_round(storage, world_id):
    """advance_battlefield 步进到下一顺位可行动者，跳过昏迷/死亡，末位回卷大轮。"""
    _seed(world_id, storage)
    cc = start_combat(storage, world_id, ["player_01", "npc_01"])
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    # 首次步进：从玩家(0)到下一顺位教徒(1)
    assert advance_battlefield(cc, entities) == "npc_01"
    assert cc["cursor"] == 1
    # 教徒失去行动能力（HP 归零），步进跳过它并回到玩家，跨末位回卷大轮
    storage.update_entity(world_id, "npc_01", hp=0)
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    cc["outnumbered"] = {"player_01": 2}
    assert advance_battlefield(cc, entities) == "player_01"
    assert cc["round_num"] == 2
    assert cc["outnumbered"] == {}
    assert cc["cursor"] == 0


def test_end_combat_clears_temp_tag_keeps_wound(storage, world_id):
    _seed(world_id, storage)
    storage.update_entity(world_id, "npc_01", tags=["战斗中", "重伤"])
    start_combat(storage, world_id, ["player_01", "npc_01"])
    end_combat(storage, world_id)
    assert read_combat(storage, world_id)["in_combat"] is False
    # 临时标记卸下、持久伤情保留
    assert "战斗中" not in storage.get_entity(world_id, "npc_01")["tags"]
    assert "重伤" in storage.get_entity(world_id, "npc_01")["tags"]


# ============================================
# 战斗轮编排（FakeLLM 多步）
# ============================================

async def test_run_combat_turn_strike_and_commit(storage, world_id, fake_llm):
    _seed(world_id, storage)
    cc = start_combat(storage, world_id, ["player_01", "npc_01"])

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [_present_payload()]}
        return {"text": None, "tool_calls": [
            _combat_resolve_payload("player_01", "npc_01")]}

    fake_llm.set_response("smart", step)
    from src.agent.combat import run_combat_turn
    ct = await run_combat_turn(
        storage, world_id, "我扑向教徒挥拳", rng=SeqRng([5, 2, 4]),
    )
    # 攻击 52 REGULAR ≤70 命中，伤害 4 → 教徒 12->8
    assert ct.directive.turn_num == 1
    turn = storage.get_turn(world_id, 1)
    assert turn["state_diff"]["numeric_changes"] == {"npc_01.hp": -4}
    cc2 = storage.get_combat_runtime(world_id)
    assert cc2["in_combat"] is True and cc2["pending_reaction"] is None


async def test_run_combat_turn_force_disengage_when_side_down(storage, world_id, fake_llm):
    """一方尽数倒下：不调用 LLM，强制脱战并落脱战轮。"""
    _seed(world_id, storage)
    storage.update_entity(world_id, "npc_01", hp=0)
    cc = start_combat(storage, world_id, ["player_01", "npc_01"])
    assert cc["in_combat"] is True

    # 状态：战斗裁决 LLM 设成抛错——若强制脱战误调 LLM 则测试失败；
    # Narrator（standard 档）不受影响，仍走默认 fake 演播文本
    fake_llm.set_response("smart", RuntimeError("不应调用 LLM"))
    from src.agent.combat import run_combat_turn
    ct = await run_combat_turn(storage, world_id, "教徒倒下")
    assert ct.narration
    assert not storage.get_combat_runtime(world_id).get("in_combat")
    assert "战斗中" not in storage.get_entity(world_id, "npc_01")["tags"]


async def test_run_combat_turn_writes_pending_reaction(storage, world_id, fake_llm):
    """NPC 命中玩家：Combat Agent 交卷写入 pending_reaction 等待玩家声明防御。"""
    _seed(world_id, storage)
    start_combat(storage, world_id, ["player_01", "npc_01"])

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            pending = {
                "attacker_id": "npc_01", "target_id": "player_01",
                "attack_skill": "斗殴", "damage_expression": "1D6",
                "threat": "教徒挥爪扑来",
            }
            return {"text": None, "tool_calls": [_present_payload(pending=pending)]}
        return {"text": None, "tool_calls": [
            _combat_resolve_payload("npc_01", "player_01")]}

    fake_llm.set_response("smart", step)
    from src.agent.combat import run_combat_turn
    await run_combat_turn(storage, world_id, "教徒扑向你", rng=SeqRng([5, 2, 4]))
    cc = storage.get_combat_runtime(world_id)
    assert cc["in_combat"] is True
    assert cc["pending_reaction"]["target_id"] == "player_01"
    assert cc["pending_reaction"]["attack_skill"] == "斗殴"


def test_combat_messages_render_pending_reaction(storage, world_id):
    _seed(world_id, storage)
    cc = start_combat(storage, world_id, ["player_01", "npc_01"])
    cc["pending_reaction"] = {
        "attacker_id": "npc_01", "target_id": "player_01",
        "attack_skill": "斗殴", "damage_expression": "1D6",
    }
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    msgs = build_combat_messages(storage, world_id, cc, "我闪避", "player_01", entities)
    joined = "\n".join(m["content"] for m in msgs)
    assert "【挂起反应】" in joined
    assert "等待 player_01 声明防御" in joined


# ============================================
# pipeline 战斗分流
# ============================================

class SpyDirector:
    """若被调用即抛错的 Director 间谍——验证战斗分流不触主 Agent。"""

    async def run_turn(self, *args, **kwargs):
        raise AssertionError("战斗状态不应进入 Director")


async def test_pipeline_routes_combat_away_from_director(storage, world_id, fake_llm):
    _seed(world_id, storage)
    start_combat(storage, world_id, ["player_01", "npc_01"])

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [_present_payload()]}
        return {"text": None, "tool_calls": [
            _combat_resolve_payload("player_01", "npc_01")]}

    fake_llm.set_response("smart", step)
    result = await run_narrated_turn(
        storage, world_id, "我攻击教徒",
        director=SpyDirector(),
        rng=SeqRng([5, 2, 4]),
    )
    assert result.narration
    # 战斗轮已落库，且未触 Director
    assert storage.get_turn(world_id, 1) is not None


async def test_pipeline_normal_path_unaffected(storage, world_id, fake_llm):
    """非战斗世界：run_narrated_turn 走常规 Director，不触发战斗异常。"""
    from src.agent.director import Director
    fake_llm.set_response(
        "smart",
        lambda messages: {"text": None, "tool_calls": [
            {"id": "c", "name": "present_directive",
             "arguments": {"narrative_directive": "### 规则裁决\n- 无事发生"}}]},
    )
    result = await run_narrated_turn(
        storage, world_id, "调查员环顾四周", director=Director(storage),
    )
    assert result.narration
    # 非战斗世界 combat_runtime 保持空软状态
    assert not storage.get_combat_runtime(world_id).get("in_combat")