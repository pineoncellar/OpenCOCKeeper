# -*- coding: utf-8 -*-
"""
@File     :   stats.py
@Desc     :   数值边界 clamp 与检定项解析（属性/技能/理智 别名映射）
@Note     :   clamp 返回实际生效增量，保证 state_diff 与 undo 取反精确对齐；
             属性表允许中英文并存，靠别名表归一后再查
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# 属性中文 → 英文缩写（别名归一用；八属性 + 幸运）
ATTRIBUTE_ALIASES = {
    "力量": "STR",
    "体质": "CON",
    "体型": "SIZ",
    "敏捷": "DEX",
    "外貌": "APP",
    "智力": "INT",
    "意志": "POW",
    "教育": "EDU",
    "幸运": "LUCK",
}
ATTRIBUTE_CODES = frozenset(ATTRIBUTE_ALIASES.values())  # STR/CON/.../LUCK
# 理智同义词（统一归一到 "理智"，检定目标 = 当前 san 值）
SAN_ALIASES = frozenset(
    {"理智", "理智值", "SAN", "SAN值", "SANITY", "心智", "疯狂值"}
)
_INVERSE_ATTRIBUTES = {code: cn for cn, code in ATTRIBUTE_ALIASES.items()}
# 未写在角色卡上的技能默认值（CoC 7th 语言类技能给基础值；如《红蔷薇之馆》读
# 《阿特拉克-纳克亚纸草》需拉丁文检定，而预设卡未写，按默认 5 仍可检定）
SKILL_DEFAULTS = {
    "拉丁文": 5,
    "拉丁语": 5,
    "图书馆": 5,
    "图书馆使用": 5,
}


# ============================================
# 数值边界
# ============================================


def clamp_stat(
    current: int,
    delta: int,
    low: int = 0,
    high: Optional[int] = None,
) -> Tuple[int, int]:
    """clamp 数值增量，返回 (实际生效增量, 新值)。

    high 为 None 或 <=0 时视为不设上界（决策 D6：*_max 未初始化放行）；
    关键语义：applied_delta 才是 state_diff 应记录的量，否则 undo 取反无法还原。
      例：current=2, delta=-5, high=12 → (-2, 0)
           current=10, delta=+5, high=12 → (+2, 12)
    """
    new_value = current + delta
    applied = delta
    if new_value < low:
        applied = low - current
        new_value = low
    elif high and high > 0 and new_value > high:
        applied = high - current
        new_value = high
    return applied, new_value


# ============================================
# 检定项解析
# ============================================


@dataclass(frozen=True)
class CheckTarget:
    """解析出的检定目标。

    kind ∈ {"san","stat","skill"}；value 为属性/技能值，san 时为 None（由调用方取实体 san）。
    """

    kind: str
    key: str
    value: Optional[int] = None


def normalize_target_name(name: str) -> Tuple[str, str]:
    """把输入名归一为 (kind, key)（不查值）。

    状态：别名归一——中文属性→缩写、理智同义词→理智、其余按技能名原样
    """
    raw = (name or "").strip()
    upper = raw.upper()
    if upper in SAN_ALIASES:
        return ("san", "理智")
    if raw in ATTRIBUTE_ALIASES:
        return ("stat", ATTRIBUTE_ALIASES[raw])
    if upper in ATTRIBUTE_CODES:
        return ("stat", upper)
    return ("skill", raw)


def resolve_check_target(skills: Dict[str, int], name: str) -> CheckTarget:
    """从 attributes_and_skills 表解析检定目标；查不到抛 KeyError（上层转 SkillNotFoundError）。

    解析顺序：先归一化别名，再按 kind 取表值；八属性取 缩写/中文 任一存在者。
    """
    kind, key = normalize_target_name(name)
    if kind == "san":
        return CheckTarget(kind, key)
    if kind == "stat":
        value = skills.get(key)
        if value is None:
            value = skills.get(_INVERSE_ATTRIBUTES.get(key, ""))
        if value is None:
            raise KeyError(key)
        return CheckTarget(kind, key, int(value))
    value = skills.get(key)
    if value is None:
        # 状态：卡上无该技能时回退技能默认值表，仍无则报缺（上层转 SkillNotFoundError）
        default = SKILL_DEFAULTS.get(key)
        if default is None:
            raise KeyError(key)
        return CheckTarget(kind, key, int(default))
    return CheckTarget(kind, key, int(value))
