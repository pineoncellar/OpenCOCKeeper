# -*- coding: utf-8 -*-
"""
@File     :   insanity.py
@Desc     :   临时疯狂计算流：SAN 损失阈值门禁 -> 智力保护检定 -> 总结发作抽表 -> 衍生 HP 扣减
@Note     :   纯函数零 LLM，所有掷骰注入 rng 保证可复现测试；只计算不落库——
             SAN/HP 变更由工具层经 _apply_numeric 记入 state_diff，Tag 落库由 Director 决策
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .checks import SUCCESS_LEVEL_LABEL, stat_check
from .dice import roll_ndn
from .insanity_tables import MANIAS, PHOBIAS, SUMMARY_BOUTS

# 临时疯狂门槛：单次理智损失 >= 5 点触发（CoC 7th 官方规则，取代旧的按比例提示）
TEMPORARY_INSANITY_LOSS = 5

# 总结发作抽表里会级联 1D100 的两个条目名
_PHOBIA_NAME = "恐惧症"
_MANIA_NAME = "躁狂症"
# 总结发作里唯一触发 HP 折半的条目名
_BATTERED_NAME = "遍体鳞伤"


# ====================================================================
# 判定结果结构
# ====================================================================


@dataclass
class InsanityResult:
    """临时疯狂判定结果（纯计算产物，供工具层打包与叙事层透传）。

    triggered=False 时 reason 区分三种成因：
      below_threshold  单次损失不足 5 点，未达触发门槛
      no_int_stat      实体无 INT 属性，视为心智保护生效（不抛异常）
      int_protection   智力检定失败，心智封闭保持清醒
    checks 携带 INT 检定与总结发作抽表的权威副本，供工具层汇入 collected_checks；
    hp_loss 仅"遍体鳞伤"时为正（当前 HP 与目标折半 HP 之差），供工具层应用 HP 变更。
    """

    triggered: bool
    reason: str = "none"
    type: str = "NONE"  # 触发时为 "TEMPORARY"
    bout_mode: str = "SUMMARY"
    bout_result: Optional[Dict[str, Any]] = None
    checks: List[Dict[str, Any]] = field(default_factory=list)
    hp_loss: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """序列化为对外契约 JSON：仅含可见键，bout_result 未触发时省略。"""
        data: Dict[str, Any] = {
            "triggered": self.triggered,
            "reason": self.reason,
            "type": self.type,
            "bout_mode": self.bout_mode,
            "checks": self.checks,
        }
        if self.bout_result is not None:
            data["bout_result"] = self.bout_result
        return data


# ====================================================================
# 临时疯狂判定主函数
# ====================================================================


def resolve_temporary_insanity(
    san_loss: int,
    int_val: Optional[int],
    current_san: int,
    current_hp: int,
    rng: Optional[object] = None,
) -> InsanityResult:
    """CoC 7th 临时疯狂判定：损失阈值 -> 智力保护 -> 总结发作抽表 -> 衍生 HP。

    骰序（确定性测试依赖此顺序）：
      先智力检定 d100（十位/个位两骰），成功才掷 1D10 抽表 8；
      抽中恐惧/躁狂再掷 1D100 级联查表 9/10；最后掷 1D10 定持续小时数。
    current_san 当前未参与计算（官方为固定 5 点阈值），保留供后续扩展不定疯狂时使用。
    """
    if san_loss < TEMPORARY_INSANITY_LOSS:
        return InsanityResult(triggered=False, reason="below_threshold")
    if int_val is None:
        return InsanityResult(triggered=False, reason="no_int_stat")

    int_result = stat_check(int_val, rng=rng)
    int_check = {
        "skill_or_attribute": "智力",
        "roll_value": int_result.roll_value,
        "threshold": int_result.threshold,
        "success_level": int_result.success_level.value,
        "success_level_label": SUCCESS_LEVEL_LABEL[int_result.success_level],
        "is_success": int_result.is_success,
        "tens_rolls": int_result.tens_rolls,
        "bonus_penalty_dice": 0,
        "kind": "int",
    }
    if not int_result.is_success:
        return InsanityResult(triggered=False, reason="int_protection", checks=[int_check])

    # 状态：智力检定通过——理解恐怖，掷 1D10 抽取表 8 总结发作
    bout_value = roll_ndn(1, 10, rng)[-1]
    bout = SUMMARY_BOUTS[bout_value]
    bout_result: Dict[str, Any] = {
        "table_id": bout_value,
        "name": bout["name"],
        "narrative_hint": bout["narrate"],
        "hp_halved": bout["name"] == _BATTERED_NAME,
    }
    bout_check = {
        "skill_or_attribute": "总结发作",
        "roll_value": bout_value,
        "threshold": 10,
        "success_level_label": f"抽中：{bout['name']}",
        "kind": "bout",
    }
    checks: List[Dict[str, Any]] = [int_check, bout_check]
    hp_loss = 0

    # 状态：恐惧/躁狂级联掷 1D100 查表 9/10，遍体鳞伤算 HP 折半（时长最后掷，保证骰序固定）
    if bout["name"] == _PHOBIA_NAME:
        extra_value = roll_ndn(1, 100, rng)[-1]
        extra_name = PHOBIAS[extra_value]
        bout_result["extra_kind"] = _PHOBIA_NAME
        bout_result["extra_name"] = extra_name
        checks.append(
            {
                "skill_or_attribute": f"抽取{_PHOBIA_NAME}",
                "roll_value": extra_value,
                "threshold": 100,
                "success_level_label": f"抽中：{extra_name}",
                "kind": "extra",
            }
        )
    elif bout["name"] == _MANIA_NAME:
        extra_value = roll_ndn(1, 100, rng)[-1]
        extra_name = MANIAS[extra_value]
        bout_result["extra_kind"] = _MANIA_NAME
        bout_result["extra_name"] = extra_name
        checks.append(
            {
                "skill_or_attribute": f"抽取{_MANIA_NAME}",
                "roll_value": extra_value,
                "threshold": 100,
                "success_level_label": f"抽中：{extra_name}",
                "kind": "extra",
            }
        )
    elif bout["name"] == _BATTERED_NAME:
        # 状态：遍体鳞伤——HP 折半（至少保留 1 点），hp_loss 交工具层经 _apply_numeric 应用
        target_hp = max(1, current_hp // 2)
        hp_loss = max(0, current_hp - target_hp)

    # 状态：最后掷 1D10 确定持续小时数（骰序：INT -> 表8 -> 级联表9/10 -> 时长）
    duration = roll_ndn(1, 10, rng)[-1]
    bout_result["duration_hours"] = duration
    # 状态：时长并入总结发作权威条目，Narrator 按【检定结果权威区】原样报幕
    bout_check["duration_hours"] = duration

    return InsanityResult(
        triggered=True,
        reason="int_understood",
        type="TEMPORARY",
        bout_result=bout_result,
        checks=checks,
        hp_loss=hp_loss,
    )
