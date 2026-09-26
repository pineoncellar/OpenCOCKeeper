# -*- coding: utf-8 -*-
"""
@File     :   combat_resolve.py
@Desc     :   战斗结算原子工具：strike 对抗攻击结算（护甲/贯穿/重伤级联）与 maneuver 战技检定
@Note     :   纯计算永不写库——骰点/成功等级/伤害全部走 src.rules.combat 内核，工具只做
             实体读取、参数编排、diff 组装与摘要；落库统一走 commit.apply_turn_change；
             反击伤害不在此结算——防守方反击命中只标记事件，由 Combat Agent 换向再调一次
             strike（attacker/target 互换），保证每个动作单向、无歧义
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..core.exceptions import EntityNotFoundError
from ..rules import (
    DEFENSE_COUNTERATTACK,
    DEFENSE_DODGE,
    DEFENSE_NONE,
    SUCCESS_LEVEL_LABEL,
    TAG_MAJOR_WOUND,
    Difficulty,
    SuccessLevel,
    maneuver_build_penalty,
    opposed_combat,
    parse_difficulty,
    resolve_check_target,
    resolve_physical_cascade,
    roll_damage_result,
    skill_check,
)
from ..storage.diff import empty_diff, record_numeric_change, record_tag_change

# 防御动作缺省技能：闪避用"闪避"技能、反击沿用攻击方近战技能（由调用方换向结算）
_DEFENSE_SKILL_DEFAULT = {
    DEFENSE_DODGE: "闪避",
    DEFENSE_COUNTERATTACK: None,  # 缺省沿用 attack_skill
}
_DEFENSE_VALUES = frozenset({DEFENSE_NONE, DEFENSE_DODGE, DEFENSE_COUNTERATTACK})
_ACTIONS = frozenset({"strike", "maneuver", "suspend"})


@dataclass
class CombatResolveInput:
    """规范化后的战斗结算输入（解析自工具调用原始参数）。"""

    world_id: str
    action: str
    attacker_id: str
    target_id: str
    attack_skill: Optional[str] = None
    defense: str = DEFENSE_NONE
    defense_skill: Optional[str] = None
    damage_expression: Optional[str] = None
    damage_bonus: str = ""
    target_armor: int = 0
    difficulty: str = "regular"
    bonus_penalty_dice: int = 0
    attacker_build: Optional[int] = None
    target_build: Optional[int] = None
    maneuver_kind: str = ""


def _require_text(value, name: str) -> str:
    """非空字符串校验与剥离，缺失抛 ValueError。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} 为必填")
    return text


def parse_combat_resolve(raw: dict) -> CombatResolveInput:
    """校验并归一化战斗结算输入；必填缺失 / 非法枚举抛 ValueError。

    注入字段（world_id）由调用方剥离后传入，模型不可见；其余参数按动作清洗。
    """
    world_id = _require_text(raw.get("world_id"), "world_id")
    action = str(raw.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"非法 action: {raw.get('action')}，可选 {sorted(_ACTIONS)}")
    attacker_id = _require_text(raw.get("attacker_id"), "attacker_id")
    target_id = _require_text(raw.get("target_id"), "target_id")
    difficulty = str(raw.get("difficulty") or "regular").strip().lower()
    parse_difficulty(difficulty)  # 状态：尽早校验非法档位
    defense = str(raw.get("defense") or DEFENSE_NONE).strip().lower()
    if defense not in _DEFENSE_VALUES:
        raise ValueError(f"非法 defense: {defense}，可选 {sorted(_DEFENSE_VALUES)}")
    return CombatResolveInput(
        world_id=world_id,
        action=action,
        attacker_id=attacker_id,
        target_id=target_id,
        attack_skill=(raw.get("attack_skill") or "").strip() or None,
        defense=defense,
        defense_skill=(raw.get("defense_skill") or "").strip() or None,
        damage_expression=(raw.get("damage_expression") or "").strip() or None,
        damage_bonus=str(raw.get("damage_bonus") or "").strip(),
        target_armor=int(raw.get("target_armor") or 0),
        difficulty=difficulty,
        bonus_penalty_dice=int(raw.get("bonus_penalty_dice") or 0),
        attacker_build=raw.get("attacker_build"),
        target_build=raw.get("target_build"),
        maneuver_kind=str(raw.get("maneuver_kind") or "").strip(),
    )


