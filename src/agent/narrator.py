# -*- coding: utf-8 -*-
"""
@File     :   narrator.py
@Desc     :   润色 Agent（Narrator）——把《叙事决策大纲》+ 近程对话翻译成玩家视角的沉浸叙事
@Note     :   无状态——不持 storage、不读不写数据库，纯输入输出转换；
             输入 = NarrativeDirective（手记 + checks 检定权威区）+ recent_turns + 本轮行动；
             输出 = 纯文本（首行场景报幕 [地点-区域-时间/天气]，1~2 段 200~300 字）；
             NPC 分层：L1/L2 严守手记与大纲指令、L3 授权即兴并依近程历史保持自洽；
             零元语言——检定成败化作客观环境变化，杜绝规则词汇破坏沉浸感；
             LLM 失败抛 NarratorError，由上层管线决定降级策略；
             默认模型档位取 config.context.narrator（缺省 standard / temperature 0.7）
"""

from __future__ import annotations

from typing import Any, List, Optional

from src.core.config import get_settings
from src.core.exceptions import NarratorError
from src.core.prompts import get_prompt
from src.webui.trace_engine import (
    get_trace_bus,
    make_llm_request_event,
    make_llm_response_event,
)

# 默认演播模型档位与温度（文学表达优先于裁决档；可被 config / 构造参数覆盖）
DEFAULT_TIER = "standard"
DEFAULT_TEMPERATURE = 0.7


# ====================================================================
# Narrator System 指令（演播契约 + NPC 守则 + 风格 + 输出格式）
# ====================================================================

# Narrator System 指令（演播契约 + NPC 守则 + 风格 + 输出格式）；
# 正文外置 prompts.yaml（narrator.system），import 时解析一次供导出/测试引用；
# 运行时 _build_messages 每次动态 get_prompt，保证 WebUI 热重载后立即生效
NARRATOR_SYSTEM = get_prompt("narrator.system")


# ====================================================================
# 终局演播 System 指令（Narrator 收束终局专用，覆盖常规"交还主动权"引导）
# ====================================================================

# 终局演播 System 指令（Narrator 收束终局专用，覆盖常规"交还主动权"引导）；
# 正文外置 prompts.yaml（narrator.ending_system），import 时解析一次供导出/测试引用
NARRATOR_ENDING_SYSTEM = get_prompt("narrator.ending_system")


# 结局类型标签：供终局演播与终局结算卡片展示
_ENDING_LABELS = {
    "HD": "完美结局 (Happy End)",
    "TD": "真实结局 (True End)",
    "BD": "坏结局 (Bad End)",
}


def ending_label(ending_type: str) -> str:
    """结局类型 -> 人类可读标签；未知类型原样返回。"""
    return _ENDING_LABELS.get(str(ending_type).strip().upper(), ending_type)


# ====================================================================
# 输入渲染辅助（纯函数，Narrator 自足不依赖装配器）
# ====================================================================


def _format_checks(checks: List[dict]) -> str:
    """把检定结果权威区渲染为 Narrator 可见事实文本。

    程序层只做透传不修改骰值/成败等级；Narrator 须把数值原样呈现给玩家，
    不得改写或臆造。无检定记录时返回占位说明。
    """
    if not checks:
        return "（本轮无检定）"
    lines = []
    for c in checks:
        parts = [f"{c.get('entity_id') or '未知'}：{c.get('skill_or_attribute') or '未知检定'}"]
        roll = c.get("roll_value")
        thr = c.get("threshold")
        label = c.get("success_level_label") or c.get("success_level") or ""
        if roll is not None and thr is not None:
            parts.append(f"{roll}/{thr}")
        if label:
            parts.append(label)
        dice = c.get("bonus_penalty_dice") or 0
        if dice:
            kind = "奖励" if dice > 0 else "惩罚"
            parts.append(f"（{kind}{abs(dice)}骰）")
        lines.append("- " + " ".join(parts))
    return "\n".join(lines)


def _render_recent(recent: List[dict]) -> str:
    """近程对话渲染：玩家输入 - 守秘人输出，剥离工具中间过程。

    与装配器渲染口径一致，保证 Narrator 看到的近程历史与主 Agent 相同；
    无对话内容的轮次跳过，空历史给占位说明。
    """
    parts: List[str] = []
    for t in recent or []:
        cd = t.get("context_data") or {}
        user = cd.get("user")
        # 状态：优先玩家视角叙事，其次权威手记——让 Narrator 看到玩家已听到的内容
        assistant = cd.get("assistant")
        if not user and not assistant:
            continue
        if user:
            parts.append(f"第 {t['turn_num']} 轮 玩家：{user}")
        if assistant:
            parts.append(f"守秘人：{assistant}")
    return "\n".join(parts) or "（暂无历史对话）"


def build_narrator_messages(
    directive,
    *,
    recent: Optional[List[dict]] = None,
    action: Optional[str] = None,
    recent_text: Optional[str] = None,
    checks_text: Optional[str] = None,
    ending: bool = False,
) -> List[dict]:
    """构造 Narrator 的 system + user 消息，可直接喂 llm。

    directive 为 NarrativeDirective（手记 + checks 权威区）；
    recent_text 已给则跳过内部渲染（调用方复用装配器产物时用）；
    checks_text 已给则跳过内部渲染；action 为本轮玩家行动；
    ending=True 时改用 NARRATOR_ENDING_SYSTEM 终局演播契约，并注入【结局类型】。
    """
    handoff = (directive.narrative_directive or "").strip() or "（本轮无手记）"
    if recent_text is None:
        recent_text = _render_recent(recent)
    if checks_text is None:
        checks_text = _format_checks(directive.checks or [])
    # 状态：终局/常规契约每次动态读配置（热重载生效）
    system = (
        get_prompt("narrator.ending_system") if ending else get_prompt("narrator.system")
    )
    user = [f"【叙事决策大纲】\n{handoff}", f"【检定结果权威区】\n{checks_text}", f"【近程对话历史】\n{recent_text}"]
    if ending and getattr(directive, "ending_type", ""):
        user.append(f"【结局类型】\n{ending_label(directive.ending_type)}")
    if action:
        user.append(f"【本轮玩家行动】\n{action}")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user)},
    ]


# ====================================================================
# Narrator 无状态演播器
# ====================================================================


class Narrator:
    """纯翻译演播器：接收契约与近程历史，输出玩家视角叙事文本。

    llm 对齐 call_llm 签名：await llm(tier, messages, temperature=...)；
    不传则运行时从 src.llm 解析（便于测试注入 fake）；不持 storage，无状态。
    """

    def __init__(
        self,
        llm: Optional[Any] = None,
        *,
        tier: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> None:
        settings = get_settings()
        self._llm = llm
        self._tier = tier or str(settings.get("context.narrator.llm_tier", DEFAULT_TIER))
        self._temperature = (
            temperature
            if temperature is not None
            else float(settings.get("context.narrator.temperature", DEFAULT_TEMPERATURE))
        )

    def _resolve_llm(self) -> Any:
        """延迟解析 llm：未注入时从 src.llm 动态取 call_llm，兼容测试 monkeypatch。"""
        if self._llm is None:
            from src import llm as llm_module

            self._llm = getattr(llm_module, "call_llm", None)
        if self._llm is None:
            raise RuntimeError("Narrator 需要可用的 llm 可调用对象")
        return self._llm

    async def narrate(
        self,
        directive,
        *,
        recent: Optional[List[dict]] = None,
        action: Optional[str] = None,
        recent_text: Optional[str] = None,
        ending: Optional[bool] = None,
        world_id: str = "",
    ) -> str:
        """把一份《叙事决策大纲》翻译成玩家叙事文本。

        recent 为近程轮次记录（通常由管线从 storage 读取，不含本轮）；
        recent_text 已给则直接使用；ending 缺省自动取 directive.is_ending，
        为 True 时走 NARRATOR_ENDING_SYSTEM 终局演播（闭幕感 + 后日谈，不交还主动权）；
        world_id 为可选项，提供时 LLM trace 按世界隔离写入；
        LLM 失败或产出空文本抛 NarratorError。
        """
        if ending is None:
            ending = bool(getattr(directive, "is_ending", False))
        messages = build_narrator_messages(
            directive,
            recent=recent,
            action=action,
            recent_text=recent_text,
            ending=ending,
        )
        # 状态：发布 LLM 请求事件到 TraceBus（含完整 messages），供 WebUI 展示演播提示词
        await get_trace_bus().publish(make_llm_request_event(
            self._tier, messages, None,
            world_id=world_id, turn_num=getattr(directive, "turn_num", 0),
        ))
        llm = self._resolve_llm()
        result = await llm(
            self._tier, messages,
            temperature=self._temperature,
            world_id=world_id, turn_num=getattr(directive, "turn_num", 0),
        )
        # 状态：发布 LLM 响应事件到 TraceBus（含全文与 tool_calls），补全叙事 Agent 输入→输出链
        await get_trace_bus().publish(make_llm_response_event(
            result, self._tier,
            world_id=world_id, turn_num=getattr(directive, "turn_num", 0),
        ))
        if not result.is_ok:
            raise NarratorError(
                f"Narrator 演播失败: {result.error or '未知错误'}"
            ) from None
        text = (result.text or "").strip()
        if not text:
            raise NarratorError("Narrator 未产出任何叙事文本")
        return text
