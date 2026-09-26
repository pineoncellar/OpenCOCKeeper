# -*- coding: utf-8 -*-
"""
@File     :   test_combat_suspend.py
@Desc     :   挂起/恢复（Suspend & Resume）与游标步进测试：NPC 攻击只投检定不扣血、
             恢复轮沿用攻击检定、挂起停留游标、恢复后步进、全轮回卷递增
@Note     :   全程 fake LLM 零网络；骰序注入 SeqRng（d100 检定消耗十位+个位两值）
"""

from __future__ import annotations

import pytest

from src.agent.combat import (
    advance_battlefield,
    build_combat_runner,
    current_actor,
    run_combat_turn,
    start_combat,
)
from src.tools.combat_resolve import combat_resolve


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)


def _seed(world_id, storage):
    """建一 PC（DEX60 斗殴70）与两 NPC（DEX40 斗殴50/65）的战斗舞台。"""
    pc_skills = {"DEX": 60, "斗殴": 70, "闪避": 60, "手枪": 50, "CON": 50}
    npc_skills = {"DEX": 40, "斗殴": 50, "闪避": 40, "手枪": 40, "CON": 50}
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


def _present_payload(narrative="战斗裁决完成，交卷。", in_combat=True):
    return {"id": "c_present", "name": "present_combat",
            "arguments": {"narrative_directive": narrative, "in_combat": in_combat}}


# ============================================
# 游标步进：全轮回卷递增
# ============================================

def test_cursor_advances_across_full_round(storage, world_id):
    _seed(world_id, storage)
    cc = start_combat(storage, world_id, ["player_01", "npc_01", "npc_02"])
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    seq = []
    for _ in range(7):
        seq.append(advance_battlefield(cc, entities))
    # 顺位 player_01 → npc_02(斗殴65 先于 npc_01) → npc_01 → 回卷大轮
    assert seq == [
        "npc_02", "npc_01", "player_01",
        "npc_02", "npc_01", "player_01", "npc_02",
    ]
    assert cc["round_num"] == 3


def test_current_actor_peeks_current_position(storage, world_id):
    _seed(world_id, storage)
    cc = start_combat(storage, world_id, ["player_01", "npc_01"])
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    assert current_actor(cc, entities) == "player_01"
    assert cc["cursor"] == 0


# ============================================
# combat_resolve：suspend 挂起 + 恢复复用攻击检定
# ============================================

def test_suspend_registers_intent_without_rolling(storage, world_id):
    """suspend 只登记攻击意图：不投检定（空骰序为证）、不扣 HP、不判伤害。"""
    _seed(world_id, storage)
    # 状态：骰序给空——挂起若误投检定会立即因骰序耗尽而失败
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "suspend",
        "attacker_id": "npc_01", "target_id": "player_01",
        "attack_skill": "斗殴", "damage_expression": "1D6",
    }, rng=SeqRng([]))
    assert r["ok"] is True
    assert r["check"] is None  # 挂起不投攻击检定
    assert "attack_roll" not in r["suspend"]
    assert r["suspend"]["attacker_id"] == "npc_01"
    assert r["suspend"]["target_id"] == "player_01"
    assert r["suspend"]["damage_expression"] == "1D6"
    # 挂起不产物理 diff，玩家 HP 原封不动
    assert r["state_diff"]["numeric_changes"] == {}
    assert storage.get_entity(world_id, "player_01")["hp"] == 12


def test_resume_rolls_both_sides_together(storage, world_id):
    """恢复轮双方一起掷骰：攻击与防御检定在同一笔 strike 内投出（不预投、不沿用）。"""
    _seed(world_id, storage)
    sus = combat_resolve(storage, {
        "world_id": world_id, "action": "suspend",
        "attacker_id": "npc_01", "target_id": "player_01",
        "attack_skill": "斗殴", "damage_expression": "1D6",
    }, rng=SeqRng([]))
    pend = sus["suspend"]
    # 攻击 42/50 常规成功（4,2），防御 84/60 失败（8,4），伤害 1D6=5
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "strike",
        "attacker_id": pend["attacker_id"], "target_id": pend["target_id"],
        "attack_skill": pend["attack_skill"], "damage_expression": pend["damage_expression"],
        "defense": "dodge",
    }, rng=SeqRng([4, 2, 8, 4, 5]))
    assert r["check"]["roll_value"] == 42  # 攻击检定本轮才投出
    assert r["outcome"]["defense"] == "dodge"
    assert r["outcome"]["attacker_hits"] is True
    assert r["damage"]["total"] == 5
    assert r["state_diff"]["numeric_changes"] == {"player_01.hp": -5}
    # 状态：combat_resolve 纯计算不落库，实体 HP 仍为原值（落库由协调器执行）
    assert storage.get_entity(world_id, "player_01")["hp"] == 12


