# -*- coding: utf-8 -*-
"""
@File     :   test_tool_check_and_update_stats.py
@Desc     :   状态更新工具单测：三段编排、clamp 与 diff 一致性、SC 表达式、rule_hints、异常
@Note     :   复用 conftest 的 storage/world_id 隔离；rng 注入确定骰序
"""

import pytest

from src.core.exceptions import (
    ConflictingInputError,
    EmptyUpdateError,
    EntityNotFoundError,
    ItemNotFoundError,
    SkillNotFoundError,
)
from src.tools import check_and_update_stats


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)


@pytest.fixture
def entity(storage, world_id):
    return storage.create_entity(
        world_id,
        "player_01",
        "PC",
        "玩家一号",
        hp=10,
        hp_max=12,
        mp=5,
        mp_max=6,
        san=58,
        san_max=70,
        attributes_and_skills={"侦查": 60, "STR": 50},
        inventory=[{"name": "手电筒", "quantity": 1}],
        tags=[],
    )


def _call(storage, entity, **kwargs):
    rng = kwargs.pop("rng", None)
    return check_and_update_stats(
        storage,
        {"world_id": entity["world_id"], "entity_id": entity["id"], **kwargs},
        rng=rng,
    )


# ============================================
# 段 A：检定
# ============================================

def test_check_only_skill(storage, entity):
    out = _call(storage, entity, skill_or_attribute="侦查", rng=SeqRng([4, 5]))
    check = out["check"]
    assert check["success_level"] == "REGULAR"  # 45 > 30(半值) 且 ≤ 60
    assert check["roll_value"] == 45
    assert check["threshold"] == 60
    assert check["is_success"] is True
    assert "侦查" in out["summary_for_agent"]


def test_check_stat_via_alias(storage, entity):
    # STR=50 属性检定，目标 = 属性值本身；掷 37 成功
    out = _call(storage, entity, skill_or_attribute="力量", rng=SeqRng([3, 7]))
    assert out["check"]["threshold"] == 50
    assert out["check"]["is_success"] is True


def test_check_san_uses_current_san(storage, entity):
    # 理智检定目标 = 当前 san 58；掷 60 失败
    out = _call(storage, entity, skill_or_attribute="理智", rng=SeqRng([6, 0]))
    assert out["check"]["threshold"] == 58
    assert out["check"]["is_success"] is False


def test_check_hard_difficulty(storage, entity):
    # 侦查 60 困难阈值 30；掷 45 失败
    out = _call(
        storage, entity, skill_or_attribute="侦查", difficulty="hard", rng=SeqRng([4, 5])
    )
    assert out["check"]["threshold"] == 30
    assert out["check"]["is_success"] is False


# ============================================
# 段 B：硬数值与 clamp
# ============================================

def test_numeric_change_and_diff(storage, entity):
    out = _call(storage, entity, hp_change=-3)
    assert out["stats_changed"]["hp"] == {"old": 10, "new": 7, "delta": -3, "requested": -3}
    assert out["state_diff"]["numeric_changes"] == {"player_01.hp": -3}


def test_numeric_clamp_applied_delta(storage, entity):
    storage.adjust_stat(entity["world_id"], entity["id"], "hp", -8)  # hp → 2
    out = _call(storage, entity, hp_change=-5)
    assert out["stats_changed"]["hp"]["old"] == 2
    assert out["stats_changed"]["hp"]["new"] == 0
    assert out["stats_changed"]["hp"]["delta"] == -2  # 实际生效量而非请求量
    assert out["state_diff"]["numeric_changes"] == {"player_01.hp": -2}


def test_numeric_high_unbounded_when_max_zero(storage, entity):
    # 决策 D6：mp_max=6 存在；换用 hp_max=0 的场景验证放行——直接建无上限实体
    e2 = storage.create_entity(
        entity["world_id"], "npc_01", "NPC", "无名", hp=5, hp_max=0
    )
    out = _call(storage, e2, hp_change=5)
    assert out["stats_changed"]["hp"]["new"] == 10  # 无上界不截断


def test_sc_expression_success_branch(storage, entity):
    # 理智检定成功（30 ≤ 58）→ 取左档 1
    out = _call(storage, entity, san_sc_expression="1/1d3", rng=SeqRng([3, 0]))
    assert out["stats_changed"]["san"] == {"old": 58, "new": 57, "delta": -1, "requested": -1}