def _load_entity(storage, world_id: str, entity_id: str) -> dict:
    """按 ID 读取实体；不存在抛 EntityNotFoundError。"""
    entity = storage.get_entity(world_id, entity_id)
    if entity is None:
        raise EntityNotFoundError(f"实体不存在: {world_id}/{entity_id}")
    return entity


def _resolve_skill_value(entity: dict, name: str) -> int:
    """从实体的属性技能表解析检定目标值；无法解析抛 SkillNotFoundError 语义错误。"""
    try:
        target = resolve_check_target(entity["attributes_and_skills"] or {}, name)
    except KeyError:
        raise ValueError(f"检定项无法解析: {name}") from None
    if target.kind == "san":
        raise ValueError(f"战斗检定目标不能是理智: {name}")
    if target.value is None:
        raise ValueError(f"战斗检定目标无值: {name}")
    return int(target.value)


def _merge_damage_expr(expression: str, bonus: str) -> str:
    """伤害表达式与伤害加成（DB）合并：空/0 加成原样返回，否则拼接。

    状态：DB 以"+" 或 "-" 前缀已有符号时直接拼接，避免双符号导致解析失败。
    """
    bonus = (bonus or "").strip()
    if not bonus or bonus == "0":
        return expression
    if bonus[0] not in "+-":
        return f"{expression}+{bonus}"
    return f"{expression}{bonus}"


def _check_block(
    *,
    label: str,
    entity_id: str,
    target_value: int,
    result,
    difficulty,
    bonus_penalty_dice: int = 0,
) -> dict:
    """组装一条检定权威块（供 loop 汇入 collected_checks 透传 Narrator）。"""
    return {
        "entity_id": entity_id,
        "skill_or_attribute": label,
        "roll_value": result.roll_value,
        "threshold": result.threshold,
        "success_level": result.success_level.value,
        "success_level_label": SUCCESS_LEVEL_LABEL[result.success_level],
        "is_success": result.is_success,
        "tens_rolls": result.tens_rolls,
        "bonus_penalty_dice": bonus_penalty_dice,
        "difficulty": difficulty,
    }


# ============================================
# strike：对抗攻击结算
# ============================================


