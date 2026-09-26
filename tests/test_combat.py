# -*- coding: utf-8 -*-
"""
@File     :   test_combat.py
@Desc     :   战斗规则内核单测：先攻排序、对抗比较、战技拦截、贯穿伤害、重伤濒死级联
@Note     :   覆盖决策要点——闪避严格大于、反击平局攻击方胜、贯穿满额+加骰、
             重伤按骰点伤害判定且护甲全挡不判、HP 归零重伤濒死
"""

import pytest

from src.rules.checks import SuccessLevel
from src.rules.combat import (
    Combatant,
    DEFENSE_COUNTERATTACK,
    DEFENSE_DODGE,
    DEFENSE_NONE,
    TAG_DYING,
    TAG_MAJOR_WOUND,
    TAG_UNCONSCIOUS,
    compare_success_levels,
    dexterity_order,
    expression_max,
    maneuver_build_penalty,
    opposed_combat,
    resolve_physical_cascade,
    roll_damage_result,
)


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)

    def shuffle(self, items):
        # 状态：随机破局由 rng.shuffle 驱动，测试桩保持原序保证可复现断言
        pass


# ============================================
# DEX 先攻排序
# ============================================

def _cs(entity_id, dex, fight=0, shoot=0, ready=False):
    return Combatant(entity_id, dex, fight, shoot, ready)


def test_initiative_ordered_by_dex_desc():
    order = dexterity_order(
        [_cs("a", 40), _cs("b", 70), _cs("c", 55)], rng=SeqRng([])
    )
    assert [e.entity_id for e in order] == ["b", "c", "a"]


def test_initiative_ready_gun_plus_50():
    # 敏捷 30 的枪手拔枪待击（生效 80）压过敏捷 60 的徒手者
    order = dexterity_order(
        [_cs("slow_gun", 30, ready=True), _cs("fast_unarmed", 60)], rng=SeqRng([])
    )
    assert [e.entity_id for e in order] == ["slow_gun", "fast_unarmed"]
    assert order[0].effective_dex == 80


def test_initiative_tiebreak_by_fight_skill_melee():
    # 近战平局按格斗技能定序：格斗 65 的先于格斗 40
    order = dexterity_order(
        [_cs("a", 50, fight=40), _cs("b", 50, fight=65)], ranged=False, rng=SeqRng([])
    )
    assert [e.entity_id for e in order] == ["b", "a"]


def test_initiative_tiebreak_by_shoot_skill_ranged():
    # 远程平局按射击技能定序
    order = dexterity_order(
        [_cs("a", 50, shoot=40), _cs("b", 50, shoot=65)], ranged=True, rng=SeqRng([])
    )
    assert [e.entity_id for e in order] == ["b", "a"]


def test_initiative_tiebreak_random_when_ties():
    # 生效 DEX 相等、战斗技能也相等时随机破局——只断言成员集合与头名稳定，
    # 不承诺具体随机序列，避免脆弱断言
    order = dexterity_order(
        [_cs("a", 50, fight=50), _cs("b", 50, fight=50), _cs("c", 80)],
        rng=SeqRng([]),
    )
    assert [e.entity_id for e in order][0] == "c"
    assert {e.entity_id for e in order[1:]} == {"a", "b"}


# ============================================
# 对抗成功等级比较
# ============================================

def test_compare_levels_relative():
    assert compare_success_levels(SuccessLevel.EXTREME, SuccessLevel.REGULAR) == 1
    assert compare_success_levels(SuccessLevel.REGULAR, SuccessLevel.REGULAR) == 0
    assert compare_success_levels(SuccessLevel.FAILURE, SuccessLevel.HARD) == -1


def test_opposed_dodge_requires_strictly_greater():
    # 闪避需严格大于：防守方常规成功但攻击方困难成功 → 平级差一档，仍被击中
    r = opposed_combat(SuccessLevel.HARD, SuccessLevel.REGULAR, DEFENSE_DODGE)
    assert r["attacker_hits"] is True and r["defender_evades"] is False
    # 防守方极难 > 攻击方常规 → 成功闪开
    r2 = opposed_combat(SuccessLevel.REGULAR, SuccessLevel.EXTREME, DEFENSE_DODGE)
    assert r2["attacker_hits"] is False and r2["defender_evades"] is True


def test_opposed_counterattack_tie_attacker_wins():
    # 反击平局攻击方胜：同档时反击不成立
    r = opposed_combat(SuccessLevel.HARD, SuccessLevel.HARD, DEFENSE_COUNTERATTACK)
    assert r["attacker_hits"] is True and r["counter_hits"] is False
    # 防守方严格大于才反击命中
    r2 = opposed_combat(
        SuccessLevel.HARD, SuccessLevel.CRITICAL, DEFENSE_COUNTERATTACK
    )
    assert r2["attacker_hits"] is False and r2["counter_hits"] is True


def test_opposed_no_defense_hits_on_success():
    r = opposed_combat(SuccessLevel.REGULAR, SuccessLevel.CRITICAL, DEFENSE_NONE)
    assert r["attacker_hits"] is True


def test_opposed_attacker_failure_misses():
    r = opposed_combat(SuccessLevel.FAILURE, SuccessLevel.REGULAR, DEFENSE_DODGE)
    assert r["attacker_hits"] is False and r["attacker_failed"] is True


