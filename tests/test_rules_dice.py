# -*- coding: utf-8 -*-
"""
@File     :   test_rules_dice.py
@Desc     :   掷骰引擎单测：d100 语义、奖惩骰十位取极值、NdN 与表达式
@Note     :   用 SeqRng 桩注入确定骰序，逐边界断言；重点防奖惩骰回归
"""

import pytest

from src.rules.dice import (
    roll_d100,
    roll_expression,
    roll_ndn,
    roll_with_bonus_penalty,
)


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)


# ============================================
# d100 基础语义
# ============================================

def test_roll_d100_composes_tens_and_ones():
    roll = roll_d100(SeqRng([3, 7]))  # 十位 3，个位 7
    assert roll.tens_list == (3,)
    assert roll.ones == 7
    assert roll.total == 37


def test_roll_d100_zero_zero_is_100():
    roll = roll_d100(SeqRng([0, 0]))  # 00 → 100
    assert roll.total == 100


def test_roll_d100_zero_tens_keeps_ones():
    roll = roll_d100(SeqRng([0, 5]))  # 十位 0，个位 5 → 05
    assert roll.total == 5


# ============================================
# 奖惩骰（十位重掷、个位固定）
# ============================================

def test_bonus_dice_takes_min_tens_keep_ones():
    # 基准 (5,3)=53；奖励骰候选 2 → 十位取 min(5,2)=2，个位 3 保持 → 23
    roll = roll_with_bonus_penalty(bonus=1, rng=SeqRng([5, 3, 2]))
    assert roll.tens_list == (5, 2)
    assert roll.ones == 3
    assert roll.total == 23


def test_penalty_dice_takes_max_tens_keep_ones():
    # 基准 (5,3)=53；惩罚骰候选 8 → 十位取 max(5,8)=8，个位 3 保持 → 83
    roll = roll_with_bonus_penalty(penalty=1, rng=SeqRng([5, 3, 8]))
    assert roll.total == 83


def test_bonus_penalty_only_rerolls_tens_not_ones():
    # 多个奖励骰：基准 (5,3)，候选 2、4 → min=2，个位 3 不变 → 23
    roll = roll_with_bonus_penalty(bonus=2, rng=SeqRng([5, 3, 2, 4]))
    assert roll.tens_list == (5, 2, 4)
    assert roll.ones == 3
    assert roll.total == 23


def test_no_bonus_penalty_equals_base():
    roll = roll_with_bonus_penalty(rng=SeqRng([5, 3]))
    assert roll.total == 53


def test_bonus_and_penalty_mutually_exclusive():
    with pytest.raises(ValueError):
        roll_with_bonus_penalty(bonus=1, penalty=1, rng=SeqRng([1, 1, 1]))


def test_bonus_can_turn_00_into_100_guard():
    # 基准 (0,0)=100；奖励骰候选 9 → min(0,9)=0 → 仍是 100（00 组合）
    roll = roll_with_bonus_penalty(bonus=1, rng=SeqRng([0, 0, 9]))
    assert roll.total == 100


# ============================================
# NdN 与表达式
# ============================================

def test_roll_ndn_counts_and_range():
    assert roll_ndn(3, 6, SeqRng([1, 6, 3])) == [1, 6, 3]
    assert roll_ndn(0, 6, SeqRng([])) == []


def test_roll_expression_plain_number():
    assert roll_expression("5", SeqRng([])) == 5


def test_roll_expression_single_die_with_modifier():
    assert roll_expression("2D6+2", SeqRng([1, 6])) == 9  # (1+6)+2


def test_roll_expression_multi_die_group():
    assert roll_expression("1D3+1D4", SeqRng([3, 4])) == 7


def test_roll_expression_negative_modifier():
    assert roll_expression("1D6-1", SeqRng([2])) == 1


def test_roll_expression_invalid_raises():
    with pytest.raises(ValueError):
        roll_expression("abc", SeqRng([]))