def _resolve_strike(storage, p: CombatResolveInput, rng) -> dict:
    """执行一次单向近战/远程攻击结算，返回权威区与状态 diff。

    状态：攻击与防御检定在本函数内一并投出（双方一起掷骰）——挂起阶段不预投攻击，
    玩家在声明闪避/反击前无从得知攻击成败，避免“看着骰子再决策”。
    """
    attacker = _load_entity(storage, p.world_id, p.attacker_id)
    target = _load_entity(storage, p.world_id, p.target_id)
    if p.damage_expression is None:
        raise ValueError("strike 需要 damage_expression")
    if p.attack_skill is None:
        raise ValueError("strike 需要 attack_skill")

    difficulty = parse_difficulty(p.difficulty)
    bonus = max(0, p.bonus_penalty_dice)
    penalty = max(0, -p.bonus_penalty_dice)
    atk_value = _resolve_skill_value(attacker, p.attack_skill)
    atk_result = skill_check(atk_value, difficulty, bonus, penalty, rng)
    atk_block = _check_block(
        label=p.attack_skill, entity_id=p.attacker_id,
        target_value=atk_value, result=atk_result, difficulty=p.difficulty,
        bonus_penalty_dice=p.bonus_penalty_dice,
    )

    extra_checks: List[dict] = []
    defense_name = p.defense_skill
    if p.defense == DEFENSE_COUNTERATTACK and not defense_name:
        defense_name = p.attack_skill  # 状态：反击缺省沿用攻击方技能（近战互搏）
    if defense_name is None:
        defense_name = _DEFENSE_SKILL_DEFAULT.get(p.defense) or None

    outcome: Dict[str, Any] = {}
    def_block: Optional[dict] = None
    if p.defense != DEFENSE_NONE and defense_name:
        def_value = _resolve_skill_value(target, defense_name)
        def_result = skill_check(def_value, Difficulty.REGULAR, rng=rng)
        def_block = _check_block(
            label=defense_name, entity_id=p.target_id,
            target_value=def_value, result=def_result, difficulty="regular",
        )
        extra_checks.append(def_block)
        outcome = opposed_combat(
            atk_result.success_level, def_result.success_level, p.defense
        )
    else:
        outcome = opposed_combat(atk_result.success_level, SuccessLevel.FAILURE, DEFENSE_NONE)

    diff = empty_diff()
    damage: Dict[str, Any] = {}
    physical: Dict[str, Any] = {}
    parts: List[str] = []
    if not outcome["attacker_hits"]:
        parts.append(f"未命中 {target['name']}")
        # 状态：即使攻击落空，防守方声明过的防御检定仍如实报出——
        # 大失败等后果都挂在防御检定上，不能因攻击失败而隐去
        if def_block:
            parts.append(
                f"{target['name']}的{def_block.get('skill_or_attribute')}检定 "
                f"{def_block.get('roll_value')}/{def_block.get('threshold')} "
                f"{def_block.get('success_level_label')}"
            )
        return {
            "ok": True,
            "check": atk_block,
            "extra_checks": extra_checks,
            "outcome": outcome,
            "damage": None,
            "physical": None,
            "state_diff": diff,
            "summary_for_agent": _compose_summary(p, parts),
        }

    # 状态：命中路径——伤害（含 DB 与贯穿）+ 护甲 + 重伤级联全在规则内核结算
    expr = _merge_damage_expr(p.damage_expression, p.damage_bonus)
    dmg = roll_damage_result(expr, atk_result.success_level, rng)
    damage = {
        "expression": expr,
        "rolled": dmg.rolled,
        "max_value": dmg.max_value,
        "impale": dmg.impale,
        "total": dmg.total,
    }
    hp_old = int(target["hp"] or 0)
    hp_max = int(target.get("hp_max") or 0)
    con = _extract_stat(target, "CON")
    physical = resolve_physical_cascade(
        hp_old=hp_old,
        hp_max=hp_max,
        armor=p.target_armor,
        damage_total=dmg.total,
        con=con,
        rng=rng,
        already_major_wound=TAG_MAJOR_WOUND in (target["tags"] or []),
    )
    # 状态：物理真相进 diff——HP 扣减记录实际生效量（全被甲挡时净 0 不写），
    # Tag 只补"新出现"的持久状态，避免重复打标
    if physical["hp_applied"]:
        record_numeric_change(diff, f"{p.target_id}.hp", physical["hp_applied"])
    existing_tags = set(target["tags"] or [])
    for tag in physical["tags"]:
        if tag not in existing_tags:
            record_tag_change(diff, p.target_id, tag, removed=False)
    # 状态：CON 检定并入权威区（供 Narrator 展示休克/苏醒判定的掷骰细节）
    if physical["con_check"]:
        cc = dict(physical["con_check"])
        cc["entity_id"] = p.target_id
        cc["skill_or_attribute"] = cc.pop("target", "体质")
        cc["success_level_label"] = SUCCESS_LEVEL_LABEL.get(
            cc.get("success_level"), ""
        )
        physical["con_check"] = _compact_con(cc)
        extra_checks.append(cc)

    parts.append(f"命中 {target['name']}")
    if damage["impale"]:
        parts.append(f"贯穿（满额 {damage['max_value']} + 掷骰 {damage['rolled']}）")
    parts.append(f"造成 {damage['total']} 点伤害")
    if physical["major_wound"]:
        parts.append("构成重伤")
    if physical["dying"]:
        parts.append(f"目标陷入濒死（HP 归零）")
    if physical["unconscious"]:
        parts.append("目标昏迷倒下")
    return {
        "ok": True,
        "check": atk_block,
        "extra_checks": extra_checks,
        "outcome": outcome,
        "damage": damage,
        "physical": physical,
        "state_diff": diff,
        "summary_for_agent": _compose_summary(p, parts),
    }