def test_attack_miss_still_reports_defense_check(storage, world_id):
    """攻击落空时防御检定仍如实呈现——玩家已声明的闪避不会因攻击失败而消失。"""
    _seed(world_id, storage)
    # 攻击 95/50 失败（9,5），防御 84/60 失败（8,4）
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "strike",
        "attacker_id": "npc_01", "target_id": "player_01",
        "attack_skill": "斗殴", "damage_expression": "1D6",
        "defense": "dodge",
    }, rng=SeqRng([9, 5, 8, 4]))
    assert r["outcome"]["attacker_hits"] is False
    assert r["check"]["roll_value"] == 95
    # 防御检定进权威副本且写进摘要
    labels = [c.get("skill_or_attribute") for c in r["extra_checks"]]
    assert "闪避" in labels
    assert "闪避检定" in r["summary_for_agent"]
    assert r["state_diff"]["numeric_changes"] == {}


# ============================================
# run_combat_turn：挂起轮与恢复轮
# ============================================

async def test_run_combat_turn_suspends_npc_attack(storage, world_id, fake_llm):
    """NPC 先攻攻击玩家：只投攻击检定并挂起，游标停留原地，玩家不扣血。"""
    _seed(world_id, storage)
    # 状态：教徒拔枪待击（DEX+50=90）压过敏捷 60 的玩家先动
    cc = start_combat(
        storage, world_id, ["player_01", "npc_01"], ready_gun_ids=["npc_01"]
    )
    assert [e["entity_id"] for e in cc["turn_order"]] == ["npc_01", "player_01"]

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [
                _present_payload("教徒扑来，等你应对。")]}
        return {"text": None, "tool_calls": [{
            "id": "c1", "name": "combat_resolve",
            "arguments": {"action": "suspend", "attacker_id": "npc_01",
                          "target_id": "player_01", "attack_skill": "斗殴",
                          "damage_expression": "1D6"},
        }]}

    fake_llm.set_response("smart", step)
    # 状态：挂起不投任何检定，骰序给空——若误投会因骰序耗尽而失败
    ct = await run_combat_turn(storage, world_id, "教徒扑向我", rng=SeqRng([]))
    assert ct.narration
    cc2 = storage.get_combat_runtime(world_id)
    # 挂起写入软状态（仅攻击意图，无攻击检定），游标停留原地，大轮不变
    assert cc2["pending_reaction"]["target_id"] == "player_01"
    assert "attack_roll" not in cc2["pending_reaction"]
    assert cc2["cursor"] == 0
    assert cc2["round_num"] == 1
    # 挂起不扣血
    assert storage.get_entity(world_id, "player_01")["hp"] == 12
    turn = storage.get_turn(world_id, 1)
    assert turn["context_data"]["combat"]["suspended"] is True
    assert turn["state_diff"]["numeric_changes"] == {}


async def test_run_combat_turn_resumes_and_steps_cursor(storage, world_id, fake_llm):
    """玩家声明防御后恢复结算：双方一起掷骰、伤害落库、清空挂起并步进游标。"""
    _seed(world_id, storage)
    # 状态：预置挂起——教徒攻击意图（未投检定），等待玩家防御
    storage.set_combat_runtime(world_id, {
        "in_combat": True,
        "round_num": 1,
        "turn_order": [
            {"entity_id": "npc_01", "dex": 40, "effective_dex": 90, "ready_gun": True},
            {"entity_id": "player_01", "dex": 60, "effective_dex": 60, "ready_gun": False},
        ],
        "cursor": 0,
        "pending_reaction": {
            "attacker_id": "npc_01", "target_id": "player_01",
            "attack_skill": "斗殴", "damage_expression": "1D6",
            "damage_bonus": "", "target_armor": 0, "difficulty": "regular",
            "bonus_penalty_dice": 0,
        },
        "outnumbered": {},
    })

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [_present_payload()]}
        return {"text": None, "tool_calls": [{
            "id": "c1", "name": "combat_resolve",
            "arguments": {"action": "strike", "defense": "dodge"},
        }]}

    fake_llm.set_response("smart", step)
    # 恢复轮：攻击 42/50 命中 + 防御 84/60 失败 + 伤害 1D6=5
    ct = await run_combat_turn(storage, world_id, "我闪避", rng=SeqRng([4, 2, 8, 4, 5]))
    assert ct.narration
    cc2 = storage.get_combat_runtime(world_id)
    # 挂起清空、游标步进（NPC 回合收尾 → 轮到玩家）
    assert cc2["pending_reaction"] is None
    assert cc2["cursor"] == 1
    # 双方一起掷骰：攻击 42/50 命中，防御 84/60 失败，伤害 5 落库
    assert storage.get_entity(world_id, "player_01")["hp"] == 7
    turn = storage.get_turn(world_id, 1)
    assert turn["context_data"]["combat"]["resumed"] is True
    assert turn["state_diff"]["numeric_changes"] == {"player_01.hp": -5}