def test_sc_expression_fail_branch_rolls_dice(storage, entity):
    # 理智检定失败（60 > 58）→ 取右档 1d3；骰序：检定(6,0)=60 失败，1d3 掷 2
    out = _call(storage, entity, san_sc_expression="1/1d3", rng=SeqRng([6, 0, 2]))
    assert out["stats_changed"]["san"]["delta"] == -2


# ============================================
# 段 C：背包
# ============================================

def test_inventory_add_and_remove(storage, entity):
    out = _call(
        storage,
        entity,
        items_to_add=[{"name": "钥匙", "quantity": 1}],
        items_to_remove=["手电筒"],
    )
    assert [i["name"] for i in out["inventory_changed"]["added"]] == ["钥匙"]
    assert out["inventory_changed"]["removed"] == ["手电筒"]
    inv_diff = out["state_diff"]["inventory"]["player_01"]
    assert [i["name"] for i in inv_diff["added"]] == ["钥匙"]
    assert [i["name"] for i in inv_diff["removed"]] == ["手电筒"]


def test_inventory_remove_missing_raises(storage, entity):
    with pytest.raises(ItemNotFoundError):
        _call(storage, entity, items_to_remove=["不存在的东西"])


# ============================================
# 三段组合与 rule_hints
# ============================================

def test_combined_sections(storage, entity):
    out = _call(
        storage,
        entity,
        skill_or_attribute="侦查",
        hp_change=-6,
        san_change=-12,
        rng=SeqRng([2, 3]),
    )
    assert out["check"]["is_success"] is True
    assert out["stats_changed"]["hp"]["delta"] == -6
    assert out["stats_changed"]["san"]["delta"] == -12
    assert out["state_diff"]["numeric_changes"] == {
        "player_01.hp": -6,
        "player_01.san": -12,
    }


def test_rule_hints_sanity_threshold(storage, entity):
    # san 58，-12 → 12 >= 5 触发疯狂判定；实体无 INT → 心智保护未发狂
    out = _call(storage, entity, san_change=-12)
    hints = out["rule_hints"]
    # 状态：临时疯狂阈值已由 insanity 结构体唯一承载，rule_hints 不再重复
    assert "temporary_insanity_hit" not in hints
    assert hints["indefinite_insanity_hit"] is False  # 12 < 70/5=14
    assert hints["san_loss_ratio"] == pytest.approx(round(12 / 58, 3))
    # 疯狂判定：触发门槛已达，但实体无 INT → 心智保护
    assert out["insanity"]["triggered"] is False
    assert out["insanity"]["reason"] == "no_int_stat"
    assert out["suggested_tags"] is None


def test_rule_hints_major_wound(storage, entity):
    # hp 10，-6 → 6 >= hp_max/2=6 → 重伤命中；hp 剩 4 未归零
    out = _call(storage, entity, hp_change=-6)
    hints = out["rule_hints"]
    assert hints["major_wound_hit"] is True
    assert hints["hp_zero"] is False


def test_rule_hints_hp_zero(storage, entity):
    out = _call(storage, entity, hp_change=-10)
    assert out["rule_hints"]["hp_zero"] is True


# ============================================
# 疯狂自动级联（官方 5 点阈值）
# ============================================

def _int_entity(storage, entity):
    """带 INT 属性的副本实体（原 fixture 无 INT，走心智保护兜底）。"""
    return storage.create_entity(
        entity["world_id"],
        "player_02",
        "PC",
        "有智玩家",
        hp=10,
        hp_max=12,
        mp=5,
        mp_max=6,
        san=58,
        san_max=70,
        attributes_and_skills={"侦查": 60, "INT": 70},
        inventory=[],
        tags=[],
    )


def test_san_loss_triggers_insanity_and_battered_hp(storage, entity):
    # san -12 >= 5：INT 检定成功（30<=70）→ 抽表 3 遍体鳞伤 → hp 折半 10→5
    e2 = _int_entity(storage, entity)
    out = _call(storage, e2, san_change=-12, rng=SeqRng([3, 0, 3, 4]))
    assert out["insanity"]["triggered"] is True
    assert out["insanity"]["type"] == "TEMPORARY"
    assert out["insanity"]["bout_result"]["name"] == "遍体鳞伤"
    assert out["insanity"]["bout_result"]["duration_hours"] == 4
    assert out["stats_changed"]["san"]["delta"] == -12
    assert out["stats_changed"]["hp"] == {"old": 10, "new": 5, "delta": -5, "requested": -5}
    assert out["suggested_tags"] == ["临时性疯狂", "状态:遍体鳞伤"]
    # 权威区：智力检定 + 总结发作抽签
    assert len(out["extra_checks"]) == 2
    assert out["extra_checks"][0]["skill_or_attribute"] == "智力"
    assert out["extra_checks"][1]["kind"] == "bout"


