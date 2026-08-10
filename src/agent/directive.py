# -*- coding: utf-8 -*-
"""
@File     :   directive.py
@Desc     :   《叙事决策大纲》契约模块：present_directive 收尾工具 schema + NarrativeDirective 产物
@Note     :   混合契约——state_changes 由程序从工具执行 diff 合并（模型不可见，杜绝幻觉重报）；
             narrative_directive 为 Markdown 自由文本，程序不解析子字段，原样透传 Narrator；
             checks 为程序从工具执行保留的检定结果权威副本（掷骰值/成功等级等），透传 Narrator；
             契约结构见 docs/主agent叙事决策大纲.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 收尾工具名：主 Agent 信息完备后调用即交卷，闭环据此提前收敛
PRESENT_DIRECTIVE_NAME = "present_directive"

# 收尾工具 schema：只暴露叙事导演手记，不暴露 state_changes（状态以程序执行为准）
PRESENT_DIRECTIVE_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PRESENT_DIRECTIVE_NAME,
        "description": (
            "【收尾工具，信息足够即必须调用】输出本轮《叙事决策大纲》的叙事导演手记"
            "（Markdown，含规则裁决、剧情推进与事实揭露、氛围与演播建议），供下游 Narrator 演播。"
            "手记可含「### NPC 扮演提示」小节：关键 NPC 写明人设与反应；"
            "次要 NPC 可略过（Narrator 将即兴发挥，不得违背其身份事实）。"
            "一旦检索与判定信息足够支撑本轮裁决，立即调用本工具结束本轮，不要继续检索"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "narrative_directive": {
                    "type": "string",
                    "description": "供 Narrator 演播的 Markdown 导演手记，可含 ### 小节",
                },
            },
            "required": ["narrative_directive"],
        },
    },
}


@dataclass
class NarrativeDirective:
    """主 Agent 一轮决策的最终交付物，供下游 Narrator 演播与日志留存。"""

    state_changes: dict                       # 合并后的 state_diff（程序权威，可回档）
    narrative_directive: str                  # Markdown 导演手记（程序不解析）
    turn_num: int                             # 本轮轮次号
    converged: bool = True                    # 是否经 present_directive 正常交卷（False=文本降级）
    checks: List[dict] = field(default_factory=list)  # 本轮检定结果权威副本（掷骰值/成功等级，透传 Narrator）


def extract_narrative_directive(
    arguments: Optional[dict], fallback: str = ""
) -> str:
    """从收尾调用参数中提取叙事导演手记；缺失或非文本时降级用 fallback。

    防止模型漏填导致解析崩溃——宁可降级也不中断主 Agent 回合。
    """
    if not arguments:
        return fallback
    value = arguments.get("narrative_directive")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback
