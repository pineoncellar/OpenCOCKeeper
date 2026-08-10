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

# 默认演播模型档位与温度（文学表达优先于裁决档；可被 config / 构造参数覆盖）
DEFAULT_TIER = "standard"
DEFAULT_TEMPERATURE = 0.7


# ====================================================================
# Narrator System 指令（演播契约 + NPC 守则 + 风格 + 输出格式）
# ====================================================================

NARRATOR_SYSTEM = (
    "你是《克苏鲁的呼唤》(CoC 7th) 的守秘人（KP）兼文学演播员。"
    "你的唯一任务：把主 Agent（Director）传给你的【叙事决策大纲】与【近程对话历史】"
    "转化为沉浸、精炼、充满画面感的自然语言回复。"
    "你不参与任何规则判定、骰点计算或剧情走向决策——那都是主 Agent 的职责。"
    "\n\n【演播契约与绝对边界】"
    "\n1. 绝对忠实大纲：严禁擅自添加大纲中未提及的剧情真相、线索，或替玩家做决定。"
    "大纲中裁决的物理事实（检定成败、伤害、环境改变）即为绝对真相，必须被精准呈现。"
    "\n2. 零元语言：严禁输出任何'检定成功/失败'、'规则判定'、'根据大纲提示'等"
    "破坏沉浸感的系统味词汇；检定成败必须隐喻为客观发生的画面"
    "（成功→动作达成/线索浮现，失败→负面后果/遭遇变故）。"
    "\n\n【NPC 扮演与即兴守则】"
    "\n1. 恪守指令（L1/L2）：大纲中【### NPC 扮演提示】或模组规定的核心立场与动机"
    "必须严格遵照执行，绝不可偏离，不得擅自泄密或扭转立场。"
    "\n2. 授权即兴（L3）：大纲未提及细节的次要 NPC，授权你当场即兴确立其外表、"
    "性情与口吻（赋予真实的阶级感与情绪）。"
    "\n3. 保持自洽：务必参考【近程对话历史】中该 NPC 过往的言行与语气，无缝延续其性格；"
    "同一 NPC 的行事作风绝不能在无剧情依据的情况下陡变。"
    "\n\n【高品质叙事风格指南】"
    "\n1. 篇幅克制：输出控制在 1~2 个自然段、200~300 字以内；语言精炼、留白自然，"
    "结尾自然停顿，把行动主动权交还给玩家，不要替玩家决定下一步。"
    "\n2. 直白与感官沉浸：避免空洞的修辞堆砌，聚焦真实的物理与感官细节"
    "（嗅觉、触感、光影、材质、衣着磨损等），用直白精准的语言描写玩家直接"
    "'看见、听到与感受到'的物理事实。"
    "\n\n【输出格式约束】"
    "\n第一行必须严格遵守此格式进行场景报幕：[场景/位置 - 概括区域 - 当前时间/天气]"
    "（时间/天气须基于大纲与近程上下文已有的物理事实，未知则用'不明'，不得臆造大纲未提及的地点）"
    "另起一行开始渲染叙事文本，结尾自然留白等待玩家决策。"
)


# ====================================================================
# 输入渲染辅助（纯函数，Narrator 自足不依赖装配器）
# ====================================================================


def _format_checks(checks: List[dict]) -> str:
    """把检定结果权威区渲染为 Narrator 可见事实文本。

    程序层只做透传不修改骰值/成败等级；Narrator 据此演绎对应客观画面，
    不得臆造或夸大成败。无检定记录时返回占位说明。
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
) -> List[dict]:
    """构造 Narrator 的 system + user 消息，可直接喂 llm。

    directive 为 NarrativeDirective（手记 + checks 权威区）；
    recent_text 已给则跳过内部渲染（调用方复用装配器产物时用）；
    checks_text 已给则跳过内部渲染；action 为本轮玩家行动。
    """
    handoff = (directive.narrative_directive or "").strip() or "（本轮无手记）"
    if recent_text is None:
        recent_text = _render_recent(recent)
    if checks_text is None:
        checks_text = _format_checks(directive.checks or [])
    user = [f"【叙事决策大纲】\n{handoff}", f"【检定结果权威区】\n{checks_text}", f"【近程对话历史】\n{recent_text}"]
    if action:
        user.append(f"【本轮玩家行动】\n{action}")
    return [
        {"role": "system", "content": NARRATOR_SYSTEM},
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
    ) -> str:
        """把一份《叙事决策大纲》翻译成玩家叙事文本。

        recent 为近程轮次记录（通常由管线从 storage 读取，不含本轮）；
        recent_text 已给则直接使用；LLM 失败或产出空文本抛 NarratorError。
        """
        messages = build_narrator_messages(
            directive, recent=recent, action=action, recent_text=recent_text
        )
        llm = self._resolve_llm()
        result = await llm(self._tier, messages, temperature=self._temperature)
        if not result.is_ok:
            raise NarratorError(
                f"Narrator 演播失败: {result.error or '未知错误'}"
            ) from None
        text = (result.text or "").strip()
        if not text:
            raise NarratorError("Narrator 未产出任何叙事文本")
        return text
