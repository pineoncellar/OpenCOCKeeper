# -*- coding: utf-8 -*-
"""
@File     :   test_rules_checks.py
@Desc     :   检定内核单测：成功等级边界、难度阈值、技能/属性检定、检定项解析、clamp
@Note     :   成功等级逐边界断言（01/96-100/阈值折半）；属性检定目标 = 属性值本身
"""

import pytest

from src.rules.checks import (
    Difficulty,
    SuccessLevel,
    determine_success_level,
    get_difficulty_threshold,
    parse_difficulty,
    skill_check,
    stat_check,
)
from src.rules.stats import (
    CheckTarget,
    clamp_stat,
    normalize_target_name,
    resolve_check_target,
)


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)


# ============================================
# 成功等级边界
# ============================================

def test_success_level_critical_on_1():
    assert determine_success_level(60, 1) is SuccessLevel.CRITICAL


def test_success_level_extreme_boundary():
    assert determine_success_level(60, 12) is SuccessLevel.EXTREME  # 60//5
    assert determine_success_level(60, 13) is not SuccessLevel.EXTREME


def test_success_level_hard_boundary():
    assert determine_success_level(60, 30) is SuccessLevel.HARD  # 60//2
    assert determine_success_level(60, 31) is not SuccessLevel.HARD


def test_success_level_regular_boundary():
    assert determine_success_level(60, 60) is SuccessLevel.REGULAR
    assert determine_success_level(60, 61) is not SuccessLevel.REGULAR


def test_success_level_fumble_on_96_plus():
    assert determine_success_level(60, 96) is SuccessLevel.FUMBLE
    assert determine_success_level(60, 95) is SuccessLevel.FAILURE  # 未到 96
    assert determine_success_level(40, 100) is SuccessLevel.FUMBLE


def test_success_level_zero_skill_never_critical():
    # skill=0 时 5 不为大成功，且 5>max(1,0//5) 判定走失败（非大失败，5<96）
    assert determine_success_level(0, 5) is SuccessLevel.FAILURE


# ============================================
# 难度阈值
# ============================================

def test_difficulty_threshold_halves():
    assert get_difficulty_threshold(60, Difficulty.REGULAR) == 60
    assert get_difficulty_threshold(60, Difficulty.HARD) == 30
    assert get_difficulty_threshold(60, Difficulty.EXTREME) == 12


def test_difficulty_threshold_floor_at_1():
    assert get_difficulty_threshold(1, Difficulty.HARD) == 1
    assert get_difficulty_threshold(1, Difficulty.EXTREME) == 1


def test_parse_difficulty_accepts_string_and_enum():
    assert parse_difficulty("hard") is Difficulty.HARD
    assert parse_difficulty(Difficulty.EXTREME) is Difficulty.EXTREME
    with pytest.raises(ValueError):
        parse_difficulty("impossible")


# ============================================
# 技能 / 属性检定
# ============================================

def test_skill_check_regular_success():
    result = skill_check(60, rng=SeqRng([2, 3]))  # 23 ≤ 60
    assert result.is_success
    assert result.threshold == 60
    assert result.roll_value == 23
    assert result.tens_rolls == [2]


def test_skill_check_hard_difficulty_lowers_threshold():
    # 60 HARD 阈值 30；掷 45 → 失败
    result = skill_check(60, difficulty=Difficulty.HARD, rng=SeqRng([4, 5]))
    assert result.threshold == 30
    assert not result.is_success


def test_skill_check_bonus_dice_applied():
    # 60 常规，奖励骰候选 1 → 最终 13 ≤ 60
    result = skill_check(60, bonus=1, rng=SeqRng([5, 3, 1]))
    assert result.roll_value == 13
    assert result.is_success


def test_stat_check_target_is_stat_value_not_x5():
    # 属性 50 → 目标 50（非 250）；掷 37 ≤ 50 成功
    result = stat_check(50, rng=SeqRng([3, 7]))
    assert result.skill_value == 50
    assert result.is_success


# ============================================
# 检定项解析（别名归一）
# ============================================

def test_normalize_aliases():
    assert normalize_target_name("力量") == ("stat", "STR")
    assert normalize_target_name("str") == ("stat", "STR")
    assert normalize_target_name("理智") == ("san", "理智")
    assert normalize_target_name("SAN") == ("san", "理智")
    assert normalize_target_name("侦查") == ("skill", "侦查")


def test_resolve_skill_from_table():
    skills = {"侦查": 60, "STR": 50, "力量": 55}
    target = resolve_check_target(skills, "侦查")
    assert target == CheckTarget("skill", "侦查", 60)


def test_resolve_stat_abbrev_preferred_then_chinese():
    skills = {"STR": 50, "力量": 55}
    assert resolve_check_target(skills, "力量").value == 50  # 缩写优先
    skills_no_abbrev = {"力量": 55}
    assert resolve_check_target(skills_no_abbrev, "力量").value == 55


def test_resolve_san_returns_none_value():
    target = resolve_check_target({}, "理智")
    assert target.kind == "san"
    assert target.value is None


def test_resolve_missing_raises_keyerror():
    with pytest.raises(KeyError):
        resolve_check_target({"侦查": 60}, "聆听")


# ============================================
# clamp 与 applied_delta
# ============================================

def test_clamp_to_low():
    assert clamp_stat(2, -5, high=12) == (-2, 0)


def test_clamp_to_high():
    assert clamp_stat(10, 5, high=12) == (2, 12)


def test_clamp_within_bounds_unchanged():
    assert clamp_stat(5, 3, high=12) == (3, 8)


def test_clamp_no_high_is_unbounded():
    # 决策 D6：high=None / <=0 视为无上界，正向放行不截断
    assert clamp_stat(10, 5, high=None) == (5, 15)
    assert clamp_stat(10, 5, high=0) == (5, 15)