# ============================================
# 行动者白名单：防越权代打与一轮多动
# ============================================

async def test_whitelist_rejects_rogue_attacker(storage, world_id, fake_llm):
    """PC 回合模型让 NPC 出手被拒，让玩家本人出手才放行。"""
    _seed(world_id, storage)
    runner = build_combat_runner(storage, current="player_01", rng=SeqRng([4, 2, 5]))
    rogue = await runner.execute("combat_resolve", {
        "action": "strike", "attacker_id": "npc_01", "target_id": "player_01",
        "attack_skill": "斗殴", "defense": "none", "damage_expression": "1D6",
    }, world_id=world_id, turn_num=1)
    assert rogue["ok"] is False
    assert "不得在本轮出手" in rogue["error"]
    # 状态：越权被拒未消耗骰序，合法行动照常结算（diff 由 runner 收集抽走）
    legal = await runner.execute("combat_resolve", {
        "action": "strike", "attacker_id": "player_01", "target_id": "npc_01",
        "attack_skill": "斗殴", "defense": "none", "damage_expression": "1D6",
    }, world_id=world_id, turn_num=1)
    assert legal["ok"] is True
    assert runner.collected_diffs[-1]["numeric_changes"] == {"npc_01.hp": -5}


async def test_whitelist_rejects_second_action_same_round(storage, world_id, fake_llm):
    """一次裁决只推进当前行动者的一次动作：同一行动者二次出手被拒。"""
    _seed(world_id, storage)
    runner = build_combat_runner(
        storage, current="player_01", rng=SeqRng([4, 2, 5, 4, 2, 5])
    )
    payload = {
        "action": "strike", "attacker_id": "player_01", "target_id": "npc_01",
        "attack_skill": "斗殴", "defense": "none", "damage_expression": "1D6",
    }
    first = await runner.execute(
        "combat_resolve", dict(payload), world_id=world_id, turn_num=1
    )
    assert first["ok"] is True
    second = await runner.execute(
        "combat_resolve", dict(payload), world_id=world_id, turn_num=1
    )
    assert second["ok"] is False
    assert "不得在本轮出手" in second["error"]


async def test_whitelist_allows_counterattack_switch(storage, world_id, fake_llm):
    """恢复轮反击命中后放行被反击方换向再结算。"""
    _seed(world_id, storage)
    pending = {
        "attacker_id": "npc_01", "target_id": "player_01",
        "attack_skill": "斗殴", "damage_expression": "1D6",
        "damage_bonus": "", "target_armor": 0, "difficulty": "regular",
        "bonus_penalty_dice": 0,
    }
    runner = build_combat_runner(
        storage, pending=pending, rng=SeqRng([4, 2, 1, 2, 4, 2, 5])
    )
    # 恢复轮首笔：npc_01 攻击 42/50 常规成功，player_01 反击 12/70 极难成功 → 反击命中
    first = await runner.execute("combat_resolve", {
        "action": "strike", "defense": "counterattack",
    }, world_id=world_id, turn_num=1)
    assert first["ok"] is True
    assert first["outcome"]["counter_hits"] is True
    # 白名单放行被反击方换向出手
    assert "player_01" in runner.actor_pool
    switch = await runner.execute("combat_resolve", {
        "action": "strike", "attacker_id": "player_01", "target_id": "npc_01",
        "attack_skill": "斗殴", "defense": "none", "damage_expression": "1D6",
    }, world_id=world_id, turn_num=1)
    assert switch["ok"] is True
    assert runner.collected_diffs[-1]["numeric_changes"] == {"npc_01.hp": -5}
