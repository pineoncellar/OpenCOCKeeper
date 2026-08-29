# -*- coding: utf-8 -*-
"""
@File     :   check_and_update_stats.py
@Desc     :   状态更新主工具：检定 + 数值变更 + 背包增减 三段式编排
@Note     :   纯计算永不写库，落库统一走 commit.apply_turn_change（决策 D1 合并提交）；
             规则全部走 src.rules，工具只做编排、镜像、diff 与 summary 组装
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.exceptions import (
    EntityNotFoundError,
    InvalidDiceExpressionError,
    ItemNotFoundError,
    SkillNotFoundError,
)
from ..rules import (
    Difficulty,
    SUCCESS_LEVEL_LABEL,
    TEMPORARY_INSANITY_LOSS,
    clamp_stat,
    parse_difficulty,
    resolve_check_target,
    resolve_temporary_insanity,
    roll_expression,
    skill_check,
    stat_check,
)
from ..storage.diff import empty_diff, record_inventory_change, record_numeric_change
from .schemas import StatsUpdateInput, parse_stats_update

# 数值字段 -> 上界列名（与 entities 表约定一致）  # 状态：字段映射
_NUMERIC_FIELDS = ("hp", "mp", "san")


def check_and_update_stats(
    storage, raw_input: dict, *, rng: Optional[object] = None
) -> dict:
    """三段式状态更新，返回结构化输出（含镜像、state_diff、rule_hints、summary_for_agent）。

    raw_input 为工具调用原始参数；本函数不写库，落库由协调器对返回的 state_diff 执行。
    rng 供测试注入确定骰序，缺省用真随机。
    """
    params = parse_stats_update(raw_input)
    entity = storage.get_entity(params.world_id, params.entity_id)
    if entity is None:
        raise EntityNotFoundError(f"实体不存在: {params.world_id}/{params.entity_id}")

    diff = empty_diff()
    check_block: Optional[dict] = None
    stats_changed: Dict[str, dict] = {}
    inventory_changed: Dict[str, list] = {"added": [], "removed": []}
    summary: List[str] = []

    # ── 段 A：检定 ──
    if params.skill_or_attribute:
        check_block = _do_check(params, entity, rng)
        summary.append(
            f"执行 [{params.skill_or_attribute}] 检定：出骰 "
            f"{check_block['roll_value']}/{check_block['threshold']}"
            f"（{check_block['success_level_label']}）"
        )

    # ── 段 B：硬数值（SC 表达式与 san_change 互斥已由 parse 保证）──
    if params.san_sc_expression:
        loss = _resolve_sc_loss(params, entity, rng)
        _apply_numeric(params, entity, diff, stats_changed, "san", -loss)
    if params.san_change is not None:
        _apply_numeric(params, entity, diff, stats_changed, "san", params.san_change)

    # 疯狂判定：SAN 结算后单次损失 >= 5 自动级联（官方阈值，唯一承载）
    insanity_block, suggested_tags, extra_checks, insanity_hp = _resolve_insanity_turn(
        entity, stats_changed, rng, summary
    )

    # HP 变更：发狂折半与 hp_change 合并为单次变更，保证 stats_changed/diff 一致
    hp_request = insanity_hp
    if params.hp_change is not None:
        hp_request += params.hp_change
    if hp_request:
        _apply_numeric(params, entity, diff, stats_changed, "hp", hp_request)
    if params.mp_change is not None:
        _apply_numeric(params, entity, diff, stats_changed, "mp", params.mp_change)

    for field in _NUMERIC_FIELDS:
        if field in stats_changed:
            m = stats_changed[field]
            high = int(entity.get(f"{field}_max") or 0)
            suffix = f"/{high}" if high else ""
            summary.append(
                f"{field.upper()} {m['delta']:+d}（剩余 {m['new']}{suffix}）"
            )

    # ── 段 C：背包增减 ──
    _apply_inventory(params, entity, diff, inventory_changed, summary)

    rule_hints = _build_rule_hints(entity, stats_changed)

    return {
        "ok": True,
        "entity_id": params.entity_id,
        "check": check_block,
        "stats_changed": stats_changed or None,
        "inventory_changed": (
            inventory_changed
            if (inventory_changed["added"] or inventory_changed["removed"])
            else None
        ),
        "insanity": insanity_block,
        "suggested_tags": suggested_tags,
        "extra_checks": extra_checks or None,
        "rule_hints": rule_hints or None,
        "state_diff": diff,
        "summary_for_agent": _compose_summary(params, summary),
    }


# ============================================
# 段 A：检定
# ============================================


def _do_check(params: StatsUpdateInput, entity: dict, rng) -> dict:
    """解析检定目标并执行 d100 检定，返回 check 结果块。

    状态：目标解析——显式 target_value 优先，否则走别名表；san 用当前值。
    """
    difficulty = parse_difficulty(params.difficulty)
    bonus = max(0, params.bonus_penalty_dice)
    penalty = max(0, -params.bonus_penalty_dice)

    if params.target_value is not None:
        result = skill_check(params.target_value, difficulty, bonus, penalty, rng)
    else:
        try:
            target = resolve_check_target(
                entity["attributes_and_skills"] or {}, params.skill_or_attribute
            )
        except KeyError as e:
            raise SkillNotFoundError(
                f"检定项无法解析: {params.skill_or_attribute}"
            ) from e
        if target.kind == "san":
            result = skill_check(int(entity["san"]), difficulty, bonus, penalty, rng)
        elif target.kind == "stat":
            result = stat_check(target.value, difficulty, rng)
        else:
            result = skill_check(target.value, difficulty, bonus, penalty, rng)

    return {
        "skill_or_attribute": params.skill_or_attribute,
        "roll_value": result.roll_value,
        "threshold": result.threshold,
        "success_level": result.success_level.value,
        "success_level_label": SUCCESS_LEVEL_LABEL[result.success_level],
        "is_success": result.is_success,
        "tens_rolls": result.tens_rolls,
        "bonus_penalty_dice": params.bonus_penalty_dice,
    }


# ============================================
# 段 B：硬数值
# ============================================


def _resolve_sc_loss(params: StatsUpdateInput, entity: dict, rng) -> int:
    """解析 SC 表达式并求值：先做理智检定（目标=当前 san），成功取左档失败取右档。"""
    expr = (params.san_sc_expression or "").strip()
    if "/" not in expr:
        raise InvalidDiceExpressionError(f"SC 表达式需为 成功/失败 两档: {params.san_sc_expression}")
    ok_part, fail_part = expr.split("/", 1)
    result = skill_check(int(entity["san"]), Difficulty.REGULAR, rng=rng)
    chosen = ok_part if result.is_success else fail_part
    try:
        loss = roll_expression(chosen, rng)
    except ValueError as e:
        raise InvalidDiceExpressionError(str(e)) from e
    return max(0, int(loss))


def _apply_numeric(
    params: StatsUpdateInput,
    entity: dict,
    diff: dict,
    stats_changed: Dict[str, dict],
    field: str,
    request_delta: int,
) -> None:
    """对单个数值字段做 clamp 并记录镜像与 diff。

    状态：clamp 后取实际生效量写 diff——这是 undo 能精确还原的前提。
    """
    old = int(entity[field])
    high = entity.get(f"{field}_max")
    applied, new = clamp_stat(old, request_delta, high=high)
    stats_changed[field] = {
        "old": old,
        "new": new,
        "delta": applied,
        "requested": request_delta,
    }
    record_numeric_change(diff, f"{params.entity_id}.{field}", applied)


# ============================================
# 段 C：背包
# ============================================


def _apply_inventory(
    params: StatsUpdateInput,
    entity: dict,
    diff: dict,
    inventory_changed: Dict[str, list],
    summary: List[str],
) -> None:
    """背包增删：新增须含 name；移除按 name 匹配完整 dict 记录，找不到抛错。"""
    current = list(entity["inventory"] or [])
    for item in params.items_to_add or []:
        if not isinstance(item, dict) or not item.get("name"):
            raise ValueError(f"背包新增项必须含 name: {item}")
        record_inventory_change(diff, params.entity_id, item, removed=False)
        inventory_changed["added"].append(item)
        summary.append(f"获得 {item['name']}")
    for name in params.items_to_remove or []:
        found = next((i for i in current if i.get("name") == name), None)
        if found is None:
            raise ItemNotFoundError(f"背包中不存在物品: {name}")
        record_inventory_change(diff, params.entity_id, found, removed=True)
        inventory_changed["removed"].append(name)
        summary.append(f"失去 {name}")


# ============================================
# 只读规则提示与摘要
# ============================================


def _build_rule_hints(entity: dict, stats_changed: Dict[str, dict]) -> dict:
    """机械阈值事实（决策 D5）：仅触达阈值时出现，绝不自动打 Tag。

    状态：纯规则计算——SAN 损失占比、不定疯狂占位阈值、HP 单次伤害一半的重伤阈值、HP 归零。
    注意：临时疯狂已由 insanity 结构体唯一承载（官方 5 点阈值），此处不再重复提示。
    """
    hints: Dict[str, Any] = {}
    san = stats_changed.get("san")
    if san and san["delta"] < 0:
        loss = abs(san["delta"])
        old = san["old"]
        if old > 0:
            hints["san_loss_ratio"] = round(loss / old, 3)
        san_max = int(entity.get("san_max") or 0)
        if san_max > 0:
            # 状态：不定疯狂占位——官方为单次损失 >= 当前 SAN 的 1/5，现以 san_max 近似，待后续核对
            hints["indefinite_insanity_hit"] = loss >= san_max / 5
    hp = stats_changed.get("hp")
    if hp:
        hp_max = int(entity.get("hp_max") or 0)
        if hp["requested"] < 0 and hp_max > 0:
            # 重伤按请求伤害判定（clamp 前的单次伤害量）  # 状态：伤害阈值
            hints["major_wound_hit"] = abs(hp["requested"]) >= hp_max / 2
        hints["hp_zero"] = hp["new"] == 0
    return hints


# ============================================
# 临时疯狂自动级联（决策 D5：只建议 Tag，不越权落库）
# ============================================


def _extract_int(entity: dict) -> Optional[int]:
    """从属性表解析 INT：英文缩写优先，中文别名兜底；缺失返回 None（心智保护兜底，不抛异常）。"""
    skills = entity.get("attributes_and_skills") or {}
    value = skills.get("INT")
    if value is None:
        value = skills.get("智力")
    return int(value) if value is not None else None


def _resolve_insanity_turn(
    entity: dict,
    stats_changed: Dict[str, dict],
    rng,
    summary: List[str],
):
    """SAN 结算后自动级联临时疯狂判定（官方 5 点阈值）。

    返回 (insanity_block, suggested_tags, extra_checks, insanity_hp)：
      insanity_block  疯狂契约 JSON（未达阈值时 None）
      suggested_tags  建议挂载的疯狂 Tag（D5——工具只建议，落库由 Director 经 manage_tags 决策）
      extra_checks    智力检定/抽表权威副本，供 loop 汇入 collected_checks 透传 Narrator
      insanity_hp     遍体鳞伤的 HP 折半增量（负值，段 B 与 hp_change 合并应用）
    """
    san = stats_changed.get("san")
    if not san or san["delta"] >= 0:
        return None, None, [], 0
    loss = abs(san["delta"])
    if loss < TEMPORARY_INSANITY_LOSS:
        return None, None, [], 0

    result = resolve_temporary_insanity(
        loss,
        _extract_int(entity),
        int(entity["san"]),
        int(entity["hp"]),
        rng=rng,
    )
    block = result.to_dict()
    extra_checks = list(result.checks)
    if not result.triggered:
        reason_note = {
            "no_int_stat": "实体无智力属性，心智封闭保持清醒",
            "int_protection": "智力检定失败，心智封闭保持清醒",
        }.get(result.reason, "未触发临时疯狂")
        summary.append(f"单次损失 {loss} 点理智，{reason_note}")
        return block, None, extra_checks, 0

    # 状态：触发——组装建议 Tag（D5：不落库，交 Director 决策后经 manage_tags 挂载）
    tags = ["临时性疯狂"]
    bout = result.bout_result or {}
    if bout.get("extra_kind") and bout.get("extra_name"):
        tags.append(f"{bout['extra_kind']}:{bout['extra_name']}")
    if bout.get("hp_halved"):
        tags.append("状态:遍体鳞伤")
    summary.append(f"单次损失 {loss} 点理智，陷入临时性疯狂（{bout.get('name')}）")
    return block, tags, extra_checks, -result.hp_loss


def _compose_summary(params: StatsUpdateInput, parts: List[str]) -> str:
    """拼装主 Agent 速读摘要：一段中文，覆盖本轮全部变更，无编号序列。"""
    if not parts:
        return f"实体 {params.entity_id} 无变更。"
    return f"玩家 {params.entity_id} " + "，".join(parts) + "。"
