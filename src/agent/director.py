# -*- coding: utf-8 -*-
"""
@File     :   director.py
@Desc     :   主 Agent（Director）编排器：装配 → Function Calling 闭环 → 契约化 → 统一落库
@Note     :   run_turn 是唯一回合入口——assemble 产出 bundle.messages 喂 run_tool_loop，
             模型调用 present_directive 交卷（或直接文本降级）后，state_changes 由
             runner.collected_diffs 程序合并（不信任模型重报，杜绝幻觉扣血/扣 Tag），
             最后统一走 apply_turn_change 落库（空 diff 也写轮，保证近程对话连续）；
             默认模型档位取 config.context.director（缺省 smart），temperature 不设则
             不覆盖模型档位默认（可用 0.3~0.4 提升决策稳定性）
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from src.agent.assembler import assemble
from src.agent.loop import _brief, build_default_runner, run_tool_loop
from src.core.config import get_settings
from src.core.log import get_logger
from src.agent.directive import (
    NarrativeDirective,
    PRESENT_DIRECTIVE_NAME,
    extract_ending,
    extract_narrative_directive,
)
from src.agent.schemas import build_main_agent_schemas
from src.core.exceptions import AgentLoopError
from src.tools.commit import apply_turn_change

logger = get_logger(__name__)

# llm 可调用签名：await llm(tier, messages, tools=..., temperature=...)
LLMCallable = Callable[..., Awaitable[Any]]

# 默认裁决模型档位（可被 config.context.director / 构造参数覆盖）
DEFAULT_TIER = "smart"


def _accept_directive(**kwargs: Any) -> dict:
    """present_directive 的兜底 handler：正常路径由 stop 收敛拦截，此函数仅防 stop 失效。"""
    return {
        "ok": True,
        "accepted": True,
        "directive": kwargs.get("narrative_directive", ""),
    }


class Director:
    """主 Agent 编排器：把装配、闭环决策、契约化与落库串成一轮回合。"""

    def __init__(
        self,
        storage,
        *,
        memory: Optional[Any] = None,
        llm: Optional[LLMCallable] = None,
        tier: Optional[str] = None,
        temperature: Optional[float] = None,
        rng: Optional[object] = None,
    ) -> None:
        self._storage = storage
        self._memory = memory
        self._llm = llm
        settings = get_settings()
        # 状态：消费方自读默认档位（对齐 Narrator），显式传入优先
        self._tier = tier or str(settings.get("context.director.llm_tier", DEFAULT_TIER))
        self._temperature = (
            temperature
            if temperature is not None
            else settings.get("context.director.temperature", None)
        )
        self._rng = rng

    async def run_turn(
        self,
        world_id: str,
        action: str,
        *,
        turn_num: Optional[int] = None,
    ) -> NarrativeDirective:
        """执行一轮完整回合，返回《叙事决策大纲》；LLM 失败抛 AgentLoopError。

        turn_num 缺省取 storage.next_turn_num（单调自增）；
        流程：装配 bundle → 闭环决策（4 原子工具 + present_directive 交卷）→
        契约化（state_changes 合并 collected_diffs）→ apply_turn_change 统一落库。
        """
        turn = (
            turn_num if turn_num is not None else self._storage.next_turn_num(world_id)
        )
        logger.info(
            "回合开始 world=%s turn=%s action=%s", world_id, turn, _brief(action)
        )
        bundle = assemble(self._storage, world_id, action=action)
        runner = build_default_runner(
            self._storage, memory=self._memory, rng=self._rng
        )
        # 状态：注册收尾工具兜底 handler（stop 收敛失效时的无害落点）
        runner.register(PRESENT_DIRECTIVE_NAME, _accept_directive)
        runner.reset_diffs()
        runner.reset_checks()
        result = await run_tool_loop(
            self._llm,
            self._tier,
            bundle.messages,
            build_main_agent_schemas(),
            runner,
            world_id=world_id,
            turn_num=turn,
            temperature=self._temperature,
            stop_tool_name=PRESENT_DIRECTIVE_NAME,
        )
        # 状态：LLM 失败或触顶未收敛 → 回合失败，抛错由上层处理
        if not result.final.is_ok:
            raise AgentLoopError(
                f"主 Agent 决策失败: {result.final.error or '未知错误'}"
            ) from None
        if result.stop_call:
            # 状态：经 present_directive 正常交卷，提取导演手记（缺失时降级用最终文本）
            narrative = extract_narrative_directive(
                result.stop_call["arguments"],
                fallback=(result.final.text or "").strip(),
            )
            # 状态：终局信号（is_ending / ending_type）为模型权威的叙事字段，提取并归一化
            is_ending, ending_type = extract_ending(result.stop_call["arguments"])
            converged = True
        elif result.final.text:
            # 状态：模型直接文本收敛（未调收尾工具），以最终文本为导演手记降级
            narrative = result.final.text.strip()
            converged = False
            is_ending, ending_type = False, ""
        else:
            raise AgentLoopError("主 Agent 未产出任何决策文本")
        # 状态：统一落库（空 diff 也写轮，保证近程对话连续），state_diff 为程序权威
        context_data: dict = {
            "user": action,
            "assistant": narrative,
            "checks": runner.collected_checks,
        }
        if is_ending:  # 状态：终局轮持久化终局字段，供收尾管线与审计回查
            context_data["is_ending"] = True
            context_data["ending_type"] = ending_type
        record = apply_turn_change(
            self._storage,
            world_id,
            turn,
            diffs=runner.collected_diffs,
            context_data=context_data,
        )
        logger.info(
            "回合裁决完成 world=%s turn=%s converged=%s 工具调用=%d diffs=%d "
            "checks=%d is_ending=%s",
            world_id, turn, converged, len(result.tool_calls),
            len(runner.collected_diffs), len(runner.collected_checks), is_ending,
        )
        return NarrativeDirective(
            state_changes=record["state_diff"],
            narrative_directive=narrative,
            turn_num=turn,
            converged=converged,
            checks=runner.collected_checks,
            is_ending=is_ending,
            ending_type=ending_type,
        )
