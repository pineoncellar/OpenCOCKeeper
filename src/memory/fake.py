# -*- coding: utf-8 -*-
"""
@File     :   fake.py
@Desc     :   假记忆后端/假门面：内存存储 + 关键词打分模拟语义相关度，零网络零依赖
@Note     :   接口对齐 MemoryBackend 协议与 Memory 门面；世界按 world_id 分桶隔离，
             测试与本地速跑用，生产请换 Mem0Memory（见 backend.py）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.db import get_db
from ..core.exceptions import WorldNotFoundError
from ..storage.storage import Storage
from .interface import (
    ConsolidateResult,
    MemoryHit,
    _first_location,
    _merge_query_hits,
    _render_turn,
)


# ============================================
# 假记忆后端
# ============================================


class FakeMemoryBackend:
    """内存版记忆后端：写入按世界分桶，搜索用关键词包含命中打分。

    仅用于测试与本地速跑，不做向量计算；
    世界隔离与真实 Mem0 一致，跨世界数据互不可见。
    """

    def __init__(self) -> None:
        # 桶结构：{world_id: [{"id","text","turn_num","location"}]}
        self._store: Dict[str, List[Dict[str, Any]]] = {}
        self._seq = 0
        self.add_calls = 0

    def add_events(
        self,
        events: List[Dict[str, Any]],
        *,
        world_id: str,
        batch_turn_nums: List[int],
        location: Optional[str] = None,
    ) -> None:
        """把一批事件写入指定世界桶；事件未带 turn 时绑定本批最大轮次。"""
        default_turn = max(batch_turn_nums) if batch_turn_nums else None
        bucket = self._store.setdefault(world_id, [])
        for event in events:
            text = event.get("text")
            if not text:
                continue
            turn = event.get("turn") if event.get("turn") is not None else default_turn
            self._seq += 1
            bucket.append(
                {
                    "id": f"mem_{self._seq}",
                    "text": str(text),
                    "turn_num": turn,
                    "location": location,
                }
            )
        self.add_calls += 1

    def search_topk(
        self,
        query: str,
        *,
        world_id: str,
        top_k: int,
        since_turn: Optional[int] = None,
    ) -> List[MemoryHit]:
        """关键词包含命中打分，按分数降序取前 top_k；可选按轮次下限过滤。"""
        bucket = self._store.get(world_id, [])
        words = [w for w in query.split() if w]
        scored: List[tuple] = []
        for item in bucket:
            if since_turn is not None and (
                item["turn_num"] is None or item["turn_num"] < since_turn
            ):
                continue  # 状态：早于 since_turn 的记忆被过滤
            score = _keyword_score(item["text"], words)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            MemoryHit(
                text=item["text"],
                turn_num=item["turn_num"],
                location=item["location"],
                score=score,
                memory_id=item["id"],
            )
            for score, item in scored[:top_k]
        ]

    def count(self, world_id: str) -> int:
        """某个世界桶内的记忆条数，供测试断言。"""
        return len(self._store.get(world_id, []))

    async def close(self) -> None:
        """清空内存桶（幂等）。"""
        self._store.clear()


def _keyword_score(text: str, words: List[str]) -> float:
    """用查询词的包含命中率模拟相关度；无词时按整句包含给保底。"""
    if not words:
        return 1.0 if text else 0.0
    hit = sum(1 for w in words if w in text)
    return hit / len(words)


# ============================================
# 假记忆门面
# ============================================


class FakeMemory:
    """零依赖假门面：接口对齐 Memory，固化直接吞轮次、搜索走内存后端。

    适合本地速跑整套 RAG 流程（不调 LLM、不装向量库）；
    要验证门面真实逻辑（LLM 提炼 / recap / 进度标记）请用
    Memory(backend=FakeMemoryBackend()) + FakeLLM 组合。
    """

    def __init__(self, storage: Optional[Storage] = None) -> None:
        self._storage = storage or Storage(db=get_db())
        self._backend = FakeMemoryBackend()

    async def consolidate(
        self,
        world_id: str,
        *,
        up_to_turn: Optional[int] = None,
    ) -> ConsolidateResult:
        """与 Memory.consolidate 同签名；不调 LLM，直接把每轮原文当事件入库。"""
        self._require_world(world_id)
        turns = self._storage.get_unsolidified_turns(
            world_id, up_to_turn=up_to_turn
        )
        result = ConsolidateResult(world_id=world_id)
        if not turns:
            return result  # 状态：无增量
        turn_nums = [t["turn_num"] for t in turns]
        events = [
            {"text": _render_turn(t), "turn": t["turn_num"]} for t in turns
        ]
        self._backend.add_events(
            events,
            world_id=world_id,
            batch_turn_nums=turn_nums,
            location=_first_location(turns),
        )
        result.events_written = len(events)
        self._storage.mark_turns_solidified(world_id, turn_nums)
        result.turns_solidified = turn_nums
        return result

    async def search(
        self,
        query: str,
        world_id: str,
        *,
        top_k: int = 5,
        since_turn: Optional[int] = None,
    ) -> List[MemoryHit]:
        """与 Memory.search 同签名，转发到内存后端。"""
        self._require_world(world_id)
        return self._backend.search_topk(
            query,
            world_id=world_id,
            top_k=max(1, int(top_k)),
            since_turn=since_turn,
        )

    async def query_memory(
        self,
        queries,
        world_id: str,
        *,
        top_k: int = 8,
        since_turn: Optional[int] = None,
    ) -> List[MemoryHit]:
        """与 Memory.query_memory 同签名：多变体召回合并，走内存后端。"""
        self._require_world(world_id)
        qs = [queries] if isinstance(queries, str) else list(queries)
        if not qs:
            return []  # 状态：空 query 列表，直接返回
        hit_lists = [
            self._backend.search_topk(
                q, world_id=world_id, top_k=max(1, int(top_k)), since_turn=since_turn
            )
            for q in qs
        ]
        return _merge_query_hits(hit_lists, top_k)

    def _require_world(self, world_id: str) -> None:
        if self._storage.get_world(world_id) is None:
            raise WorldNotFoundError(f"世界不存在: {world_id}")

    @property
    def backend(self) -> FakeMemoryBackend:
        """暴露内存后端，测试里直接断言写入条数与内容。"""
        return self._backend

    async def close(self) -> None:
        await self._backend.close()
