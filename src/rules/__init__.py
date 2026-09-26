# -*- coding: utf-8 -*-
"""
@File     :   __init__.py
@Desc     :   规则内核包导出：掷骰、检定、数值边界与检定项解析
@Note     :   纯函数零 IO，不 import storage/llm；任何规则判定必须走本包，杜绝与 Prompt 混用
"""

from .checks import (
    CheckResult,
    Difficulty,
    SuccessLevel,
    SUCCESS_LEVEL_LABEL,
    determine_success_level,
    get_difficulty_threshold,
    parse_difficulty,
    skill_check,
    stat_check,
)
from .combat import (
    Combatant,
    DamageResult,
    DEFENSE_COUNTERATTACK,
    DEFENSE_DODGE,
    DEFENSE_KINDS,
    DEFENSE_NONE,
    InitiativeEntry,
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
from .dice import D100Roll, roll_d100, roll_expression, roll_ndn, roll_with_bonus_penalty
from .insanity import InsanityResult, TEMPORARY_INSANITY_LOSS, resolve_temporary_insanity
from .stats import CheckTarget, clamp_stat, normalize_target_name, resolve_check_target

__all__ = [
    # checks
    "CheckResult",
    "Difficulty",
    "SuccessLevel",
    "SUCCESS_LEVEL_LABEL",
    "determine_success_level",
    "get_difficulty_threshold",
    "parse_difficulty",
    "skill_check",
    "stat_check",
    # combat
    "Combatant",
    "DamageResult",
    "DEFENSE_COUNTERATTACK",
    "DEFENSE_DODGE",
    "DEFENSE_KINDS",
    "DEFENSE_NONE",
    "InitiativeEntry",
    "TAG_DYING",
    "TAG_MAJOR_WOUND",
    "TAG_UNCONSCIOUS",
    "compare_success_levels",
    "dexterity_order",
    "expression_max",
    "maneuver_build_penalty",
    "opposed_combat",
    "resolve_physical_cascade",
    "roll_damage_result",
    # dice
    "D100Roll",
    "roll_d100",
    "roll_expression",
    "roll_ndn",
    "roll_with_bonus_penalty",
    # insanity
    "InsanityResult",
    "TEMPORARY_INSANITY_LOSS",
    "resolve_temporary_insanity",
    # stats
    "CheckTarget",
    "clamp_stat",
    "normalize_target_name",
    "resolve_check_target",
]
