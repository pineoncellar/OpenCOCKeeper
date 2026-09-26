# -*- coding: utf-8 -*-
"""
@File     :   test_combat_resolve.py
@Desc     :   战斗结算原子工具单测：strike 对抗命中/伤害/贯穿/重伤级联、maneuver 战技检定
@Note     :   走 storage fixture 读实体、不写库——断言焦点在 checks 权威区、state_diff 与摘要
"""

import pytest

from src.tools.combat_resolve import (
    build_combat_resolve_schema,
    combat_resolve,
    parse_combat_resolve,
)


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)


def _make(storage, world_id, eid, etype="NPC", hp=12, hp_max=12, skills=None, tags=None):
    """在测试世界建一个实体，返回实体 dict。"""
    base = {
        "斗殴": 70,
        "手枪": 60,
        "闪避": 60,
        "射击(手枪)": 60,
        "STR": 50,
        "DEX": 50,
        "CON": 50,
        "SIZ": 50,
    }
    base.update(skills or {})
    return storage.create_entity(
        world_id, eid, etype, f"角色{eid}",
        hp=hp, hp_max=hp_max, attributes_and_skills=base, tags=tags or [],
    )


# ============================================
# strike：对抗攻击结算
# ============================================

def test_strike_no_defense_hits_with_damage(storage, world_id):
    _make(storage, world_id, "atk", etype="PC")
    _make(storage, world_id, "tgt")
    # 骰序 [5,2] 攻击 52 REGULAR（≤70），[4] 伤害 4 点 → tgt 12->8
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "strike",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "damage_expression": "1D6",
    }, rng=SeqRng([5, 2, 4]))
    assert r["ok"] and r["damage"]["total"] == 4
    assert r["physical"]["hp_new"] == 8
    assert r["state_diff"]["numeric_changes"] == {"tgt.hp": -4}
    assert r["check"]["success_level"] == "REGULAR"


def test_strike_no_defense_miss(storage, world_id):
    _make(storage, world_id, "atk", etype="PC", skills={"斗殴": 30})
    _make(storage, world_id, "tgt")
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "strike",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "damage_expression": "1D6",
    }, rng=SeqRng([5, 5]))  # 55 > 30 FAILURE
    assert r["damage"] is None
    assert r["state_diff"]["numeric_changes"] == {}
    assert "未命中" in r["summary_for_agent"]


def test_strike_dodge_succeeds_when_strictly_greater(storage, world_id):
    _make(storage, world_id, "atk", etype="PC")
    _make(storage, world_id, "tgt")
    # 攻击 52 REGULAR，闪避 12 EXTREME（严格大于）→ 成功闪开
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "strike",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "defense": "dodge",
        "damage_expression": "1D6",
    }, rng=SeqRng([5, 2, 1, 2]))
    assert r["outcome"]["defender_evades"] is True
    assert r["outcome"]["attacker_hits"] is False
    assert r["damage"] is None
    # 权威区含攻击与闪避两条检定
    assert len(r["extra_checks"]) == 1
    assert r["extra_checks"][0]["skill_or_attribute"] == "闪避"


def test_strike_dodge_tie_attacker_hits(storage, world_id):
    _make(storage, world_id, "atk", etype="PC")
    _make(storage, world_id, "tgt")
    # 攻击 30 HARD、闪避 30 HARD 同档 → 闪避未压过攻击，被命中
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "strike",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "defense": "dodge",
        "damage_expression": "1D6",
    }, rng=SeqRng([3, 0, 3, 0, 4]))
    assert r["outcome"]["attacker_hits"] is True
    assert r["damage"]["total"] == 4


def test_strike_counterattack_tie_attacker_wins(storage, world_id):
    _make(storage, world_id, "atk", etype="PC")
    _make(storage, world_id, "tgt")
    # 反击平局攻击方胜：同档 HARD 时反击不成立，仍被命中
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "strike",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "defense": "counterattack",
        "damage_expression": "1D6",
    }, rng=SeqRng([3, 0, 3, 0, 4]))
    assert r["outcome"]["counter_hits"] is False
    assert r["outcome"]["attacker_hits"] is True


def test_strike_counterattack_success_counters(storage, world_id):
    _make(storage, world_id, "atk", etype="PC")
    _make(storage, world_id, "tgt", skills={"斗殴": 99})
    # 攻击 52 REGULAR，防守方 01 CRITICAL（严格大于）→ 反击成立并顶掉攻击
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "strike",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "defense": "counterattack",
        "damage_expression": "1D6",
    }, rng=SeqRng([5, 2, 0, 1]))
    assert r["outcome"]["counter_hits"] is True
    assert r["outcome"]["attacker_hits"] is False