def test_opposed_fumble_marked():
    r = opposed_combat(SuccessLevel.FUMBLE, SuccessLevel.CRITICAL, DEFENSE_COUNTERATTACK)
    assert r["attacker_fumbled"] is True and r["attacker_hits"] is False


def test_opposed_unknown_defense_raises():
    with pytest.raises(ValueError):
        opposed_combat(SuccessLevel.REGULAR, SuccessLevel.REGULAR, "parry")


# ============================================
# 战技体格拦截
# ============================================

def test_maneuver_intercept_when_defender_much_bigger():
    r = maneuver_build_penalty(attacker_build=1, defender_build=4)
    assert r["intercept"] is True and r["penalty_dice"] == 0


def test_maneuver_penalty_when_build_disadvantage():
    r = maneuver_build_penalty(attacker_build=1, defender_build=3)
    assert r["intercept"] is False and r["penalty_dice"] == 2
    r2 = maneuver_build_penalty(attacker_build=2, defender_build=3)
    assert r2["penalty_dice"] == 1


def test_maneuver_no_penalty_on_even_or_advantage():
    r = maneuver_build_penalty(attacker_build=3, defender_build=1)
    assert r["intercept"] is False and r["penalty_dice"] == 0


# ============================================
# 贯穿与伤害结算
# ============================================

def test_expression_max_resolves():
    assert expression_max("1D6") == 6
    assert expression_max("2D6+2") == 14
    assert expression_max("1D6+1D4") == 10
    assert expression_max("1D6-1") == 5
    assert expression_max("5") == 5


def test_expression_max_invalid_raises():
    with pytest.raises(ValueError):
        expression_max("abc")


def test_damage_regular_roll():
    r = roll_damage_result("1D6", SuccessLevel.REGULAR, rng=SeqRng([4]))
    assert r.rolled == 4 and r.impale is False and r.total == 4


def test_damage_impale_max_plus_rolled():
    # 极难成功贯穿：满额 6 + 掷骰 4 = 10
    r = roll_damage_result("1D6", SuccessLevel.EXTREME, rng=SeqRng([4]))
    assert r.impale is True and r.total == 10
    r2 = roll_damage_result("1D6", SuccessLevel.CRITICAL, rng=SeqRng([4]))
    assert r2.impale is True and r2.total == 10


def test_damage_hard_success_not_impale():
    r = roll_damage_result("1D8", SuccessLevel.HARD, rng=SeqRng([7]))
    assert r.impale is False and r.total == 7


# ============================================
# 重伤与濒死级联
# ============================================

def _cascade(**kw):
    base = dict(hp_old=10, hp_max=10, armor=0, damage_total=0, con=50, rng=None)
    base.update(kw)
    return resolve_physical_cascade(**base)


def test_cascade_no_damage_no_tags():
    r = _cascade(damage_total=2)
    assert r["net_damage"] == 2 and r["major_wound"] is False
    assert r["hp_new"] == 8 and r["tags"] == []


def test_cascade_major_wound_con_fail_unconscious():
    # 骰点 6 >= 半血 5，CON 困难检定失败 → 重伤 + 昏迷；骰序 [6] 十位->个位=66 > 25
    r = _cascade(damage_total=6, rng=SeqRng([6, 6]))
    assert r["major_wound"] is True and r["unconscious"] is True
    assert TAG_MAJOR_WOUND in r["tags"] and TAG_UNCONSCIOUS in r["tags"]


def test_cascade_major_wound_con_success_stays_awake():
    # CON 困难成功（25）→ 重伤但不昏迷；骰序 [2, 5] = 25 <= 25
    r = _cascade(damage_total=6, rng=SeqRng([2, 5]))
    assert r["major_wound"] is True and r["unconscious"] is False
    assert TAG_MAJOR_WOUND in r["tags"] and TAG_UNCONSCIOUS not in r["tags"]


def test_cascade_hp_zero_without_major_unconscious():
    # hp 3 吃 3 点（< 半血 5 不构成重伤）→ 归零但只昏迷不濒死
    r = _cascade(hp_old=3, hp_max=10, damage_total=3)
    assert r["dying"] is False and r["unconscious"] is True
    assert TAG_UNCONSCIOUS in r["tags"] and TAG_DYING not in r["tags"]


def test_cascade_hp_zero_with_major_dying():
    # 重伤（6 >= 5）且 hp 归零 → 濒死
    r = _cascade(hp_old=4, hp_max=10, damage_total=6)
    assert r["dying"] is True
    assert TAG_DYING in r["tags"] and TAG_MAJOR_WOUND in r["tags"]


def test_cascade_hp_zero_with_previous_major_dying():
    # 此前已受重伤（already_major_wound）+ 本次 hp 归零（伤害不足半血）→ 濒死
    r = _cascade(
        hp_old=3, hp_max=10, damage_total=3, already_major_wound=True,
    )
    assert r["dying"] is True


def test_cascade_armor_fully_absorbs_no_major():
    # 护甲吞噬全部伤害（净伤 0）→ 不判重伤、无 Tag
    r = _cascade(hp_old=10, hp_max=10, damage_total=6, armor=6)
    assert r["net_damage"] == 0 and r["major_wound"] is False and r["tags"] == []


def test_cascade_armor_partial_reduces_hp():
    r = _cascade(hp_old=10, hp_max=10, damage_total=6, armor=4)
    assert r["armor_abs"] == 4 and r["net_damage"] == 2
    assert r["hp_new"] == 8