# -*- coding: utf-8 -*-
"""
@File     :   test_combat_batch.py
@Desc     :   战斗批量调度（run_combat_batch）与程序横幅测试：多 NPC 自动推进、
             轮回玩家/挂起/账战即停、on_step_narrated 逐动外推、NPC 自主轮不交还主动权
@Note     :   全程 fake LLM 零网络；骰序注入 SeqRng（d100 各消耗十位+个位两值）
"""

from __future__ import annotations

import pytest

from src.agent.combat import current_actor, run_combat_batch, run_combat_turn, start_combat
from src.agent.pipeline import run_narrated_turn


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)


def _seed(world_id, storage):
    """建一 PC（DEX60 格斗70 闪避60）与两 NPC（DEX40 斗殴50/65）的战斗舞台。"""
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


def _suspend_payload(attacker="npc_01", target="player_01", roll=(3, 0)):
    return {"id": "c1", "name": "combat_resolve", "arguments": {
        "action": "suspend", "attacker_id": attacker, "target_id": target,
        "attack_skill": "斗殴", "damage_expression": "1D6",
    }}


def _strike_payload(**kw):
    args = {"action": "strike", "attacker_id": "player_01", "target_id": "npc_01",
            "attack_skill": "斗殴", "defense": "none", "damage_expression": "1D6"}
    args.update(kw)
    return {"id": "c1", "name": "combat_resolve", "arguments": args}


def _present_payload(narrative="裁决完成。", in_combat=True):
    return {"id": "cp", "name": "present_combat",
            "arguments": {"narrative_directive": narrative, "in_combat": in_combat}}


def _mixed_step(suspend_attacker="npc_02", present="裁决完成。"):
    """Combat Agent 混合桩：PC 回合攻击敌人，NPC 回合向玩家挂起（触发 suspend 停止）。

    状态：按 messages 里的"当前行动者"分流，模拟真实 Combat Agent 的身份感知行为。
    """

    def step(messages):
        # 状态：assistant 工具调用轮的 content 可能为 None，统一折叠为字符串
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [_present_payload(present)]}
        if "【挂起反应·恢复结算轮】" in joined:
            return {"text": None, "tool_calls": [{
                "id": "c1", "name": "combat_resolve",
                "arguments": {"action": "strike", "defense": "dodge"},
            }]}
        if "当前行动者: player_01" in joined:
            return {"text": None, "tool_calls": [_strike_payload(defense="none")]}
        return {"text": None, "tool_calls": [_suspend_payload(attacker=suspend_attacker)]}

    return step


def _peaceful_step(present="战况推进。"):
    """Combat Agent 和平桩：PC 回合攻击敌人，NPC 回合做非攻击动作（挂掩体 Tag）。

    状态：NPC 不攻击则不触发挂起，用于验证调度器能连续自动推送并逐动外推。
    """

    def step(messages):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [_present_payload(present)]}
        if "当前行动者: player_01" in joined:
            return {"text": None, "tool_calls": [_strike_payload(defense="none")]}
        return {"text": None, "tool_calls": [{
            "id": "c1", "name": "manage_tags",
            "arguments": {"entity_id": "npc_01", "add_tags": ["寻找掩体"]},
        }]}

    return step


class SpyDirector:
    """若被调用即抛错的 Director 间谍——验证战斗分流不触主 Agent。"""

    async def run_turn(self, *args, **kwargs):
        raise AssertionError("战斗状态不应进入 Director")


# ============================================
# 程序横幅：先攻次序 + 行动主体 + 生死简况
# ============================================

def test_render_turn_banner_shows_order_and_actor(storage, world_id):
    from src.agent.combat import render_turn_banner

    _seed(world_id, storage)
    cc = start_combat(storage, world_id, ["player_01", "npc_01", "npc_02"])
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    # npc_02（斗殴65）先于 npc_01（斗殴50）行动；当前行动者 = 玩家
    banner = render_turn_banner(storage, world_id, cc, "player_01", entities)
    assert "战斗轮 1" in banner
    assert "调查员甲" in banner and "行动中" in banner
    # 先攻次序含全部参战者，且按实体名而非 id 呈现
    assert "教徒乙" in banner and "教徒甲" in banner
    assert "→" in banner