def test_strike_impale_max_plus_rolled_and_major_wound(storage, world_id):
    _make(storage, world_id, "atk", etype="PC")
    _make(storage, world_id, "tgt", hp=20, hp_max=20)
    # 攻击 10 EXTREME（≤14）贯穿：满额 6 + 掷骰 4 = 10；CON 困难 66 > 25 失败 → 昏迷
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "strike",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "damage_expression": "1D6",
    }, rng=SeqRng([1, 0, 4, 6, 6]))
    dmg = r["damage"]
    assert dmg["impale"] is True and dmg["max_value"] == 6 and dmg["total"] == 10
    phy = r["physical"]
    assert phy["major_wound"] is True and phy["unconscious"] is True
    diff = r["state_diff"]
    assert diff["numeric_changes"] == {"tgt.hp": -10}
    assert set(diff["tags"]["tgt"]["added"]) == {"重伤", "昏迷"}
    # CON 检定进权威区
    con = [c for c in r["extra_checks"] if c.get("kind") == "con_check_major_wound"]
    assert len(con) == 1 and con[0]["is_success"] is False


def test_strike_impale_respects_armor_no_tag_when_zero_net(storage, world_id):
    _make(storage, world_id, "atk", etype="PC")
    _make(storage, world_id, "tgt")
    # 命中 4 点被 4 点护甲全挡 → 无 HP 变更、不判重伤
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "strike",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "damage_expression": "1D6",
        "target_armor": 4,
    }, rng=SeqRng([5, 2, 4]))
    assert r["physical"]["net_damage"] == 0
    assert r["state_diff"]["numeric_changes"] == {}


# ============================================
# maneuver：战技检定
# ============================================

def test_maneuver_intercept_no_roll(storage, world_id):
    _make(storage, world_id, "atk", etype="PC")
    _make(storage, world_id, "tgt")
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "maneuver",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "attacker_build": 1, "target_build": 4,
    }, rng=SeqRng([]))
    assert r["build"]["intercept"] is True
    assert r["is_success"] is False
    assert r["extra_checks"] == []


def test_maneuver_penalty_applied_to_check(storage, world_id):
    _make(storage, world_id, "atk", etype="PC", skills={"斗殴": 50})
    _make(storage, world_id, "tgt")
    # 体型劣势 2 惩罚骰：骰序 [5,2,8,9] → 十位候选 5/8/9 取大 → 92 > 50 失败
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "maneuver",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "attacker_build": 1, "target_build": 3,
        "maneuver_kind": "绊摔",
    }, rng=SeqRng([5, 2, 8, 9]))
    assert r["build"]["penalty_dice"] == 2
    assert r["is_success"] is False
    assert r["extra_checks"][0]["bonus_penalty_dice"] == -2


def test_maneuver_success_no_penalty(storage, world_id):
    _make(storage, world_id, "atk", etype="PC", skills={"斗殴": 70})
    _make(storage, world_id, "tgt")
    # 攻击者体格占优无惩罚：掷 23 REGULAR → 成功
    r = combat_resolve(storage, {
        "world_id": world_id, "action": "maneuver",
        "attacker_id": "atk", "target_id": "tgt",
        "attack_skill": "斗殴", "attacker_build": 2, "target_build": 1,
        "maneuver_kind": "擒抱",
    }, rng=SeqRng([2, 3]))
    assert r["is_success"] is True
    assert "擒抱命中成功" in r["summary_for_agent"]


# ============================================
# 输入校验与 schema
# ============================================

def test_parse_combat_resolve_requires_fields():
    with pytest.raises(ValueError):
        parse_combat_resolve({"action": "strike", "attacker_id": "a"})
    with pytest.raises(ValueError):
        parse_combat_resolve({"action": "bogus", "world_id": "w",
                              "attacker_id": "a", "target_id": "b"})
    with pytest.raises(ValueError):
        parse_combat_resolve({"action": "strike", "world_id": "w",
                              "attacker_id": "a", "target_id": "b",
                              "defense": "parry"})


def test_build_combat_resolve_schema_shape():
    schema = build_combat_resolve_schema()
    assert "action" in schema["properties"]
    assert "attacker_id" in schema["required"]


def test_strike_missing_entity_raises(storage, world_id):
    with pytest.raises(Exception):
        combat_resolve(storage, {
            "world_id": world_id, "action": "strike",
            "attacker_id": "ghost", "target_id": "tgt",
            "attack_skill": "斗殴", "damage_expression": "1D6",
        }, rng=SeqRng([5, 2]))