# -*- coding: utf-8 -*-
"""
@File     :   dice.py
@Desc     :   掷骰引擎：d100 基础骰、奖惩骰（十位重掷）、NdN 与表达式掷骰
@Note     :   纯函数无副作用，全部支持注入 rng 保证可复现测试；
             奖惩骰严格按 CoC 7th——个位永不重掷，只对十位骰取极值
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ============================================
# 掷骰结果
# ============================================


@dataclass(frozen=True)
class D100Roll:
    """一次 d100 掷骰完整明细（含奖惩骰候选）。

    tens_list: 全部十位骰候选（首位为基准），供 agent 展示奖惩过程
    ones:      个位骰结果（奖惩骰时永不重掷）
    total:     最终判定点数（十位/个位全 0 时记 100）
    """

    tens_list: Tuple[int, ...]
    ones: int
    total: int

    @property
    def tens_rolls(self) -> List[int]:
        return list(self.tens_list)


# ============================================
# 基础掷骰
# ============================================


def _tens_die(rng) -> int:
    return rng.randint(0, 9)


def _compose_total(tens: int, ones: int) -> int:
    """十位 + 个位合成 d100；00 组合按 CoC 表示 100。"""
    return 100 if (tens == 0 and ones == 0) else tens * 10 + ones


def roll_d100(rng: Optional[random.Random] = None) -> D100Roll:
    """掷一组基准 d100（十位骰 + 个位骰），无奖惩骰。"""
    rng = rng or random
    tens = _tens_die(rng)
    ones = _tens_die(rng)
    return D100Roll((tens,), ones, _compose_total(tens, ones))


def roll_with_bonus_penalty(
    bonus: int = 0,
    penalty: int = 0,
    rng: Optional[random.Random] = None,
) -> D100Roll:
    """掷 d100 并应用奖惩骰，返回最终判定点数与全部十位骰明细。

    CoC 7th 规则：个位固定为基准骰的个位，十位骰额外掷出后取极值——
    奖励骰取 min（更易成功），惩罚骰取 max（更难成功）。
      例：基准 (5,3)=53，奖励骰候选 2 → 最终 23；惩罚骰候选 8 → 最终 83
    """
    if bonus > 0 and penalty > 0:
        raise ValueError("奖励骰与惩罚骰不可同时存在")
    rng = rng or random
    base_tens = _tens_die(rng)
    ones = _tens_die(rng)
    candidates: List[int] = [base_tens]
    extra = bonus if bonus > 0 else penalty
    for _ in range(abs(extra)):
        candidates.append(_tens_die(rng))
    final_tens = max(candidates) if penalty else min(candidates)
    return D100Roll(tuple(candidates), ones, _compose_total(final_tens, ones))


def roll_ndn(
    n: int, sides: int, rng: Optional[random.Random] = None
) -> List[int]:
    """掷 n 个 sides 面骰，返回每个骰子的值列表；非法参数钳制到 0/1。"""
    rng = rng or random
    n = max(0, int(n))
    sides = max(1, int(sides))
    return [rng.randint(1, sides) for _ in range(n)]


# ============================================
# 表达式掷骰
# ============================================

_EXPR_DIE = re.compile(r"^(\d+)[D](\d+)$")


def roll_expression(
    expression: str, rng: Optional[random.Random] = None
) -> int:
    """解析并掷骰表达式，返回总和。

    支持格式：纯数字 "5"、"1D6"、"2D6+2"、"1D3+1D4"、"1D6-1"；
    非法表达式抛 ValueError。
    """
    expr = (expression or "").strip().upper()
    try:
        return int(expr)
    except ValueError:
        pass
    total = 0
    sign = 1
    for part in re.split(r"([+-])", expr):
        if part == "+":
            sign = 1
            continue
        if part == "-":
            sign = -1
            continue
        part = part.strip()
        if not part:
            continue
        match = _EXPR_DIE.match(part)
        if match:
            n, sides = int(match.group(1)), int(match.group(2))
            total += sign * sum(roll_ndn(n, sides, rng))
        else:
            try:
                total += sign * int(part)
            except ValueError:
                raise ValueError(f"无效的骰子表达式: {expression}") from None
    return total
