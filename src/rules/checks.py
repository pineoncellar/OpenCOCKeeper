# -*- coding: utf-8 -*-
"""
@File     :   checks.py
@Desc     :   检定内核：成功等级判定、难度阈值、技能/属性检定
@Note     :   100% 确定性无 LLM；属性检定目标 = 属性值本身（CoC 7th 属性即百分比，非 ×5）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from .dice import roll_with_bonus_penalty


class SuccessLevel(Enum):
    """成功等级（按判定优先级从高到低）。"""

    CRITICAL = "CRITICAL"  # 大成功：骰出 01
    EXTREME = "EXTREME"  # 极难成功：≤ 生效阈值 / 5
    HARD = "HARD"  # 困难成功：≤ 生效阈值 / 2
    REGULAR = "REGULAR"  # 常规成功：≤ 生效阈值
    FAILURE = "FAILURE"  # 失败
    FUMBLE = "FUMBLE"  # 大失败：96-100 且 > 生效阈值


class Difficulty(Enum):
    REGULAR = "REGULAR"
    HARD = "HARD"
    EXTREME = "EXTREME"


# 成功等级中文标签，供 summary_for_agent 直接拼装  # 状态：展示映射
SUCCESS_LEVEL_LABEL = {
    SuccessLevel.CRITICAL: "大成功",
    SuccessLevel.EXTREME: "极难成功",
    SuccessLevel.HARD: "困难成功",
    SuccessLevel.REGULAR: "常规成功",
    SuccessLevel.FAILURE: "失败",
    SuccessLevel.FUMBLE: "大失败",
}


@dataclass
class CheckResult:
    """检定结果：成功等级、判定点数、生效阈值、原始目标值与十位骰明细。"""

    success_level: SuccessLevel
    roll_value: int
    threshold: int
    skill_value: int
    tens_rolls: List[int] = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return self.success_level in (
            SuccessLevel.REGULAR,
            SuccessLevel.HARD,
            SuccessLevel.EXTREME,
            SuccessLevel.CRITICAL,
        )

    @property
    def is_critical(self) -> bool:
        return self.success_level is SuccessLevel.CRITICAL

    @property
    def is_fumble(self) -> bool:
        return self.success_level is SuccessLevel.FUMBLE


# ============================================
# 难度与成功等级
# ============================================


def parse_difficulty(value) -> Difficulty:
    """把字符串/枚举归一为 Difficulty；非法值抛 ValueError。

    工具层输入为 "regular"/"hard"/"extreme"，先经此归一再进规则内核。
    """
    if isinstance(value, Difficulty):
        return value
    name = str(value).strip().upper()
    try:
        return Difficulty[name]
    except KeyError:
        raise ValueError(f"未知难度: {value}，可选 regular/hard/extreme") from None


def get_difficulty_threshold(skill_value: int, difficulty: Difficulty) -> int:
    """难度换算后的通过阈值。  # 状态：阈值换算
    REGULAR → skill ; HARD → skill//2 ; EXTREME → skill//5，向下取整且至少为 1。
    """
    if difficulty is Difficulty.HARD:
        return max(1, skill_value // 2)
    if difficulty is Difficulty.EXTREME:
        return max(1, skill_value // 5)
    return skill_value


def determine_success_level(skill_value: int, roll_value: int) -> SuccessLevel:
    """按 CoC 7th 判定成功等级（skill_value 为生效阈值）。

    先判大成功与 96-100 大失败，再按 极难→困难→常规 逐档收敛。
    """
    if roll_value == 1:
        return SuccessLevel.CRITICAL
    if roll_value >= 96 and roll_value > skill_value:
        return SuccessLevel.FUMBLE
    if roll_value <= max(1, skill_value // 5):
        return SuccessLevel.EXTREME
    if roll_value <= max(1, skill_value // 2):
        return SuccessLevel.HARD
    if roll_value <= skill_value:
        return SuccessLevel.REGULAR
    return SuccessLevel.FAILURE


# ============================================
# 检定入口
# ============================================


def _check(
    skill_value: int,
    difficulty: Difficulty,
    bonus: int,
    penalty: int,
    rng,
) -> CheckResult:
    roll = roll_with_bonus_penalty(bonus, penalty, rng)
    threshold = get_difficulty_threshold(skill_value, difficulty)
    level = determine_success_level(threshold, roll.total)
    return CheckResult(
        success_level=level,
        roll_value=roll.total,
        threshold=threshold,
        skill_value=skill_value,
        tens_rolls=roll.tens_rolls,
    )


def skill_check(
    skill_value: int,
    difficulty: Difficulty = Difficulty.REGULAR,
    bonus: int = 0,
    penalty: int = 0,
    rng: Optional[object] = None,
) -> CheckResult:
    """技能检定：掷 d100（含奖惩骰）→ 难度换算阈值 → 判定成功等级。"""
    return _check(skill_value, difficulty, bonus, penalty, rng)


def stat_check(
    stat_value: int,
    difficulty: Difficulty = Difficulty.REGULAR,
    rng: Optional[object] = None,
) -> CheckResult:
    """属性检定：目标 = 属性值本身（CoC 7th 属性已是百分比，无需 ×5）。"""
    return _check(stat_value, difficulty, 0, 0, rng)
