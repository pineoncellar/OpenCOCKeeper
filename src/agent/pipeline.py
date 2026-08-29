# -*- coding: utf-8 -*-
"""
@File     :   pipeline.py
@Desc     :   串行管线编排：Director（裁决）→ Narrator（演播）→ 玩家视角叙事落库；
             终局分支——Director 判定 is_ending 后进入收尾管线（终局演播 + 全盘固化
             + __ENDING__ 快照 + 世界归档），常规回合与终局收尾共用本模块
@Note     :   run_narrated_turn 为阶段三的对外入口——先跑主 Agent 回合拿契约，
             再让 Narrator 把契约翻译成玩家文本，最后把叙事覆盖 assistant 落库
             （手记权威副本存 directive 键、checks 保留），保证近程历史成为
             玩家视角对话、审计仍可回查手记；Narrator 失败抛 NarratorError，
             物理状态与手记已落库不受影响，上层可降级对外输出；
             终局收尾走 run_ending_wrapup——无静默降级，任一步失败抛 EndingError
             且世界保持 ACTIVE 不归档（用户修复后重试），固化成功后才置 ARCHIVED
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable, Optional

from src.agent.directive import (
    ENDING_TYPES,
    NarrativeDirective,
)
from src.agent.director import Director
from src.agent.narrator import Narrator
from src.core.config import get_settings
from src.core.exceptions import EndingError
from src.core.log import get_logger
from src.core.prompts import get_prompt
from src.tools.commit import apply_turn_change
from src.webui.trace_engine import (
    get_trace_bus,
    make_narration_event,
    make_player_input_event,
)

logger = get_logger(__name__)

# 场景切换判定阈值：新旧手记整块文本重叠率低于该值视为场景大转移
# （场景转换几乎整块重写，同场景演进是增量修改；阈值保守防误触发频繁 force 固化）
SCENE_TRANSITION_RATIO = 0.3


def _is_scene_transition(
    old_notes: str, new_notes: str, *, threshold: float = SCENE_TRANSITION_RATIO
) -> bool:
    """场景大转移检测：仅比较新旧手记整块文本差异度，绝不解析内容子字段。

    手记是当前场景的进行时状态（软信息，程序零解析红线），场景转换时几乎整块
    重写、同场景演进则是增量修改——用字符重叠率（SequenceMatcher.ratio）做保守
    代理信号；任一侧为空或完全相同不视为切换。
    """
    old = (old_notes or "").strip()
    new = (new_notes or "").strip()
    if not old or not new or old == new:
        return False
    return SequenceMatcher(None, old, new).ratio() < threshold


def _manual_ending_handoff() -> str:
    """/world archive 主动结团的默认导演手记（BD 软结局：未走到模组终幕由 KP 收束）。
    正文外置 prompts.yaml（pipeline.manual_ending_handoff），运行时动态读取支持热重载。"""
    return get_prompt("pipeline.manual_ending_handoff")


@dataclass
class NarratedTurn:
    """一轮完整管线的对外交付物：权威契约 + 玩家视角叙事文本。"""

    directive: NarrativeDirective       # 主 Agent 裁决契约（含手记 / checks / state_changes）
    narration: str                      # Narrator 演播的最终玩家文本
    ended: bool = False                 # 是否为终局收尾轮（is_ending=True 并已归档）
    ending_type: str = ""               # 终局类型 HD/TD/BD，非终局为空串
    recap: str = ""                     # 归档后的最终全盘前情提要（终局轮才非空）


@dataclass
class EndedTurn:
    """终局收尾的对外交付物：终局演播文本 + 最终 recap + 结局类型。"""

    narration: str                      # 终局演播文本（闭幕感 + 后日谈）
    ending_type: str                    # 结局类型 HD/TD/BD
    recap: str                          # 归档后的最终全盘前情提要（global_recap）
    turn_num: int                       # 终局落库轮次


# ====================================================================
# 终局收尾管线
# ====================================================================


def prepare_manual_ending(
    storage,
    world_id: str,
    *,
    ending_type: str = "BD",
    handoff: Optional[str] = None,
) -> NarrativeDirective:
    """为 /world archive 主动结团构造终局契约：复用已存在的未归档终局轮（重试幂等），
    否则新落一轮终局轮次（user='[KP 主动结团]'）并返回契约。

    ending_type 非法时兜底 BD；世界不存在/已归档抛 EndingError。
    """
    world = storage.get_world(world_id)
    if world is None:
        raise EndingError(f"世界不存在: {world_id}")
    if world.get("status") == "ARCHIVED":
        raise EndingError(f"世界已归档: {world_id}，无需重复结团")
    # 重试幂等：已有未归档终局轮则复用该轮，避免 /world archive 失败后重试重复落轮
    for t in reversed(storage.get_recent_turns(world_id)):
        cd = t.get("context_data") or {}
        if cd.get("is_ending"):
            return NarrativeDirective(
                state_changes={},
                narrative_directive=cd.get("directive") or _manual_ending_handoff(),
                turn_num=t["turn_num"],
                converged=False,
                is_ending=True,
                ending_type=cd.get("ending_type") or "BD",
            )
    et = str(ending_type).strip().upper()
    if et not in ENDING_TYPES:
        et = "BD"
    handoff = (handoff or _manual_ending_handoff()).strip()
    turn_num = storage.next_turn_num(world_id)
    apply_turn_change(
        storage,
        world_id,
        turn_num,
        diffs=[],
        context_data={
            "user": "[KP 主动结团]",
            "assistant": handoff,
            "directive": handoff,
            "is_ending": True,
            "ending_type": et,
        },
    )
    return NarrativeDirective(
        state_changes={},
        narrative_directive=handoff,
        turn_num=turn_num,
        converged=False,
        is_ending=True,
        ending_type=et,
    )


async def run_ending_wrapup(
    storage,
    world_id: str,
    directive: NarrativeDirective,
    *,
    memory: Any,
    llm: Optional[Any] = None,
    narrator: Optional[Narrator] = None,
    worker: Optional[Any] = None,
) -> EndedTurn:
    """终局收尾：终局演播 → 终局叙事落库 → 全盘固化 → __ENDING__ 快照 → 世界归档。

    无静默降级——演播/落库/固化/快照/归档任一步失败抛 EndingError 中断停运，
    世界状态保持 ACTIVE 不归档（状态绝对一致，用户修复后经 /world archive 重试，
    复用同一终局轮不重复落轮）；固化/快照/归档与后台固化共享世界锁防并发。
    前置要求：directive 的终局轮已落库（模型路径由 Director 落，手动路径由
    prepare_manual_ending 落）。
    """
    if narrator is None:
        narrator = Narrator(llm=llm)
    # 终局演播为纯计算前置，失败零残留（终局轮已含手记，可干净重试）
    try:
        narration = await narrator.narrate(directive, ending=True, world_id=world_id)
    except Exception as e:  # noqa: BLE001  演播失败统一转 EndingError
        raise EndingError(f"终局演播失败: {type(e).__name__}: {e}") from e
    # 终局叙事覆盖 assistant 落库，终局字段权威副本入 context_data
    try:
        storage.update_turn_context_data(
            world_id,
            directive.turn_num,
            assistant=narration,
            is_ending=True,
            ending_type=directive.ending_type,
        )
    except Exception as e:  # noqa: BLE001
        raise EndingError(f"终局叙事落库失败: {type(e).__name__}: {e}") from e
    # 固化 + 快照 + 归档：与后台固化共享世界锁，保证与轮询/回档互斥
    if worker is not None:
        async with worker.get_world_lock(world_id):  # 状态：与后台固化互斥
            return await _finalize_ending(storage, memory, world_id, directive, narration)
    return await _finalize_ending(storage, memory, world_id, directive, narration)


async def _finalize_ending(
    storage, memory, world_id: str, directive: NarrativeDirective, narration: str
) -> EndedTurn:
    """无锁终局固化主体：全盘固化 -> 读最终 recap -> 写快照 -> 世界归档。"""
    # 全盘固化：await 同步完成（非后台 fire-and-forget），失败不标记进度可整批重试
    try:
        await memory.consolidate(world_id)
    except Exception as e:  # noqa: BLE001
        raise EndingError(f"终局固化失败: {type(e).__name__}: {e}") from e
    recap = (storage.get_world(world_id) or {}).get("global_recap") or ""
    # 终局快照：仅存最终 recap + 结局类型 + 终局演播文本这一核心锚点
    try:
        await memory.write_ending_snapshot(
            world_id,
            recap=recap,
            ending_type=directive.ending_type,
            narration=narration,
            turn_num=directive.turn_num,
        )
    except Exception as e:  # noqa: BLE001
        raise EndingError(f"终局快照写入失败: {type(e).__name__}: {e}") from e
    # 世界归档：固化与快照全部成功后才置 ARCHIVED，杜绝半归档脏状态
    try:
        storage.update_world(world_id, status="ARCHIVED")
    except Exception as e:  # noqa: BLE001
        raise EndingError(f"世界归档失败: {type(e).__name__}: {e}") from e
    logger.info(
        "终局收尾完成 world=%s turn=%s ending=%s",
        world_id, directive.turn_num, directive.ending_type,
    )
    return EndedTurn(
        narration=narration,
        ending_type=directive.ending_type,
        recap=recap,
        turn_num=directive.turn_num,
    )


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
    memory: Optional[Any] = None,
    worker: Optional[Any] = None,
) -> NarratedTurn:
    """执行一轮完整管线：裁决 → 演播 → 落库，返回 NarratedTurn。

    director / narrator 可注入（测试用 fake 或定制）；缺省分别新建；
    llm 为两者共用的可调用对象（对齐 call_llm 签名，不传则各自动态解析）；
    recent_limit 为近程历史注入轮数，缺省取 config.context.assembler.recent_turns；
    on_turn_committed 为常规回合落库完成后的触发钩子（如通知后台固化 Worker），
    签名 (world_id, turn_num)，应尽快返回——长耗时逻辑请自行 create_task；
    钩子失败仅记日志，不影响本轮交付；终局轮不触发该钩子（走同步收尾固化）。
    memory / worker 为终局收尾所需——Director 判定 is_ending=True 时进入收尾管线
    （终局演播 + 全盘固化 + 快照 + 归档），memory 缺失抛 EndingError 无静默降级。
    流程：先 Director.run_turn 落库契约，再按是否终局分叉——终局走
    run_ending_wrapup 收尾并归档；常规走 Narrator 翻译、叙事覆盖 assistant 落库
    （手记存 directive 键）、触发 on_turn_committed 钩子。
    """
    if director is None:
        director = Director(
            storage, llm=llm, tier=tier, temperature=temperature, rng=rng,
            memory=memory,
        )
    if narrator is None:
        narrator = Narrator(llm=llm)
    # 状态：统一解析真实轮号（缺省取 next_turn_num，纯计算幂等无副作用），
    # 输入锚点与 Director 落库共用同一轮号，杜绝 player_input 事件塌缩到 turn 0
    turn = turn_num if turn_num is not None else storage.next_turn_num(world_id)
    # 状态：发布玩家输入事件到 TraceBus（每轮 trace 的输入锚点）
    await get_trace_bus().publish(make_player_input_event(
        action, world_id=world_id, turn_num=turn,
    ))
    # 状态：捕获本轮执行前的旧场景手记，供常规分支做场景切换差异检测（force 固化强信号）
    old_scene_notes = (storage.get_world(world_id) or {}).get("scene_notes") or ""
    directive = await director.run_turn(
        world_id, action, turn_num=turn
    )
    # 终局分支：Director 交卷带 is_ending=True，直接进入收尾管线，不再走常规演播/钩子
    if directive.is_ending:
        if memory is None:
            raise EndingError("终局收尾需要 memory 后端（当前未配置）")
        ended = await run_ending_wrapup(
            storage, world_id, directive,
            memory=memory, llm=llm, narrator=narrator, worker=worker,
        )
        # 状态：发布终局演播文本事件到 TraceBus，供 WebUI 双 Agent 对比区实时渲染
        await get_trace_bus().publish(make_narration_event(
            ended.narration, world_id=world_id, turn_num=directive.turn_num,
        ))
        return NarratedTurn(
            directive=directive,
            narration=ended.narration,
            ended=True,
            ending_type=ended.ending_type,
            recap=ended.recap,
        )
    if recent_limit is None:
        recent_limit = int(get_settings().get("context.assembler.recent_turns", 10))
    # 状态：近程历史剔除本轮——本轮手记已随契约下发，避免 Narrator 重复读到"守秘人：手记"
    recent = [
        t
        for t in storage.get_recent_turns(world_id, limit=recent_limit)
        if t["turn_num"] != directive.turn_num
    ]
    narration = await narrator.narrate(directive, recent=recent, action=action, world_id=world_id)
    logger.info(
        "Narrator 演播完成 world=%s turn=%s 叙事长度=%d",
        world_id, directive.turn_num, len(narration),
    )
    # 状态：玩家视角叙事覆盖 assistant，手记权威副本转存 directive 键，checks 保留
    storage.update_turn_context_data(
        world_id,
        directive.turn_num,
        assistant=narration,
        directive=directive.narrative_directive,
    )
    # 状态：发布演播文本事件到 TraceBus，供 WebUI 双 Agent 对比区实时渲染
    await get_trace_bus().publish(make_narration_event(
        narration, world_id=world_id, turn_num=directive.turn_num,
    ))
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
    # 状态：场景切换强信号——旧手记被整块重写（差异度超阈值）时触发 force 固化，
    # 把旧场景未固化轮次提炼进 RAG，随后手记被新场景自然接管；仅整块文本比较，
    # 不解析内容，worker 未配置或差异不足时静默跳过（见 docs/场景级工作上下文 §5）
    if worker is not None and _is_scene_transition(old_scene_notes, directive.scene_notes):
        worker.trigger_world(world_id, force=True)
    return NarratedTurn(directive=directive, narration=narration)
