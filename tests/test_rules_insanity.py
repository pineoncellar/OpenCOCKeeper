# -*- coding: utf-8 -*-
"""
@File     :   test_rules_insanity.py
@Desc     :   临时疯狂规则内核单测：阈值门禁、INT 成败、总结发作抽表、衍生 HP、级联恐惧/躁狂
@Note     :   纯函数注入 SeqRng 固定骰序；骰序见 resolve_temporary_insanity 文档
"""

from src.rules.insanity import (
    TEMPORARY_INSANITY_LOSS,
    resolve_temporary_insanity,
)
from src.rules.insanity_tables import MANIAS, PHOBIAS, SUMMARY_BOUTS


class SeqRng:
    """按预设队列依次返回 randint 结果的测试桩，保证掷骰序列确定。"""

    def __init__(self, values):
        self._values = list(values)

    def randint(self, a, b):
        assert self._values, "预设骰序已耗尽"
        return self._values.pop(0)


# ============================================
# 阈值门禁
# ============================================

def test_below_threshold_not_triggered():
    # san_loss=4 < 5 直接不触发，不消耗骰序
    r = resolve_temporary_insanity(4, 70, 58, 10, rng=SeqRng([]))
    assert r.triggered is False
    assert r.reason == "below_threshold"
    assert r.checks == []


def test_threshold_boundary_exact_five():
    # san_loss=5 == 门槛：需 INT 检定；掷 30 <= 70 通过
    r = resolve_temporary_insanity(
        5, 70, 58, 10, rng=SeqRng([3, 0, 3, 4])
    )
    assert r.triggered is True
    assert r.reason == "int_understood"


def test_threshold_constant_is_five():
    assert TEMPORARY_INSANITY_LOSS == 5


# ============================================
# INT 缺失与 INT 失败（心智保护）
# ============================================

def test_no_int_stat_protection():
    # 实体无 INT：直接视为心智保护生效，不抛异常、不消耗骰序
    r = resolve_temporary_insanity(6, None, 58, 10, rng=SeqRng([]))
    assert r.triggered is False
    assert r.reason == "no_int_stat"
    assert r.to_dict()["triggered"] is False


def test_int_failure_protection():
    # INT 检定失败（70 > 70 目标）：掷 78 失败 -> 心智封闭未发狂
    r = resolve_temporary_insanity(6, 70, 58, 10, rng=SeqRng([7, 8]))
    assert r.triggered is False
    assert r.reason == "int_protection"
    assert len(r.checks) == 1
    assert r.checks[0]["skill_or_attribute"] == "智力"
    assert r.checks[0]["is_success"] is False


# ============================================
# 触发：总结发作抽表与时长
# ============================================

def test_triggered_summary_bout_and_duration():
    # INT 检定成功（30 <= 70），1D10 抽表 = 1（失忆），时长 1D10 = 4
    r = resolve_temporary_insanity(6, 70, 58, 10, rng=SeqRng([3, 0, 1, 4]))
    assert r.triggered is True
    assert r.type == "TEMPORARY"
    assert r.bout_mode == "SUMMARY"
    bout = r.bout_result
    assert bout["table_id"] == 1
    assert bout["name"] == "失忆"
    assert bout["duration_hours"] == 4
    assert bout["hp_halved"] is False
    assert r.hp_loss == 0
    assert len(r.checks) == 2  # 智力检定 + 总结发作
    assert r.checks[1]["kind"] == "bout"
    assert r.checks[1]["success_level_label"] == "抽中：失忆"


def test_table8_has_ten_entries():
    assert len(SUMMARY_BOUTS) == 10


# ============================================
# 遍体鳞伤：衍生 HP 折半
# ============================================

def test_battered_halves_hp():
    # INT 通过（30<=70），抽表 = 3（遍体鳞伤），时长 4；hp 10 -> target 5
    r = resolve_temporary_insanity(6, 70, 58, 10, rng=SeqRng([3, 0, 3, 4]))
    assert r.triggered is True
    assert r.bout_result["name"] == "遍体鳞伤"
    assert r.bout_result["hp_halved"] is True
    assert r.hp_loss == 5


def test_battered_keeps_min_one_hp():
    # hp=1 折半 -> max(1, 0) = 1，hp_loss 0（不产生空变更）
    r = resolve_temporary_insanity(6, 70, 58, 1, rng=SeqRng([3, 0, 3, 2]))
    assert r.hp_loss == 0
    assert r.bout_result["hp_halved"] is True


# ============================================
# 级联：恐惧症 / 躁狂症 1D100 抽表
# ============================================

def test_phobia_cascade():
    # INT 通过（30<=70），抽表 = 9（恐惧症），1D100 掷 48 按表 9 取条目，时长 2
    r = resolve_temporary_insanity(6, 70, 58, 10, rng=SeqRng([3, 0, 9, 48, 2]))
    assert r.triggered is True
    bout = r.bout_result
    assert bout["name"] == "恐惧症"
    assert bout["extra_kind"] == "恐惧症"
    assert bout["extra_name"] == PHOBIAS[48]
    assert r.hp_loss == 0
    assert len(r.checks) == 3  # 智力 + 总结发作 + 抽取恐惧症
    assert r.checks[2]["kind"] == "extra"
    assert r.checks[2]["roll_value"] == 48


def test_mania_cascade():
    # INT 通过（30<=70），抽表 = 10（躁狂症），1D100 掷 36 按表 10 取条目，时长 3
    r = resolve_temporary_insanity(6, 70, 58, 10, rng=SeqRng([3, 0, 10, 36, 3]))
    assert r.triggered is True
    assert r.bout_result["extra_name"] == MANIAS[36]
    assert r.bout_result["extra_kind"] == "躁狂症"
    assert len(r.checks) == 3
    assert r.checks[2]["roll_value"] == 36


def test_phobia_mania_tables_full():
    assert len(PHOBIAS) == 100
    assert len(MANIAS) == 100


# ============================================
# 序列化契约
# ============================================

def test_to_dict_omits_bout_when_not_triggered():
    r = resolve_temporary_insanity(4, 70, 58, 10, rng=SeqRng([]))
    d = r.to_dict()
    assert "bout_result" not in d
    assert d == {
        "triggered": False,
        "reason": "below_threshold",
        "type": "NONE",
        "bout_mode": "SUMMARY",
        "checks": [],
    }


def test_to_dict_contains_bout_when_triggered():
    r = resolve_temporary_insanity(6, 70, 58, 10, rng=SeqRng([3, 0, 1, 4]))
    d = r.to_dict()
    assert "bout_result" in d
    assert d["bout_result"]["name"] == "失忆"
