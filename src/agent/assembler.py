# -*- coding: utf-8 -*-
"""
@File     :   assembler.py
@Desc     :   Context Assembler——主 Agent 确定性上下文装配器：元认知指令 + 物理基础快照 + 近程对话
@Note     :   红线：零逻辑推演、零 RAG 预检索、纯 SQLite 确定性数据；场景/NPC/线索一律由主 Agent
             经 Function Calling 自主检索，装配器绝不预判；
             context_data 契约约定 user / assistant 两键；不维护 location 等场景元数据，
             场景与环境完全由 LLM 在叙事文本与检索中自然感知；
             为保证近程对话完整，主 Agent 每轮统一经 apply_turn_change 落库（空 diff 也写轮）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.core.config import get_settings
from src.core.exceptions import WorldNotFoundError

# ====================================================================
# 元认知指令（System）
# ====================================================================

# 默认元认知模板：身份定位 + 知识盲区断言 + 行动铁律，可被 assemble(system=) 整体覆盖
DEFAULT_SYSTEM = (
    "你是《克苏鲁的呼唤》跑团系统的总导演与规则裁决者。"
    "你的职责是依据规则做出判定、推动剧情走向并决定事实揭露；你不直接进行文学渲染，"
    "最终叙事由下游 Narrator 负责。"
    "你当前手头只有调查员的基础数值与宏观前情提要，不掌握任何场景细节、NPC 设定或模组秘密。"
    "行动铁律：若玩家行动触及未知的环境细节、NPC 反应或线索，必须调用 search_module 或 query_memory；"
    "涉及规则判定必须调用 check_and_update_stats；需要修改角色或环境状态时调用 manage_tags。"
    "调查员的属性、技能、背包与状态 Tag 已在本消息【调查员状态】中完整给出，"
    "严禁为获取 PC 自身数据调用任何检索工具；search_module 仅查模组剧情，query_memory 仅查长程记忆。"
    "严禁凭空脑补任何未检索确认的信息。"
    "NPC 扮演：手记可含「### NPC 扮演提示」小节——关键 NPC 写明人设与反应；"
    "次要 NPC 可略过，交由 Narrator 即兴确立性情细节，"
    "但不得违背其身份事实（职业/立场/关键秘密）与已确立的态度基调。"
    "当已获取足够信息支撑本轮裁决时，立即调用 present_directive 交卷结束本轮；"
    "严禁无谓地反复检索同一主题，也不要替下游 Narrator 渲染最终叙事。"
)


# ====================================================================
# 组装产物
# ====================================================================


@dataclass
class ContextBundle:
    """一次装配的产出：四段式结构，prompt 为可直接下发主 Agent 的完整 user 消息。"""

    system: str                         # 元认知 system 指令
    snapshot: str                       # Base Snapshot 文本（纯确定性数据）
    recent: str                         # 近程对话文本
    prompt: str                         # 完整 user 消息（snapshot + recent + 本轮行动）
    action: Optional[str] = None        # 本轮玩家行动（原样保留）
    pc_count: int = 0                   # 实际渲染的调查员数
    recent_count: int = 0               # 实际渲染的对话轮数

    @property
    def messages(self) -> List[Dict[str, str]]:
        """可直接喂给 run_tool_loop 的消息列表（system + user）。"""
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.prompt},
        ]


# ====================================================================
# 渲染
# ====================================================================


def _render_item(item: Any) -> str:
    """物品渲染：字典取 name 并附可选的 ammo 计数，标量直接转文本。"""
    if isinstance(item, dict):
        name = str(item.get("name", item))
        ammo = item.get("ammo")
        return f"{name}×{ammo}" if isinstance(ammo, int) else name
    return str(item)


# ====================================================================
# 角色背景渲染
# ====================================================================

# 背景字段规范：存储键 → 展示标签（有序渲染，与 glyphkeeper Character 字段键对齐）
BACKGROUND_FIELDS: tuple[tuple[str, str], ...] = (
    ("appearance_desc", "形象描述"),
    ("belief", "思想与信念"),
    ("significant_person", "重要之人"),
    ("significant_place", "意义非凡之地"),
    ("cherished_possession", "宝贵之物"),
    ("trait", "特质"),
    ("injury_scar", "伤口和疤痕"),
    ("phobias_manias", "恐惧症和躁狂症"),
    ("full_backstory", "背景故事"),
)

# 占位/空值，渲染时整项跳过
_BACKGROUND_EMPTY = frozenset({"", "无", "暂无", "-", "—", "N/A", "None"})


def render_background(background: Optional[Dict[str, Any]]) -> str:
    """把背景 JSON 渲染为长文段（仅含非空字段，逐项【标签】分行）。

    背景语义（重要）：这是调查员进入模组剧情**之前**的故事——人物底色、动机、
    羁绊与创伤；模组内新发生的事件属剧情记忆，应走 query_memory，绝不写回背景。
    返回空串表示无可渲染内容（调用方决定是否占位）。
    """
    if not background:
        return ""
    lines: List[str] = []
    for key, label in BACKGROUND_FIELDS:
        value = background.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in _BACKGROUND_EMPTY:
            continue
        lines.append(f"【{label}】{text}")
    return "\n".join(lines)


def _render_pc(pc: Dict[str, Any]) -> str:
    """渲染单个调查员硬数据：数值 + 技能表 + 动态 Tag + 背包物品清单。

    技能表必须渲染进快照——否则模型看不到 PC 技能值，会误用 query_memory 反复
    检索自身数据（真实链路触顶教训），浪费检索并陷入死循环。
    """
    parts = [
        f"- {pc['name']}（{pc['id']}）",
        f"  HP {pc['hp']}/{pc['hp_max']} | SAN {pc['san']}/{pc['san_max']} | "
        f"MP {pc['mp']}/{pc['mp_max']}",
    ]
    skills = pc.get("attributes_and_skills") or {}
    if skills:
        parts.append(f"  技能：{'、'.join(f'{k}{v}' for k, v in skills.items())}")
    if pc.get("tags"):
        parts.append(f"  状态：{'、'.join(pc['tags'])}")
    inv = pc.get("inventory") or []
    if inv:
        parts.append(f"  物品：{'、'.join(_render_item(i) for i in inv)}")
    return "\n".join(parts)


def _render_flags(game_phase: str, flags: Dict[str, Any]) -> str:
    """全局标志渲染：游戏阶段独立成行，其余键值平铺；全空则占位。"""
    if not flags and not game_phase:
        return "（暂无）"
    lines = []
    if game_phase:
        lines.append(f"当前阶段：{game_phase}")
    lines.extend(f"{k}={v}" for k, v in flags.items())
    return "\n".join(lines) or "（暂无）"


def _render_snapshot(world: Dict[str, Any], pcs: List[dict]) -> str:
    """Base Snapshot 文本：前情提要 + 全局标志 + 调查员状态。"""
    recap = (world.get("global_recap") or "").strip() or "（暂无前情提要）"
    section = ["【前情提要】", recap, "", "【全局标志】"]
    section.append(
        _render_flags(world.get("game_phase") or "", world.get("global_flags") or {})
    )
    if pcs:
        section += ["", "【调查员状态】"]
        for pc in pcs:
            section += _render_pc(pc).split("\n")
    else:
        section += ["", "【调查员状态】", "（暂无绑定调查员）"]
    # 角色背景不随快照注入：静态人物底稿由主 Agent 经 get_pc_background 按需查询，
    # 避免每轮默认烧 token（背景故事可能为大段散文，且不随回合变化）
    return "\n".join(section)


def _render_recent(turns: List[dict]) -> tuple[str, int]:
    """近程对话文本：剥离 Tool Call 中间过程，仅保留 玩家输入 - Narrator 输出。

    返回 (文本, 实际渲染轮数)；无对话内容的轮次（纯机械轮）跳过。
    """
    parts: List[str] = []
    count = 0
    for t in turns:
        cd = t.get("context_data") or {}
        user = cd.get("user")
        assistant = cd.get("assistant")
        if not user and not assistant:  # 状态：无对话内容的轮次跳过
            continue
        count += 1
        if user:
            parts.append(f"第 {t['turn_num']} 轮 玩家：{user}")
        if assistant:
            parts.append(f"守秘人：{assistant}")
    text = "\n".join(parts)
    return text or "（暂无历史对话）", count


# ====================================================================
# 装配入口
# ====================================================================


def _collect_pcs(storage, world_id: str, player_ids: List[str]) -> List[dict]:
    """按 world_state.player_ids 显式绑定读取调查员实体，绝不越权加载 NPC/怪物。"""
    pcs = []
    for pid in player_ids:
        entity = storage.get_entity(world_id, pid)
        if entity is not None:
            pcs.append(entity)
    return pcs


def _build_prompt(snapshot: str, recent: str, action: Optional[str]) -> str:
    """完整 user 消息：快照 + 近程对话 + 本轮行动（行动缺省则仅前两者）。"""
    parts = [snapshot, recent]
    if action:
        parts.append(f"【本轮行动】\n{action}")
    return "\n\n".join(p for p in parts if p)


def assemble(
    storage,
    world_id: str,
    *,
    action: Optional[str] = None,
    limit: Optional[int] = None,
    system: Optional[str] = None,
) -> ContextBundle:
    """装配主 Agent 初始上下文；世界不存在抛 WorldNotFoundError。

    limit 为近程对话注入轮数，缺省取 config.context.assembler.recent_turns（默认 10）；
    system 可覆盖默认元认知指令（测试与多场景定制用）。
    """
    world = storage.get_world(world_id)
    if world is None:
        raise WorldNotFoundError(f"世界不存在: {world_id}，请先 ensure_world 创建")
    meta = system if system is not None else DEFAULT_SYSTEM
    if limit is None:
        limit = int(get_settings().get("context.assembler.recent_turns", 10))
    turns = storage.get_recent_turns(world_id, limit=limit)
    pcs = _collect_pcs(storage, world_id, world.get("player_ids") or [])
    snapshot = _render_snapshot(world, pcs)
    recent, recent_count = _render_recent(turns)
    prompt = _build_prompt(snapshot, recent, action)
    return ContextBundle(
        system=meta,
        snapshot=snapshot,
        recent=recent,
        prompt=prompt,
        action=action,
        pc_count=len(pcs),
        recent_count=recent_count,
    )
