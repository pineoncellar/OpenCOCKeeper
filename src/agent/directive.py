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
from typing import Any, Dict, List, Optional, Tuple

from src.core.prompts import get_prompt

# 收尾工具名：主 Agent 信息完备后调用即交卷，闭环据此提前收敛
PRESENT_DIRECTIVE_NAME = "present_directive"

# 结局类型：跑团圈通用三分类，缺失/非法时归一化兜底为 TD（真实结局）
ENDING_TYPE_HD = "HD"
ENDING_TYPE_TD = "TD"
ENDING_TYPE_BD = "BD"
ENDING_TYPES = frozenset({ENDING_TYPE_HD, ENDING_TYPE_TD, ENDING_TYPE_BD})
DEFAULT_ENDING_TYPE = ENDING_TYPE_TD  # 状态：is_ending 为真但类型缺失/非法时的兜底

# 收尾工具 schema 构造函数：description 动态读配置（热重载生效）；
# state_changes 不暴露（状态以程序执行为准），终局信号 is_ending / ending_type 为模型权威
# （与程序权威区 state_changes / checks 不同）——软结局判定信任模型，缺失按非终局处理
def build_present_directive_schema() -> Dict[str, Any]:
    """构建 present_directive 收尾工具 schema；description 动态读配置（热重载生效）。"""
    return {
        "type": "function",
        "function": {
            "name": PRESENT_DIRECTIVE_NAME,
            "description": get_prompt("directive.present_directive"),
            "parameters": {
                "type": "object",
                "properties": {
                    "narrative_directive": {
                        "type": "string",
                        "description": (
                            "供 Narrator 演播的导演手记（Markdown）：固定按"
                            "规则裁决/剧情推进与事实揭露/氛围与演播建议分小节，"
                            "可选「### NPC 扮演提示」（写明 NPC 本轮态度与允许透露的"
                            "1~2 个简短信息点）；剧情揭露仅限本轮直接触发的客观事实，"
                            "手记是导演指令不是成品叙事，严禁代操玩家动作/心理/台词，"
                            "严禁编写成品长篇台词或输出选项列表菜单"
                        ),
                    },
                    "is_ending": {
                        "type": "boolean",
                        "description": (
                            "剧情是否达到终局（解决事件 / 主动逃离或放弃调查 / "
                            "因疯狂或重伤导致调查终止），达终局置 true，默认 false"
                        ),
                    },
                    "ending_type": {
                        "type": "string",
                        "enum": [ENDING_TYPE_HD, ENDING_TYPE_TD, ENDING_TYPE_BD],
                        "description": (
                            "仅当 is_ending=true 时的结局类型："
                            "HD=完美结局（彻底解决事件）、TD=真实结局（经历完整但有缺憾）、"
                            "BD=坏结局（全灭/被困/中途逃跑）"
                        ),
                    },
                },
                "required": ["narrative_directive"],
            },
        },
    }


# 兼容旧引用：import 时求值一次的默认 schema（导出符号不破坏；
# 运行时请走 build_present_directive_schema() 以保证热重载生效）
PRESENT_DIRECTIVE_SCHEMA: Dict[str, Any] = build_present_directive_schema()


@dataclass
class NarrativeDirective:
    """主 Agent 一轮决策的最终交付物，供下游 Narrator 演播与日志留存。"""

    state_changes: dict                       # 合并后的 state_diff（程序权威，可回档）
    narrative_directive: str                  # Markdown 导演手记（程序不解析）
    turn_num: int                             # 本轮轮次号
    converged: bool = True                    # 是否经 present_directive 正常交卷（False=文本降级）
    checks: List[dict] = field(default_factory=list)  # 本轮检定结果权威副本（掷骰值/成功等级，透传 Narrator）
    is_ending: bool = False                   # 是否终局轮（模型权威的叙事信号，非程序判定）
    ending_type: str = ""                     # 终局类型 HD/TD/BD，非终局为空串


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


def extract_ending(arguments: Optional[dict]) -> Tuple[bool, str]:
    """从收尾调用参数提取终局信号，返回 (is_ending, ending_type)。

    is_ending 非真（缺失/False）一律按正常回合处理，ending_type 置空串；
    is_ending 为真但 ending_type 缺失/非法时归一化兜底为 TD（真实结局），
    保证收尾管线始终拿到合法类型，杜绝脏值流入落库与快照。
    """
    if not arguments or not arguments.get("is_ending"):
        return False, ""
    et = str(arguments.get("ending_type") or "").strip().upper()
    if et not in ENDING_TYPES:
        et = DEFAULT_ENDING_TYPE
    return True, et
