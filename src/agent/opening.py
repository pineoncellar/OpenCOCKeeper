# -*- coding: utf-8 -*-
"""
@File     :   opening.py
@Desc     :   开场初始化 Agent（Opening Agent）+ Turn 0 演播管线——模组大纲提炼与开场裁决
             结合 PC 职业/背景动态裁定切入点，产出开场契约并由 Narrator 演播落库
@Note     :   无静默降级——前置条件缺失（未绑模组 / 无有效 PC）或任一步失败一律抛 OpeningError，
             修复后重试；Opening Agent 复用 run_tool_loop 闭环，只暴露 3 工具
             （search_module / get_pc_background / present_opening 收尾）；
             Turn 0 三件套：落 recent_turns(0) + seed_events(0) + mark_turns_solidified([0])，
             使开场前情直达 RAG 且不被后台 ConsolidationWorker 二次提炼；
             副作用后置——开场决策与 Narrator 演播均为纯计算前置，落库/植记忆后置，
             保证 LLM 失败时零残留、可干净重试
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.agent.directive import NarrativeDirective
from src.agent.loop import ToolRunner, _run_pc_background_tool, _run_search_tool, run_tool_loop
from src.agent.narrator import Narrator
from src.agent.schemas import build_tool_schemas
from src.core.config import get_settings
from src.core.exceptions import OpeningError

# 默认开场裁决模型档位（可被 config.context.opening / 构造参数覆盖）
DEFAULT_TIER = "smart"


# ====================================================================
# 收尾工具：present_opening
# ====================================================================

PRESENT_OPENING_NAME = "present_opening"

# 收尾工具 schema：产出开场契约四要素（场景报幕 / 大纲提炼 / 开场手记 / 前情记忆）
PRESENT_OPENING_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PRESENT_OPENING_NAME,
        "description": (
            "【收尾工具，信息足够即必须调用】输出开场决策契约：首行场景报幕 + 供 Narrator "
            "演播的开场导演手记 + 需预植入记忆库的前情记忆。一旦检索完模组开篇与调查员 "
            "背景、裁定好切入场景，立即调用本工具结束开场决策，不要继续检索。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scene_tag": {
                    "type": "string",
                    "description": "首行场景报幕，如'阿诺兹堡 - 调查员事务所 - 雨后下午'",
                },
                "opening_summary": {
                    "type": "string",
                    "description": "模组核心大纲提炼：驱动机制（受托/卷入/考察/社交/固定开场）与事件 hook",
                },
                "narrative_directive": {
                    "type": "string",
                    "description": "供 Narrator 演播的开场导演手记 Markdown（含场景描写、NPC 登场与首个抉择点收尾）",
                },
                "seeded_memories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "需预植入 RAG 的前情记忆句子（每句一个完整剧情事实）",
                },
            },
            "required": ["scene_tag", "narrative_directive", "seeded_memories"],
        },
    },
}


# ====================================================================
# 开场 System 指令（工作流引导：先检索模组、再对齐 PC、后交卷）
# ====================================================================

OPENING_SYSTEM = (
    "你是《克苏鲁的呼唤》跑团系统的开场初始化 Agent（Opening Agent）。"
    "你的任务：在玩家首个行动之前，结合模组开场背景与已创建的调查员角色卡，"
    "裁定最契合的切入场景与剧情 hook，并产出供 Narrator 演播的开场导演手记。"
    "工作流程：先调用 search_module 检索模组引言/开篇章节，提炼模组核心驱动机制"
    "（受托委托 / 突发事件卷入 / 考察探索 / 社交日常 / 固定开场）与事件 hook；"
    "再调用 get_pc_background 读取调查员职业与背景故事，让切入点与调查员身份深度对齐"
    "（如私家侦探→委托人登门、学者→学术引荐咖啡馆、警探→直达案发现场）；"
    "信息齐备后调用 present_opening 交卷。"
    "严禁凭空脑补模组未提供的设定；切入点必须同时契合模组 hook 与调查员身份，"
    "且不得替玩家决定后续行动——开场手记以抛出第一个抉择点收尾。"
)


# ====================================================================
# 开场契约与交付物
# ====================================================================


@dataclass
class OpeningSetupResult:
    """开场决策契约：场景报幕 + 开场导演手记 + 预植入前情记忆。"""

    scene_tag: str                       # 首行场景报幕文本（如 '事务所 - 雨后下午'）
    narrative_directive: str             # 供 Narrator 演播的开场导演手记（Markdown）
    seeded_memories: List[str] = field(default_factory=list)  # 需预植入 RAG 的前情记忆
    summary: str = ""                    # 模组大纲提炼（驱动机制 + hook）
    turn_num: int = 0                    # 开场固定落在 Turn 0
    converged: bool = True               # 是否经 present_opening 正常交卷


@dataclass
class NarratedOpening:
    """Turn 0 开场演播的对外交付物：开场契约 + 玩家可见开场白。"""

    setup: OpeningSetupResult            # 开场决策契约（权威副本）
    narration: str                       # Narrator 演播的玩家可见开场白


def _accept_opening(**kwargs: Any) -> dict:
    """present_opening 兜底 handler：正常路径由 stop 收敛拦截，此函数仅防 stop 失效。"""
    return {"ok": True, "accepted": True, "opening": kwargs}


def _extract_opening_setup(
    arguments: Optional[dict], fallback: str = ""
) -> OpeningSetupResult:
    """从收尾调用参数提取开场契约；缺失字段容错，手记缺失用 fallback 文本。"""
    args = arguments or {}
    narrative = str(args.get("narrative_directive") or "").strip()
    if not narrative:
        narrative = fallback
    scene = str(args.get("scene_tag") or "").strip()
    summary = str(args.get("opening_summary") or "").strip()
    mems = args.get("seeded_memories") or []
    if isinstance(mems, list):
        mems = [str(m).strip() for m in mems if str(m).strip()]
    else:
        mems = []
    return OpeningSetupResult(
        scene_tag=scene,
        narrative_directive=narrative,
        seeded_memories=mems,
        summary=summary,
    )


# ====================================================================
# Opening Agent 专属 runner 与 schema
# ====================================================================


def build_opening_runner(storage, memory: Optional[Any] = None) -> ToolRunner:
    """构造开场专属 ToolRunner：只暴露 3 个工具（search / get_pc_background / present_opening）。

    开场阶段不需要检定、标签与历史记忆查询，暴露反而干扰开场 Agent 聚焦；
    收尾工具 present_opening 由 run_tool_loop 的 stop 语义拦截，此处注册兜底 handler 防失效。
    """
    runner = ToolRunner()
    runner.register("search_module", _run_search_tool)
    runner.register(
        "get_pc_background", lambda **kw: _run_pc_background_tool(storage, **kw)
    )
    runner.register(PRESENT_OPENING_NAME, _accept_opening)
    return runner


def _opening_schemas() -> List[Dict[str, Any]]:
    """开场 Agent 工具 schema：从主 Agent 5 工具清单中挑 search/get_pc_background，
    再拼 present_opening 收尾——保证与主 Agent 同构、schema 描述复用。"""
    all_schemas = {s["function"]["name"]: s for s in build_tool_schemas()}
    return [
        all_schemas["search_module"],
        all_schemas["get_pc_background"],
        PRESENT_OPENING_SCHEMA,
    ]


# ====================================================================
# 开场决策（Opening Agent 闭环，纯计算零副作用）
# ====================================================================


async def run_opening_setup(
    storage,
    world_id: str,
    *,
    memory: Optional[Any] = None,
    llm: Optional[Any] = None,
    tier: Optional[str] = None,
    temperature: Optional[float] = None,
) -> OpeningSetupResult:
    """执行开场决策（Opening Agent 闭环），返回开场契约；失败抛 OpeningError。

    前置校验（无静默降级）：世界必须已绑定模组且存在有效 PC，否则直接抛 OpeningError；
    决策复用 run_tool_loop 闭环（search_module + get_pc_background + present_opening 收尾），
    纯计算零副作用，供 run_opening_narration 演播落库。
    """
    world = storage.get_world(world_id)
    if world is None:
        raise OpeningError(f"世界不存在: {world_id}")
    module_name = (world.get("module_name") or "").strip()
    if not module_name:
        raise OpeningError("世界未绑定模组，无法进行开场初始化")
    pcs = storage.get_entities(world_id, entity_type="PC")
    if not pcs:
        raise OpeningError("世界无有效 PC 角色卡，请先创建/绑定调查员")

    settings = get_settings()
    _tier = tier or str(settings.get("context.opening.llm_tier", DEFAULT_TIER))
    _temperature = (
        temperature
        if temperature is not None
        else settings.get("context.opening.temperature", None)
    )
    runner = build_opening_runner(storage, memory=memory)
    runner.reset_diffs()
    runner.reset_checks()
    messages = [
        {"role": "system", "content": OPENING_SYSTEM},
        {
            "role": "user",
            "content": (
                f"当前世界: {world_id}（绑定模组: {module_name}）。"
                f"请先检索模组开篇章节提炼驱动机制与事件 hook，再读取调查员职业与背景，"
                f"裁定最契合的开场切入点后调用 present_opening 交卷。"
            ),
        },
    ]
    result = await run_tool_loop(
        llm,
        _tier,
        messages,
        _opening_schemas(),
        runner,
        world_id=world_id,
        turn_num=0,
        temperature=_temperature,
        stop_tool_name=PRESENT_OPENING_NAME,
    )
    # 状态：LLM 失败或触顶未收敛 → 开场决策失败，抛错由上层处理
    if not result.final.is_ok:
        raise OpeningError(f"开场决策失败: {result.final.error or '未知错误'}")
    if result.stop_call:
        # 状态：经 present_opening 正常交卷，提取开场契约（缺失时降级用最终文本）
        setup = _extract_opening_setup(
            result.stop_call["arguments"],
            fallback=(result.final.text or "").strip(),
        )
        converged = True
    elif result.final.text:
        # 状态：模型直接文本收敛（未调收尾工具），以最终文本为开场手记降级
        setup = OpeningSetupResult(
            scene_tag="",
            narrative_directive=result.final.text.strip(),
            seeded_memories=[],
            converged=False,
        )
    else:
        raise OpeningError("开场 Agent 未产出任何决策")
    return setup


# ====================================================================
# Turn 0 演播管线（决策 -> 演播 -> 三件套落库）
# ====================================================================


async def run_opening_narration(
    storage,
    world_id: str,
    *,
    memory: Optional[Any] = None,
    llm: Optional[Any] = None,
    narrator: Optional[Narrator] = None,
    tier: Optional[str] = None,
    temperature: Optional[float] = None,
) -> NarratedOpening:
    """Turn 0 开场演播管线：决策 -> 演播 -> 三件套落库，返回开场白与契约。

    幂等保护：世界已存在 Turn 0（已开场）则抛 OpeningError，拒绝重复初始化；
    顺序——开场决策（纯计算）-> Narrator 演播（纯计算）-> 副作用落库：
    落 recent_turns(0) + seed_events(0) + mark_turns_solidified([0])，
    使开场前情直达 RAG 且不被后台固化 Worker 二次提炼；任何一步失败抛异常，
    无静默降级，副作用后置保证 LLM 失败时零残留、可干净重试。
    """
    if storage.get_turn(world_id, 0) is not None:
        raise OpeningError(f"世界 {world_id} 已开场（Turn 0 已存在），勿重复初始化")

    setup = await run_opening_setup(
        storage, world_id, memory=memory, llm=llm, tier=tier, temperature=temperature,
    )
    # 状态：场景报幕并入开场手记，Narrator 依 NARRATOR_SYSTEM 首行输出报幕
    handoff = setup.narrative_directive
    if setup.scene_tag:
        handoff = f"【首行场景报幕】{setup.scene_tag}\n\n{handoff}"
    directive = NarrativeDirective(
        narrative_directive=handoff,
        state_changes={},
        checks=[],
        turn_num=0,
        converged=setup.converged,
    )
    if narrator is None:
        narrator = Narrator(llm=llm)
    try:
        # 状态：Narrator 演播为纯计算，前置——失败时未落任何库，可干净重试；
        # world_id 必传，否则开场 trace 事件落到 traces 根目录不归任何世界
        narration = await narrator.narrate(directive, action=None, world_id=world_id)
    except Exception as e:  # noqa: BLE001  开场演播失败统一转 OpeningError
        raise OpeningError(f"开场演播失败: {type(e).__name__}: {e}") from e

    # 副作用后置：三件套——落 Turn 0、seed 前情记忆、标记已固化防二次提炼
    try:
        storage.commit_turn(
            world_id,
            0,
            state_diff={},
            context_data={
                "directive": handoff,
                "scene_tag": setup.scene_tag,
                "opening_summary": setup.summary,
            },
        )
        if memory is not None and setup.seeded_memories:
            await memory.seed_events(world_id, setup.seeded_memories, turn_num=0)
        storage.mark_turns_solidified(world_id, [0])
        storage.update_turn_context_data(world_id, 0, assistant=narration)
    except Exception as e:  # noqa: BLE001  落库/植记忆失败统一转 OpeningError
        raise OpeningError(f"开场落库失败: {type(e).__name__}: {e}") from e
    return NarratedOpening(setup=setup, narration=narration)