def test_san_loss_int_failure_protection(storage, entity):
    # INT 检定失败（78 > 70）→ 心智封闭未发狂，HP 不折半
    e2 = _int_entity(storage, entity)
    out = _call(storage, e2, san_change=-12, rng=SeqRng([7, 8]))
    assert out["insanity"]["triggered"] is False
    assert out["insanity"]["reason"] == "int_protection"
    assert out["suggested_tags"] is None
    assert "hp" not in out["stats_changed"]


def test_san_loss_below_threshold_no_insanity(storage, entity):
    # san -3 < 5 不触发疯狂，不产出额外检定
    out = _call(storage, entity, san_change=-3)
    assert out["insanity"] is None
    assert out["suggested_tags"] is None
    assert out["extra_checks"] is None


def test_san_loss_phobia_cascade_suggested_tag(storage, entity):
    # INT 成功（30<=70）→ 抽表 9 恐惧症 → 级联 1D100=48 抽中某恐惧症
    from src.rules.insanity_tables import PHOBIAS

    e2 = _int_entity(storage, entity)
    out = _call(storage, e2, san_change=-12, rng=SeqRng([3, 0, 9, 48, 2]))
    bout = out["insanity"]["bout_result"]
    assert bout["name"] == "恐惧症"
    assert bout["extra_name"] == PHOBIAS[48]
    assert out["suggested_tags"] == ["临时性疯狂", f"恐惧症:{PHOBIAS[48]}"]
    # 权威区：智力 + 总结发作 + 级联抽取
    assert len(out["extra_checks"]) == 3
    assert out["extra_checks"][2]["kind"] == "extra"


def test_san_loss_mania_cascade_suggested_tag(storage, entity):
    from src.rules.insanity_tables import MANIAS

    e2 = _int_entity(storage, entity)
    out = _call(storage, e2, san_change=-12, rng=SeqRng([3, 0, 10, 36, 3]))
    assert out["insanity"]["bout_result"]["extra_name"] == MANIAS[36]
    assert out["suggested_tags"] == ["临时性疯狂", f"躁狂症:{MANIAS[36]}"]


def test_sc_expression_loss_triggers_insanity(storage, entity):
    # SC 失败档 1d10 掷 9：理智检定失败（60>58）→ 损失 9 ≥ 5 → 触发疯狂
    e2 = _int_entity(storage, entity)
    out = _call(
        storage, e2, san_sc_expression="0/1d10", rng=SeqRng([6, 0, 9, 3, 0, 1, 4])
    )
    assert out["stats_changed"]["san"]["delta"] == -9
    assert out["insanity"]["triggered"] is True
    assert out["insanity"]["bout_result"]["name"] == "失忆"


def test_insanity_keeps_diff_rollback_consistent(storage, entity):
    # 疯狂折半 HP 与 diff 一致（回档取反可精确还原）
    e2 = _int_entity(storage, entity)
    out = _call(storage, e2, san_change=-12, rng=SeqRng([3, 0, 3, 4]))
    assert out["state_diff"]["numeric_changes"] == {
        "player_02.san": -12,
        "player_02.hp": -5,
    }


# ============================================
# 异常路径
# ============================================

def test_empty_update_raises(storage, entity):
    with pytest.raises(EmptyUpdateError):
        _call(storage, entity)


def test_unknown_skill_raises(storage, entity):
    with pytest.raises(SkillNotFoundError):
        _call(storage, entity, skill_or_attribute="聆听")


def test_sc_conflict_raises(storage, entity):
    with pytest.raises(ConflictingInputError):
        _call(storage, entity, san_change=-1, san_sc_expression="1/1d3")


def test_missing_entity_raises(storage, world_id):
    with pytest.raises(EntityNotFoundError):
        check_and_update_stats(
            storage,
            {"world_id": world_id, "entity_id": "ghost_99", "hp_change": -1},
        )


def test_tool_never_writes_db(storage, entity):
    # 纯计算契约：调用后库内数值与背包均未变（写库只走协调器）
    _call(storage, entity, hp_change=-3, items_to_add=[{"name": "钥匙"}], rng=SeqRng([2, 3]))
    fresh = storage.get_entity(entity["world_id"], entity["id"])
    assert fresh["hp"] == 10
    assert fresh["inventory"] == [{"name": "手电筒", "quantity": 1}]