def test_render_turn_banner_marks_incapacitated(storage, world_id):
    from src.agent.combat import render_turn_banner

    _seed(world_id, storage)
    storage.update_entity(world_id, "npc_01", hp=0)
    cc = start_combat(storage, world_id, ["player_01", "npc_01", "npc_02"])
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    banner = render_turn_banner(storage, world_id, cc, "player_01", entities)
    # 已倒下的教徒甲带“已倒下”标记
    assert "教徒甲（已倒下）" in banner


# ============================================
# 批量调度：多 NPC 自动推进
# ============================================

async def test_batch_auto_advances_npcs_until_pc_turn(storage, world_id, fake_llm):
    """玩家行动结算后，调度器自动按顺位推进后续 NPC，直到轮回玩家才停。"""
    _seed(world_id, storage)
    # 状态：NPC 均做非攻击动作（不挂起），调度器才能一路推到轮回玩家
    fake_llm.set_response("smart", _peaceful_step())
    fake_llm.set_default(lambda messages: "教徒嘶吼着扑来。")
    start_combat(storage, world_id, ["player_01", "npc_01", "npc_02"])
    # 顺位 player_01 → npc_02 → npc_01，每行动者一 d100
    result = await run_combat_batch(
        storage, world_id, "我挥剑斩向教徒",
        rng=SeqRng([5, 2, 4, 9, 3, 7]),
    )
    assert result.narration
    # 战斗仍在进行且未产生挂起——调度器应停在轮回玩家处
    cc2 = storage.get_combat_runtime(world_id)
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    nxt = current_actor(cc2, entities)
    assert (entities.get(nxt) or {}).get("type") == "PC"
    # 三个行动者各落一轮独立 turn_num（玩家 + 两 NPC）
    assert storage.get_turn(world_id, 3) is not None


async def test_batch_stops_on_suspend(storage, world_id, fake_llm):
    """NPC 向玩家发起攻击挂起：调度器立即停止自动推送，等待玩家声明防御。"""
    _seed(world_id, storage)
    fake_llm.set_response("smart", _mixed_step(suspend_attacker="npc_01"))
    # 单 NPC：玩家步后即轮到 NPC，NPC 向玩家挂起 → 立即停
    start_combat(storage, world_id, ["player_01", "npc_01"])
    # 状态：玩家攻击消耗 2+1 骰，NPC 挂起不投骰（空余不消耗）
    await run_combat_batch(
        storage, world_id, "我挥剑斩向教徒", rng=SeqRng([5, 2, 4]),
    )
    cc2 = storage.get_combat_runtime(world_id)
    # 挂起写入软状态（仅攻击意图，无检定）、游标停留原地（指向发起攻击的 NPC，
    # 回合未收尾）、玩家未扣血（suspend 只登记不结算伤害）
    assert cc2["pending_reaction"]["attacker_id"] == "npc_01"
    assert "attack_roll" not in cc2["pending_reaction"]
    assert cc2["cursor"] == 1
    assert storage.get_entity(world_id, "player_01")["hp"] == 12


async def test_batch_on_step_narrated_fires_for_npc_turns(storage, world_id, fake_llm):
    """NPC 做非攻击动作不触发挂起：调度器连续自动推进并逐动外推。"""
    _seed(world_id, storage)
    fake_llm.set_response("smart", _peaceful_step())
    fake_llm.set_default(lambda messages: "教徒嘶吼着扑来。")
    pushed = []

    async def on_step(world_id, turn_num, narration):
        pushed.append((turn_num, narration))

    start_combat(storage, world_id, ["player_01", "npc_01", "npc_02"])
    result = await run_combat_batch(
        storage, world_id, "我挥剑斩向教徒",
        on_step_narrated=on_step,
        rng=SeqRng([5, 2, 4, 9, 3, 6]),
    )
    # 两个 NPC 步均被外推（末尾轮回玩家步作返回）
    assert len(pushed) == 2
    for tn, narration in pushed:
        assert narration


# ============================================
# NPC 回合的玩家声明：拒绝 + 落库语义对齐
# ============================================

async def test_npc_turn_rejects_player_input_with_notice(storage, world_id, fake_llm):
    """NPC 回合提交战斗声明：不进入结算、附拒绝提示，该轮 user 落占位符。"""
    _seed(world_id, storage)
    # 状态：教徒拔枪待击（DEX+50）压过玩家先动 —— 首轮即 NPC 回合
    start_combat(storage, world_id, ["player_01", "npc_01"], ready_gun_ids=["npc_01"])
    fake_llm.set_response("smart", _mixed_step(suspend_attacker="npc_01"))
    result = await run_combat_batch(
        storage, world_id, "我冲上去给他一棍子", rng=SeqRng([]),
    )
    # 玩家声明未生效：NPC 按自主战术挂起攻击，返回叙事附提示
    cc2 = storage.get_combat_runtime(world_id)
    assert cc2["pending_reaction"]["attacker_id"] == "npc_01"
    assert "未生效" in result.narration
    # 该轮实际结算的是 NPC 自主行动，落库 user 为占位符（不记玩家输入）
    turn = storage.get_turn(world_id, 1)
    assert turn["context_data"]["user"] == "（战斗轮自动推进）"


async def test_pc_turn_consumes_player_input(storage, world_id, fake_llm):
    """轮到 PC 时玩家输入正常消费：user 记玩家输入、行动者白名单放行玩家。"""
    _seed(world_id, storage)
    # 状态：玩家 DEX 60 高于教徒 40 —— 首轮即 PC 回合
    start_combat(storage, world_id, ["player_01", "npc_01"])
    fake_llm.set_response("smart", _mixed_step(suspend_attacker="npc_01"))
    await run_combat_batch(
        storage, world_id, "我挥剑斩向教徒", rng=SeqRng([4, 2, 5]),
    )
    turn = storage.get_turn(world_id, 1)
    assert turn["context_data"]["user"] == "我挥剑斩向教徒"
    # 玩家已出手 → 教徒受创
    assert storage.get_entity(world_id, "npc_01")["hp"] == 7


def _start_combat_payload(participants=("player_01", "npc_01")):
    return {"id": "sc", "name": "start_combat",
            "arguments": {"participant_ids": list(participants)}}


def _present_directive_payload(narrative="战斗建立，敌手抢得先手。"):
    return {"id": "pd", "name": "present_directive",
            "arguments": {"narrative_directive": narrative}}


def _director_open_then_combat_step():
    """开战轮走主 Agent（start_combat + present_directive），后续战斗轮走 Combat Agent。"""

    def step(messages):
        sysc = str((messages[0] or {}).get("content") or "")
        if "战斗轮专职裁判" in sysc:
            if any(m["role"] == "tool" for m in messages):
                return {"text": None, "tool_calls": [_present_payload()]}
            return {"text": None, "tool_calls": [_suspend_payload()]}
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [_present_directive_payload()]}
        return {"text": None, "tool_calls": [_start_combat_payload()]}

    return step


async def test_pipeline_auto_advances_npc_right_after_start_combat(storage, world_id, fake_llm):
    """开战轮后顺位首位是 NPC：pipeline 立即接力推送 NPC，不让系统悬停空等玩家。"""
    # 状态：NPC DEX 70 高于玩家 60 —— 开战后首行动者是 NPC
    storage.create_entity(
        world_id, "player_01", "PC", "托马斯", hp=11, hp_max=11,
        attributes_and_skills={"DEX": 60, "格斗": 70, "闪避": 60},
    )
    storage.create_entity(
        world_id, "npc_01", "NPC", "尖刀利奥", hp=10, hp_max=10,
        attributes_and_skills={"DEX": 70, "格斗": 50},
    )
    fake_llm.set_response("smart", _director_open_then_combat_step())
    fake_llm.set_response("standard", lambda messages: "刀锋在雨幕中亮起。")
    result = await run_narrated_turn(storage, world_id, "我握紧短棍上前")
    cc = storage.get_combat_runtime(world_id)
    # 开战公告并入返回值（本测试未传外推回调）
    assert "————战斗开始————" in result.narration
    # 首位 NPC 已当场行动并挂起攻击，无需玩家先输入
    assert cc["pending_reaction"]["attacker_id"] == "npc_01"
    # 开战轮 + NPC 自主轮各落一轮（逐动独立 turn_num）
    assert storage.get_turn(world_id, 2) is not None