# ============================================
# maneuver：战技检定
# ============================================


def _resolve_maneuver(storage, p: CombatResolveInput, rng) -> dict:
    """执行一次战技检定：体格比对（拦截/惩罚骰）+ 战技技能掷骰。"""
    attacker = _load_entity(storage, p.world_id, p.attacker_id)
    _load_entity(storage, p.world_id, p.target_id)  # 状态：目标必须存在，校验即读
    if p.attack_skill is None:
        raise ValueError("maneuver 需要 maneuver_skill（经 attack_skill 传入）")
    if p.attacker_build is None or p.target_build is None:
        raise ValueError("maneuver 需要 attacker_build 与 target_build")

    build = maneuver_build_penalty(int(p.attacker_build), int(p.target_build))

    diff = empty_diff()
    parts: List[str] = []
    extra_checks: List[dict] = []
    skill = p.attack_skill
    if build["intercept"]:
        # 状态：物理拦截——体型压制使战技直接失败，不再掷骰（引擎确定性裁决）
        label = p.maneuver_kind or "战技"
        parts.append(f"体格差距过大，{label}被物理拦截")
        return {
            "ok": True,
            "check": None,
            "extra_checks": extra_checks,
            "build": build,
            "is_success": False,
            "state_diff": diff,
            "summary_for_agent": _compose_summary(p, parts),
        }

    value = _resolve_skill_value(attacker, skill)
    result = skill_check(
        value, Difficulty.REGULAR, penalty=build["penalty_dice"], rng=rng
    )
    # 状态：战技惩罚骰并入权威区，Narrator 可展示体型劣势的惩罚过程
    check = _check_block(
        label=skill, entity_id=p.attacker_id,
        target_value=value, result=result, difficulty="regular",
        bonus_penalty_dice=-build["penalty_dice"],
    )
    extra_checks.append(check)
    label = p.maneuver_kind or "战技"
    if result.is_success:
        parts.append(f"{label}命中成功")
    else:
        parts.append(f"{label}未奏效")
    return {
        "ok": True,
        "check": check,
        "extra_checks": extra_checks,
        "build": build,
        "is_success": result.is_success,
        "state_diff": diff,
        "summary_for_agent": _compose_summary(p, parts),
    }


# ============================================
# suspend：NPC 发起攻击的挂起结算（不结算防御/伤害）
# ============================================


def _resolve_suspend(storage, p: CombatResolveInput, rng) -> dict:
    """NPC 对玩家发起攻击的挂起登记：只存攻击意图，不投任何检定。

    状态：为公平起见，攻击检定推迟到玩家声明防御后与防御检定一起掷——
    若挂起时先投攻击，玩家便能先看到成败再决定闪避/反击（等于白看底牌）。
    rng 参数仅为与其它动作签名一致，本路径不消耗骰序。
    """
    _load_entity(storage, p.world_id, p.attacker_id)
    _load_entity(storage, p.world_id, p.target_id)  # 状态：双方必须存在，校验即读
    if p.damage_expression is None:
        raise ValueError("suspend 需要 damage_expression")
    if p.attack_skill is None:
        raise ValueError("suspend 需要 attack_skill")

    return {
        "ok": True,
        "check": None,
        "extra_checks": [],
        "outcome": None,
        "damage": None,
        "physical": None,
        "suspend": {
            "attacker_id": p.attacker_id,
            "target_id": p.target_id,
            "attack_skill": p.attack_skill,
            "damage_expression": p.damage_expression,
            "damage_bonus": p.damage_bonus,
            "target_armor": p.target_armor,
            "difficulty": p.difficulty,
            "bonus_penalty_dice": p.bonus_penalty_dice,
        },
        "state_diff": empty_diff(),
        "summary_for_agent": (
            f"{p.attacker_id} 的攻击已挂起，等待 {p.target_id} 先声明防御；"
            "双方检定将在玩家声明后一起掷出。"
        ),
    }


