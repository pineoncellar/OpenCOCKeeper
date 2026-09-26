# -*- coding: utf-8 -*-
"""
@File     :   combat.py
@Desc     :   战斗规则内核（纯函数）：DEX 先攻排序、对抗成功等级比较、战技体格拦截、
             贯穿与伤害结算、重伤濒死物理级联
@Note     :   100% 确定性无 LLM、无副作用，全部支持注入 rng 保证可复现测试；
             依赖现有 checks（成功等级/检定）与 dice（掷骰）原语，不 import storage/llm；
             物理真相判定（贯穿/重伤/濒死）一律在本模块产出，杜绝 LLM 数值幻觉
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .checks import Difficulty, SuccessLevel, stat_check
from .dice import roll_expression
from .stats import clamp_stat

# 成功等级排位（从高到低）：平局/胜负比较用此序——CRITICAL 5 > EXTREME 4 >
# HARD 3 > REGULAR 2 > FAILURE 1 > FUMBLE 0
_LEVEL_RANK: Dict[SuccessLevel, int] = {
    SuccessLevel.CRITICAL: 5,
    SuccessLevel.EXTREME: 4,
    SuccessLevel.HARD: 3,
    SuccessLevel.REGULAR: 2,
    SuccessLevel.FAILURE: 1,
    SuccessLevel.FUMBLE: 0,
}

# 拔枪待击的先攻加成（CoC 7th：先手拔枪者 DEX+50，仍须通过先攻检定，引擎侧以加成近似）
READY_GUN_BONUS = 50

# 战技体格差阈值：防守者体格比攻击者大达到该值即物理拦截（碾压，战技无法施展）
BUILD_INTERCEPT_DIFF = 3

# 贯穿所需的最低成功等级：极难/大成功均触发贯穿（满额伤害 + 加骰伤害）
IMPALE_LEVELS = frozenset({SuccessLevel.CRITICAL, SuccessLevel.EXTREME})

# 防御动作枚举：闪避需严格大于攻击方等级；反击平局时攻击方占优
DEFENSE_DODGE = "dodge"
DEFENSE_COUNTERATTACK = "counterattack"
DEFENSE_NONE = "none"
DEFENSE_KINDS = frozenset({DEFENSE_DODGE, DEFENSE_COUNTERATTACK, DEFENSE_NONE})

# 物理状态 Tag（濒死/昏迷/重伤为持久伤情，战斗结束保留；由 tool 层写 state_diff）
TAG_MAJOR_WOUND = "重伤"
TAG_UNCONSCIOUS = "昏迷"
TAG_DYING = "濒死"

# ============================================
# 先攻排序
# ============================================


@dataclass(frozen=True)
class Combatant:
    """参战者先攻要素：DEX 与平局时的战斗技能值。

    ready_gun 为真表示已拔枪待击（DEX+50）；fight_skill 近战平局用、shoot_skill 远程平局用。
    """

    entity_id: str
    dex: int
    fight_skill: int = 0
    shoot_skill: int = 0
    ready_gun: bool = False


@dataclass(frozen=True)
class InitiativeEntry:
    """排序后的一名参战者（含生效 DEX 与破局信息）。"""

    entity_id: str
    dex: int                        # 原始 DEX
    effective_dex: int              # 生效 DEX（拔枪 +50）
    ordering_skill: int             # 平局用于对比的战斗技能值
    tiebreak: str = ""              # 平局破局说明（技能名/随机），供展示


def _resolve_tiebreak(
    left: Combatant, right: Combatant, ranged: bool
) -> str:
    """平局（生效 DEX 相等）时用战斗技能定序：近战比格斗、远程比射击。

    技能也相等时返回空串，由调用方随机破局。
    """
    left_skill = left.shoot_skill if ranged else left.fight_skill
    right_skill = right.shoot_skill if ranged else right.fight_skill
    if left_skill != right_skill:
        return "射击" if ranged else "格斗"
    return ""


def dexterity_order(
    combatants: List[Combatant],
    *,
    ranged: bool = False,
    rng: Optional[object] = None,
) -> List[InitiativeEntry]:
    """按生效 DEX 降序排定先攻；拔枪待击 DEX+50，平局按战斗技能对比、仍平则随机破局。

    返回全部参战者的行动顺位（高 DEX 在前）。稳定排序：技能与随机都无法区分时保持输入序。
    """
    import random as _random

    rng = rng or _random
    entries = []
    for c in combatants:
        effective = c.dex + (READY_GUN_BONUS if c.ready_gun else 0)
        skill = c.shoot_skill if ranged else c.fight_skill
        entries.append((c, effective))
    # 状态：先按生效 DEX 降序粗排，避免全量随机破局破坏技能判定的可解释性
    entries.sort(key=lambda pair: pair[1], reverse=True)
    result: List[InitiativeEntry] = []
    i = 0
    while i < len(entries):
        j = i
        # 状态：把同生效 DEX 的一段收拢，段内用战斗技能/随机破局定序
        while j + 1 < len(entries) and entries[j + 1][1] == entries[i][1]:
            j += 1
        if j == i:
            c, effective = entries[i]
            result.append(InitiativeEntry(
                entity_id=c.entity_id, dex=c.dex, effective_dex=effective,
                ordering_skill=c.shoot_skill if ranged else c.fight_skill,
            ))
        else:
            # 状态：段内按技能降序，技能仍等则随机洗牌（稳定破局保证可复现测试）
            group = [pair[0] for pair in entries[i : j + 1]]
            tie_name = _resolve_tiebreak(group[0], group[-1], ranged) or "随机"
            if tie_name != "随机":
                group.sort(
                    key=lambda c: (c.shoot_skill if ranged else c.fight_skill),
                    reverse=True,
                )
            else:
                rng.shuffle(group)
            # 状态：随机破局的段内顺位反写解释字段，其余段内顺位即技能序
            keep_skill = tie_name != "随机"
            for idx, c in enumerate(group):
                result.append(InitiativeEntry(
                    entity_id=c.entity_id, dex=c.dex,
                    effective_dex=c.dex + (READY_GUN_BONUS if c.ready_gun else 0),
                    ordering_skill=c.shoot_skill if ranged else c.fight_skill,
                    tiebreak=tie_name if (idx == 0 or not keep_skill) else "",
                ))
        i = j + 1
    return result


# ============================================
# 对抗成功等级比较
# ============================================


def compare_success_levels(left: SuccessLevel, right: SuccessLevel) -> int:
    """两个成功等级相对高低：left 高于 right 回 1、相等回 0、低于回 -1。"""
    lr, rr = _LEVEL_RANK[left], _LEVEL_RANK[right]
    return (lr > rr) - (lr < rr)


def opposed_combat(
    attacker_level: SuccessLevel,
    defender_level: SuccessLevel,
    defense: str = DEFENSE_NONE,
) -> Dict[str, Any]:
    """结算一次近战对抗：攻击方成功等级 vs 防守方成功等级，按防御动作定命中断点。

    defense 取值 dodge / counterattack / none：
      dodge          闪避——防守方成功等级严格大于攻击方等级才闪开，平级或更低被击中
      counterattack  反击——平局攻击方占优，防守方须严格大于才顶掉攻击并命中攻击方
      none           无防御——目标未反应，攻击成功即命中（忽略防守方等级）
    返回 dict：attacker_hits / defender_evades / counter_hits / 各侧等级与失败标记。
    """
    if defense not in DEFENSE_KINDS:
        raise ValueError(f"未知防御动作: {defense}，可选 {sorted(DEFENSE_KINDS)}")
    atk_rank = _LEVEL_RANK[attacker_level]
    attacker_failed = attacker_level in (SuccessLevel.FAILURE, SuccessLevel.FUMBLE)
    attacker_fumbled = attacker_level is SuccessLevel.FUMBLE
    defender_fumbled = defender_level is SuccessLevel.FUMBLE
    out: Dict[str, Any] = {
        "attacker_level": attacker_level.value,
        "defender_level": defender_level.value,
        "defense": defense,
        "attacker_hits": False,
        "defender_evades": False,
        "counter_hits": False,
        "attacker_failed": attacker_failed,
        "attacker_fumbled": attacker_fumbled,
        "defender_fumbled": defender_fumbled,
    }
    if attacker_failed:
        return out
    # 状态：攻击成功路径——按防御动作二分；counterattack 时防守方严格大于=反击命中
    if defense == DEFENSE_NONE:
        out["attacker_hits"] = True
        return out
    if defense == DEFENSE_DODGE:
        cmp = compare_success_levels(defender_level, attacker_level)
        out["defender_evades"] = cmp > 0
        out["attacker_hits"] = not out["defender_evades"]
        return out
    cmp = compare_success_levels(defender_level, attacker_level)
    out["counter_hits"] = cmp > 0
    out["attacker_hits"] = not out["counter_hits"]
    return out


# ============================================
# 战技体格拦截
# ============================================


def maneuver_build_penalty(
    attacker_build: int, defender_build: int
) -> Dict[str, Any]:
    """战技体格比对：防守者体格远大于攻击者时物理拦截，体型占优方吃惩罚骰。

    diff = 防守者体格 - 攻击者体格：
      防守者大 >= 3        物理拦截（战技施展即被压制，不可破解）
      防守者大 1~2         攻击者吃 abs(diff) 个惩罚骰（体型劣势）
      其余                 无拦截无惩罚（攻击者体格占优或对等）
    """
    diff = defender_build - attacker_build
    if diff >= BUILD_INTERCEPT_DIFF:
        return {"intercept": True, "penalty_dice": 0,
                "difficulty_note": "体型差距过大，战技被物理拦截"}
    if diff in (1, 2):
        return {"intercept": False, "penalty_dice": diff,
                "difficulty_note": "体型劣势，承受惩罚骰"}
    return {"intercept": False, "penalty_dice": 0,
            "difficulty_note": "体型占优或对等，无额外惩罚"}


# ============================================
# 贯穿与伤害结算
# ============================================

_DIE_PART = re.compile(r"^(\d+)[D](\d+)$")


def expression_max(expression: str) -> int:
    """伤害表达式的最大可能值（贯穿满额伤害用）：1D6→6、2D6+2→14、1D6+1D4→10。

    非法表达式抛 ValueError，与 roll_expression 口径一致。
    """
    expr = (expression or "").strip().upper()
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
        match = _DIE_PART.match(part)
        if match:
            n, sides = int(match.group(1)), int(match.group(2))
            total += sign * n * sides
        else:
            try:
                total += sign * int(part)
            except ValueError:
                raise ValueError(f"无效的伤害表达式: {expression}") from None
    return total


@dataclass(frozen=True)
class DamageResult:
    """一次命中伤害结算：常规掷骰 + 贯穿满额 + 合计。

    impale 为真（极难/大成功）时 CoC 7th 贯穿=最大伤害 + 加骰伤害，total 即两者之和。
    """

    expression: str
    rolled: int          # 常规掷骰结果
    max_value: int       # 表达式上限（贯穿基准）
    impale: bool
    total: int           # 未贯穿=rolled，贯穿=rolled+max_value


def roll_damage_result(
    expression: str,
    success_level: SuccessLevel,
    rng: Optional[object] = None,
) -> DamageResult:
    """按命中成功等级结算伤害：极难/大成功贯穿（满额+加骰），其余常规掷骰。"""
    rolled = roll_expression(expression, rng)
    max_value = expression_max(expression)
    impale = success_level in IMPALE_LEVELS
    total = (rolled + max_value) if impale else rolled
    return DamageResult(
        expression=expression, rolled=rolled, max_value=max_value,
        impale=impale, total=total,
    )


# ============================================
# 重伤与濒死物理级联
# ============================================


def _con_block(con: int, rng) -> Optional[dict]:
    """重伤复苏的 CON 困难检定权威块（无 CON 属性时返回 None，心智/体质缺失不中断）。"""
    if con is None:
        return None
    result = stat_check(int(con), Difficulty.HARD, rng=rng)
    return {
        "kind": "con_check_major_wound",
        "target": "天性体质",
        "roll_value": result.roll_value,
        "threshold": result.threshold,
        "success_level": result.success_level.value,
        "success_level_label": None,  # 由 tool 层经 SUCCESS_LEVEL_LABEL 补齐
        "is_success": result.is_success,
    }


def resolve_physical_cascade(
    *,
    hp_old: int,
    hp_max: int,
    armor: int,
    damage_total: int,
    con: Optional[int],
    rng: Optional[object] = None,
    already_major_wound: bool = False,
) -> Dict[str, Any]:
    """一次命中伤害的物理级联：护甲吸收 → 重伤判定 → CON 困难检定 → HP 扣减 → 濒死导出。

    重伤基准 = 扣甲前骰点伤害 >= 最大 HP 的一半（更致命、贯穿易触发），但护甲全挡
    （净伤为 0）时不判重伤；HP 归零时若本次为重伤或此前已受重伤则濒死，否则单纯昏迷。
    返回 dict 含 net_damage / hp 镜像 / major_wound / con_check 权威块 / 状态 Tag。
    """
    armor_abs = min(max(0, armor), max(0, damage_total))
    net = max(0, damage_total) - armor_abs
    major = net > 0 and hp_max > 0 and damage_total >= hp_max / 2
    con_check = None
    unconscious = False
    if major:
        con_check = _con_block(con, rng)
        if con_check is not None and not con_check["is_success"]:
            unconscious = True
    applied, hp_new = clamp_stat(hp_old, -net, low=0, high=hp_max)
    dying = False
    if hp_new <= 0:
        if major or already_major_wound:
            dying = True
        else:
            unconscious = True
    # 状态：持久伤情 Tag 只建议给"确实命中"的结果，未命中/全被甲挡不产出
    # 状态：持久伤情 Tag 只建议给"确实命中"的结果，未命中/全被甲挡不产出；
    # 濒死者视为昏迷倒下（unconscious 一并置真，语义=失去行动能力但可急救）
    tags: List[str] = []
    if major:
        tags.append(TAG_MAJOR_WOUND)
    if dying:
        tags.append(TAG_DYING)
    if unconscious or dying:
        tags.append(TAG_UNCONSCIOUS)
    return {
        "armor_abs": armor_abs,
        "net_damage": net,
        "hp_old": hp_old,
        "hp_new": hp_new,
        "hp_applied": applied,
        "major_wound": major,
        "unconscious": unconscious,
        "dying": dying,
        "tags": tags,
        "con_check": con_check,
    }