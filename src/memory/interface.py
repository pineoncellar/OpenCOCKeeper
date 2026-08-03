# -*- coding: utf-8 -*-
"""
@File     :   interface.py
@Desc     :   记忆门面：只暴露"固化"与"搜索"两个核心接口，底层 RAG 后端可替换
@Note     :   固化=从 recent_turns 未固化轮次提炼原子事件写 RAG（微观）+
             融合旧前情提要刷新 global_recap 写回 SQLite（宏观）；
             搜索=带 world_id 强制过滤的 Top-K 语义召回，绝不掺硬数值；
             backend 需实现 MemoryBackend 协议（见 backend.py），测试走 fake.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Union

from ..core.config import get_settings
from ..core.db import get_db
from ..core.exceptions import MemoryOperationError, WorldNotFoundError
from ..core.log import get_logger
from ..llm import call_llm as _default_call_llm
from ..storage.storage import Storage

logger = get_logger(__name__)

# 事件提炼 prompt：要求 LLM 输出 JSON 数组，每条为一句完整的话
_EVENT_EXTRACT_PROMPT = (
    "你是 COC 跑团守秘人的记忆提炼器。下面给出最近几轮的对话与状态记录，"
    "请提炼出对后续剧情有长期影响的\"原子事件\"。"
    "每条事件用一句完整的话描述，只保留剧情事实、互动结果与场景细节，"
    "不要写 HP/MP/SAN、技能数值与道具清单（那些由 SQLite 管理）。"
    '用 JSON 数组返回，元素形如 {"event": "事件描述", "turn": 轮次号}，'
    "turn 取该事件最相关的轮次号，不要输出数组以外的任何内容。\n\n"
    "待提炼的记录：\n"
)

# 宏观 recap prompt：融合旧前情提要与新事件，生成最新全局前情提要
_RECAP_PROMPT = (
    "你是 COC 跑团守秘人的前情提要整理器。请把\"旧前情提要\"与\"新发生的事件\""
    "融合成一份不超过 {max} 字的全局前情提要，只输出正文，不要编号与标题。\n"
    "旧前情提要：\n{old}\n\n新发生的事件：\n{new}\n"
)


# ============================================
# 返回值类型
# ============================================


@dataclass
class MemoryHit:
    """一条语义记忆命中：文本 + 来源轮次/地点 + 相关度分数。"""
    text: str
    turn_num: Optional[int] = None
    location: Optional[str] = None
    score: float = 0.0
    memory_id: Optional[str] = None


@dataclass
class ConsolidateResult:
    """一次固化操作的产出汇总，供调用方写日志 / 驱动后续动作。"""
    world_id: str
    turns_solidified: List[int] = field(default_factory=list)
    events_written: int = 0
    recap_updated: bool = False

    @property
    def ok(self) -> bool:
        """本次固化是否产出了任何内容（写了事件或刷新了 recap）。"""
        return self.events_written > 0 or self.recap_updated

    def brief(self) -> dict:
        """轻量摘要，供日志使用。"""
        return {
            "world_id": self.world_id,
            "turns": self.turns_solidified,
            "events": self.events_written,
            "recap": self.recap_updated,
        }


# ============================================
# Memory 门面
# ============================================


class Memory:
    """记忆门面：固化与搜索两个接口，RAG 后端经 backend 注入可替换。

    固化接口 consolidate 先读未固化轮次，再把轮次内容交给 LLM 提炼原子事件
    写入 RAG（微观），随后融合旧 recap 生成最新前情提要写回 SQLite（宏观），
    最后把处理过的轮次标记为已固化，保证下次只处理增量。
    搜索接口 search 只做带 world_id 过滤的 Top-K 召回，与硬状态彻底解耦。
    """

    def __init__(
        self,
        *,
        backend: Optional[Any] = None,
        storage: Optional[Storage] = None,
        llm=None,
        tier: Optional[str] = None,
        recap_max_chars: int = 500,
    ) -> None:
        # backend 缺省构建真实 Mem0 后端；环境未就绪（缺 embedding 端点）时构造期即抛清晰错误
        if backend is None:
            from .backend import Mem0Memory

            backend = Mem0Memory.from_config()
        self._backend = backend
        self._storage = storage or Storage(db=get_db())
        # llm 运行时从 src.llm 包属性读取，conftest monkeypatch 后即走 fake，
        # 避免 import 时提前绑定导致测试注入失效
        self._llm = llm if llm is not None else self._resolve_llm()
        self._tier = str(
            tier or get_settings().get("memory.rag.llm_tier", "standard")
        )
        self._recap_max_chars = int(recap_max_chars)

    @staticmethod
    def _resolve_llm():
        """运行时解析默认 LLM 入口，保证测试注入的 fake 能生效。"""
        import src.llm as llm_module

        return getattr(llm_module, "call_llm", _default_call_llm)

    # ---- 核心接口：固化 ----

    async def consolidate(
        self,
        world_id: str,
        *,
        up_to_turn: Optional[int] = None,
    ) -> ConsolidateResult:
        """固化指定世界尚未固化的轮次到语义记忆，返回产出汇总。

        up_to_turn 非空时只固化到该轮次（含），缺省固化全部未固化轮次；
        没有未固化轮次时直接返回空结果，不产生任何 RAG 写入。
        任一步失败（LLM 提炼 / recap 生成 / 后端写入）都抛 MemoryOperationError，
        且不标记进度，上层事件处理流捕获后可整批重试。
        """
        self._require_world(world_id)
        turns = self._storage.get_unsolidified_turns(
            world_id, up_to_turn=up_to_turn
        )
        result = ConsolidateResult(world_id=world_id)
        if not turns:
            return result  # 状态：无增量，直接返回

        turn_nums = [t["turn_num"] for t in turns]
        # 第一步：LLM 计算全部前置，任一失败立即抛 MemoryOperationError，
        # 此时未写 RAG / 未写 recap / 未标记进度，整批可干净重试  # 状态：LLM 计算
        events = await self._extract_events(turns)  # 微观：提炼原子事件
        new_recap = await self._compose_recap(world_id, events)  # 宏观：生成前情提要

        # 第二步：副作用写入——写 RAG 原子事件、写回 global_recap、标记固化进度
        batch_location = _first_location(turns)
        self._backend.add_events(
            events,
            world_id=world_id,
            batch_turn_nums=turn_nums,
            location=batch_location,
        )
        self._storage.update_world(world_id, global_recap=new_recap)
        self._storage.mark_turns_solidified(world_id, turn_nums)

        result.events_written = len(events)
        result.recap_updated = True
        result.turns_solidified = turn_nums
        return result

    # ---- 核心接口：搜索 ----

    async def search(
        self,
        query: str,
        world_id: str,
        *,
        top_k: int = 5,
        since_turn: Optional[int] = None,
    ) -> List[MemoryHit]:
        """按 query 在世界内做 Top-K 语义召回，返回按相关度排序的命中列表。

        world_id 强制过滤，后端据此隔离，杜绝跨世界污染；
        since_turn 非空时只召回 >= 该轮次的记忆，供撤销后回看新剧情等场景使用。
        """
        self._require_world(world_id)
        hits = self._backend.search_topk(
            query,
            world_id=world_id,
            top_k=max(1, int(top_k)),
            since_turn=since_turn,
        )
        return list(hits)

    # ---- 核心接口：主 Agent 检索 ----

    async def query_memory(
        self,
        queries: Union[str, List[str]],
        world_id: str,
        *,
        top_k: int = 8,
        since_turn: Optional[int] = None,
    ) -> List[MemoryHit]:
        """主 Agent 专用检索入口：单条或多条语义化 query 合并召回。

        与 search 的差别在两点：默认 top_k 放宽到 8（容忍语义低排名命中），
        且接受多条 query 变体——主 Agent 可把一次感知拆成多个角度表述，
        逐条召回后按分数合并去重。实证表明描述性/多角度 query 的命中率
        显著高于单感官关键词。
        """
        qs = [queries] if isinstance(queries, str) else list(queries)
        if not qs:
            return []  # 状态：空 query 列表，直接返回
        hit_lists = [
            await self.search(q, world_id, top_k=top_k, since_turn=since_turn)
            for q in qs
        ]
        return _merge_query_hits(hit_lists, top_k)

    # ---- 内部：LLM 提炼与 recap ----

    async def _extract_events(self, turns: List[dict]) -> List[Dict[str, Any]]:
        """调 LLM 把一批轮次提炼为原子事件列表。

        每条事件为 {"text": str, "turn": int(可选)}；
        LLM 失败或返回不可解析内容时一律抛 MemoryOperationError，
        不做任何降级兜底——上层事件处理流靠异常中断后整批重试。
        """
        blocks = "\n".join(_render_turn(t) for t in turns)
        prompt = _EVENT_EXTRACT_PROMPT + blocks
        result = await self._llm(self._tier, [{"role": "user", "content": prompt}])
        if not result.is_ok:
            raise MemoryOperationError(
                f"事件提炼失败(tier={self._tier}): {result.error}"
            )
        events = _parse_events(result.text or "")
        if not events:
            raise MemoryOperationError(
                f"事件提炼返回不可解析内容(tier={self._tier}): {result.text!r}"
            )
        return events

    async def _compose_recap(
        self, world_id: str, events: List[Dict[str, Any]]
    ) -> str:
        """融合旧前情提要与新事件，生成最新 global_recap（纯计算，不落库）。

        LLM 失败或返回空白内容时抛 MemoryOperationError；
        由调用方在副作用阶段统一写回 SQLite，保证失败时不留半截状态。
        """
        world = self._storage.get_world(world_id)
        old_recap = (world or {}).get("global_recap") or ""
        new_text = "\n".join(e["text"] for e in events)
        prompt = _RECAP_PROMPT.format(
            old=old_recap, new=new_text, max=self._recap_max_chars
        )
        result = await self._llm(self._tier, [{"role": "user", "content": prompt}])
        if not result.is_ok:
            raise MemoryOperationError(
                f"宏观 recap 生成失败(tier={self._tier}): {result.error}"
            )
        text = (result.text or "").strip()
        if not text:
            raise MemoryOperationError("宏观 recap 生成了空白内容")
        return text

    # ---- 内部：校验 ----

    def _require_world(self, world_id: str) -> None:
        if self._storage.get_world(world_id) is None:
            raise WorldNotFoundError(f"世界不存在: {world_id}")

    async def close(self) -> None:
        """释放后端资源（真实 Mem0 断开连接），幂等可重复调用。"""
        close = getattr(self._backend, "close", None)
        if close is not None:
            await close()


# ============================================
# 纯函数辅助
# ============================================


def _merge_query_hits(hit_lists: List[List[MemoryHit]], top_k: int) -> List[MemoryHit]:
    """多变体召回结果合并：按 (text, turn_num) 去重，按 score 降序取前 top_k。"""
    seen: Set[tuple] = set()
    merged: List[MemoryHit] = []
    for hits in hit_lists:
        for h in hits:
            key = (h.text, h.turn_num)
            if key in seen:
                continue  # 状态：跨 query 重复命中，只保留第一条
            seen.add(key)
            merged.append(h)
    merged.sort(key=lambda h: h.score, reverse=True)
    return merged[:top_k]


def _render_turn(turn: dict) -> str:
    """把一轮 recent_turn 渲染成供 LLM 提炼的可读文本。"""
    num = turn.get("turn_num")
    ctx = turn.get("context_data") or {}
    lines = [f"第{num}轮"]
    if isinstance(ctx, dict):
        if ctx.get("location"):
            lines.append(f"地点: {ctx['location']}")
        for key, value in ctx.items():
            if key == "location":
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _first_location(turns: List[dict]) -> Optional[str]:
    """从一批轮次里取第一个出现的地点（按轮次顺序），用于事件元数据绑定。"""
    for turn in turns:
        ctx = turn.get("context_data") or {}
        if isinstance(ctx, dict) and ctx.get("location"):
            return str(ctx["location"])
    return None


def _parse_events(text: str) -> List[Dict[str, Any]]:
    """解析 LLM 返回的原子事件 JSON，兼容元素为 str 或 {"event","turn"} 两种形态。

    解析失败返回空列表，由调用方降级为原文入库存。
    """
    content = text.strip()
    if content.startswith("```"):  # 状态：剥掉可能的 markdown 代码块围栏
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:].strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    events: List[Dict[str, Any]] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            events.append({"text": item.strip()})
        elif isinstance(item, dict):
            text_val = item.get("event") or item.get("text")
            if isinstance(text_val, str) and text_val.strip():
                entry: Dict[str, Any] = {"text": text_val.strip()}
                if item.get("turn") is not None:
                    entry["turn"] = int(item["turn"])
                events.append(entry)
    return events