# ============================================
# 工具门面
# ============================================


def combat_resolve(
    storage, raw_input: dict, *, rng: Optional[object] = None
) -> dict:
    """战斗结算门面：按 action 分派 suspend / strike / maneuver，返回统一结构（含 state_diff）。

    raw_input 为工具调用原始参数；本函数不写库，落库由协调器对返回的 state_diff 执行。
    rng 供测试注入确定骰序，缺省用真随机。
    """
    p = parse_combat_resolve(raw_input)
    if p.action == "suspend":
        return _resolve_suspend(storage, p, rng)
    if p.action == "strike":
        return _resolve_strike(storage, p, rng)
    return _resolve_maneuver(storage, p, rng)


def build_combat_resolve_schema() -> Dict[str, Any]:
    """导出 combat_resolve 的 OpenAI Function Calling parameters JSON Schema。

    注入字段（world_id）不在此暴露，由 agent/schemas 层 _drop_keys 剔除。
    """
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["strike", "maneuver", "suspend"],
                "description": "strike 对抗攻击结算（双方一起掷骰）；maneuver 战技检定；suspend 挂起 NPC 攻击等玩家先声明防御",
            },
            "attacker_id": {"type": "string", "description": "攻击方/施技方实体 ID"},
            "target_id": {"type": "string", "description": "目标实体 ID"},
            "attack_skill": {"type": "string", "description": "攻击/战技用技能名（如 斗殴、手枪）"},
            "defense": {
                "type": "string",
                "enum": ["none", "dodge", "counterattack"],
                "description": "目标防御动作：闪避需严格大于攻击方等级，反击平局攻击方胜",
            },
            "defense_skill": {"type": "string", "description": "防御用技能名；缺省按防御动作推断"},
            "defense_skill": {"type": "string", "description": "防御用技能名；缺省按防御动作推断"},
            "damage_expression": {"type": "string", "description": "武器伤害表达式（如 1D6、2D6+2）"},
            "damage_bonus": {"type": "string", "description": "伤害加成 DB（如 1D4），空/0 不叠加"},
            "target_armor": {"type": "integer", "description": "目标护甲值，0 无护甲"},
            "difficulty": {
                "type": "string",
                "enum": ["regular", "hard", "extreme"],
                "description": "攻击检定难度（hard/extreme 以难中换取贯穿）",
            },
            "bonus_penalty_dice": {
                "type": "integer",
                "description": "奖惩骰：正=奖励（援护/优势），负=惩罚（掩体/倒地）",
            },
            "attacker_build": {"type": "integer", "description": "攻击方体格 Build（maneuver 用）"},
            "target_build": {"type": "integer", "description": "目标体格 Build（maneuver 用）"},
            "maneuver_kind": {"type": "string", "description": "战技类型描述（绊摔/擒抱/缴械等）"},
        },
        "required": ["action", "attacker_id", "target_id"],
    }


def _extract_stat(entity: dict, code: str) -> Optional[int]:
    """从实体属性表按英文缩写取属性值（CON/DEX/STR 等）；缺失返回 None 容错。"""
    skills = entity.get("attributes_and_skills") or {}
    value = skills.get(code)
    return int(value) if value is not None else None


def _compact_con(cc: dict) -> dict:
    """压缩 CON 检定块：剔除嵌套 target 键，仅保留权威区可见字段。"""
    return {
        "kind": cc.get("kind", "con_check_major_wound"),
        "entity_id": cc.get("entity_id"),
        "skill_or_attribute": cc.get("skill_or_attribute", "体质"),
        "roll_value": cc.get("roll_value"),
        "threshold": cc.get("threshold"),
        "success_level": cc.get("success_level"),
        "success_level_label": cc.get("success_level_label"),
        "is_success": cc.get("is_success"),
    }


def _compose_summary(p: CombatResolveInput, parts: List[str]) -> str:
    """拼装 Combat Agent 速读摘要：一段中文，无编号序列。"""
    if not parts:
        return f"{p.attacker_id} 对 {p.target_id} 未产生有效动作。"
    return "，".join(parts) + "。"