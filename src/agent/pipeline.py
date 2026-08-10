# -*- coding: utf-8 -*-
"""
@File     :   pipeline.py
@Desc     :   串行管线编排：Director（裁决）→ Narrator（演播）→ 玩家视角叙事落库
@Note     :   run_narrated_turn 为阶段三的对外入口——先跑主 Agent 回合拿契约，
             再让 Narrator 把契约翻译成玩家文本，最后把叙事覆盖 assistant 落库
             （手记权威副本存 directive 键、checks 保留），保证近程历史成为
             玩家视角对话、审计仍可回查手记；Narrator 失败抛 NarratorError，
             物理状态与手记已落库不受影响，上层可降级对外输出
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from src.agent.directive import NarrativeDirective
from src.agent.director import Director
from src.agent.narrator import Narrator
from src.core.config import get_settings
from src.core.log import get_logger

logger = get_logger(__name__)


@dataclass
class NarratedTurn:
    """一轮完整管线的对外交付物：权威契约 + 玩家视角叙事文本。"""

    directive: NarrativeDirective       # 主 Agent 裁决契约（含手记 / checks / state_changes）
    narration: str                      # Narrator 演播的最终玩家文本


async def run_narrated_turn(
    storage,
    world_id: str,
    action: str,
    *,
    director: Optional[Director] = None,
    narrator: Optional[Narrator] = None,
    llm: Optional[Any] = None,
    tier: str = "smart",
    temperature: Optional[float] = None,
    turn_num: Optional[int] = None,
    recent_limit: Optional[int] = None,
    rng: Optional[object] = None,
    on_turn_committed: Optional[Callable[[str, int], Awaitable[None]]] = None,
) -> NarratedTurn:
    """执行一轮完整管线：裁决 → 演播 → 落库，返回 NarratedTurn。

    director / narrator 可注入（测试用 fake 或定制）；缺省分别新建；
    llm 为两者共用的可调用对象（对齐 call_llm 签名，不传则各自动态解析）；
    recent_limit 为近程历史注入轮数，缺省取 config.context.assembler.recent_turns；
    on_turn_committed 为落库完成后的触发钩子（如通知后台固化 Worker），
    签名 (world_id, turn_num)，应尽快返回——长耗时逻辑请自行 create_task；
    钩子失败仅记日志，不影响本轮交付。
    流程：先 Director.run_turn 落库契约，再读近程历史（不含本轮），
    Narrator 翻译后把叙事覆盖该轮 assistant 落库（手记存 directive 键），
    最后触发 on_turn_committed 钩子。
    """
    if director is None:
        director = Director(
            storage, llm=llm, tier=tier, temperature=temperature, rng=rng
        )
    if narrator is None:
        narrator = Narrator(llm=llm)
    directive = await director.run_turn(
        world_id, action, turn_num=turn_num
    )
    if recent_limit is None:
        recent_limit = int(get_settings().get("context.assembler.recent_turns", 10))
    # 状态：近程历史剔除本轮——本轮手记已随契约下发，避免 Narrator 重复读到"守秘人：手记"
    recent = [
        t
        for t in storage.get_recent_turns(world_id, limit=recent_limit)
        if t["turn_num"] != directive.turn_num
    ]
    narration = await narrator.narrate(directive, recent=recent, action=action)
    # 状态：玩家视角叙事覆盖 assistant，手记权威副本转存 directive 键，checks 保留
    storage.update_turn_context_data(
        world_id,
        directive.turn_num,
        assistant=narration,
        directive=directive.narrative_directive,
    )
    # 状态：落库完成后触发上层钩子（如通知固化 Worker），失败不影响本轮交付
    if on_turn_committed is not None:
        try:
            await on_turn_committed(world_id, directive.turn_num)
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"on_turn_committed 钩子失败 world={world_id} "
                f"turn={directive.turn_num}: {e}",
                exc_info=True,
            )
    return NarratedTurn(directive=directive, narration=narration)
