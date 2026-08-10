# -*- coding: utf-8 -*-
"""
@File     :   test_rollback.py
@Desc     :   按 turn_num 物理回档测试：storage.undo_from 单事务撤销 / 后端 delete_since
             / Memory.undo 门面 / rollback_world 双侧编排与幂等重试 / worker 不复活
@Note     :   全程 FakeMemoryBackend + FakeLLM + 真实 Storage（tmp 库），零网络零向量库；
             与 test_memory.py 共用 conftest 独立库隔离，不触碰真实 data/app.db
"""

from __future__ import annotations

import pytest

from src.core.exceptions import TurnNotFoundError
from src.memory import (
    ConsolidationWorker,
    FakeMemory,
    FakeMemoryBackend,
    Memory,
    rollback_world,
)


# ============================================
# 工具
# ============================================


def _append_turns(storage, world_id: str, count: int) -> None:
    """按轮次 1..count 顺序写入若干未固化轮次（空 diff）。"""
    for i in range(1, count + 1):
        storage.append_turn(
            world_id,
            turn_num=i,
            context_data={"user": f"玩家行动 {i}", "kp": f"守秘人回应 {i}"},
            state_diff={},
        )


def _three_events_llm(messages):
    """按 prompt 区分事件提炼与 recap 两条路径，返回 3 条绑定轮次的事件。"""
    content = messages[0]["content"]
    if "原子事件" in content:
        return (
            '[{"event": "事件一", "turn": 1}, '
            '{"event": "事件二", "turn": 2}, '
            '{"event": "事件三", "turn": 3}]'
        )
    if "前情提要" in content:
        return "前三轮事件摘要。"
    return "fake-ok"


# ============================================
# Storage 层：undo_from
# ============================================


async def test_undo_from_reverses_and_removes(storage, world_id, fake_llm):
    """单事务撤销 >= N 轮：状态倒序反转、记录批量删除、返回被撤销轮次。"""
    storage.create_entity(world_id, "pc_01", "PC", "费莉西蒂", san=58, san_max=70)
    storage.commit_turn(world_id, 1, state_diff={"numeric_changes": {"pc_01.san": -1}})
    storage.commit_turn(world_id, 2, state_diff={"numeric_changes": {"pc_01.san": -2}})
    storage.commit_turn(world_id, 3, state_diff={"numeric_changes": {"pc_01.san": -3}})
    assert storage.get_entity(world_id, "pc_01")["san"] == 52

    undone = storage.undo_from(world_id, 2)
    assert [t["turn_num"] for t in undone] == [2, 3]
    # 状态：撤销 turn 2/3（+2/+3），turn 1 的 -1 保留 → 58-1+5 = 57
    assert storage.get_entity(world_id, "pc_01")["san"] == 57
    assert storage.get_turn(world_id, 2) is None
    assert storage.get_turn(world_id, 3) is None
    assert storage.get_turn(world_id, 1) is not None


def test_undo_from_missing_turn_raises(storage, world_id, fake_llm):
    """目标轮次已被窗口裁剪/不存在：抛 TurnNotFoundError，不静默吞掉。"""
    storage.append_turn(world_id, turn_num=1, context_data={"user": "u"})
    with pytest.raises(TurnNotFoundError):
        storage.undo_from(world_id, 5)


# ============================================
# 后端与门面：delete_since / undo
# ============================================


def test_backend_delete_since(storage, world_id, fake_llm):
    """FakeMemoryBackend 按 turn_num >= N 剔除，缺失 turn 条目保留。"""
    backend = FakeMemoryBackend()
    backend.add_events(
        [
            {"text": "a", "turn": 1},
            {"text": "b", "turn": 2},
            {"text": "c", "turn": 3},
            {"text": "d"},  # 无 turn → 绑定批内最大轮 3
        ],
        world_id=world_id,
        batch_turn_nums=[1, 2, 3],
    )
    assert backend.count(world_id) == 4
    n = backend.delete_since(world_id, 2)
    assert n == 3  # b(2)/c(3)/d(3) 被删，a(1) 保留
    assert backend.count(world_id) == 1


async def test_memory_undo_clears_search(storage, world_id, fake_llm):
    """固化后 Memory.undo：RAG 中 >= N 的记忆消失，早于 N 的保留。"""
    fake_llm.set_response("standard", _three_events_llm)
    _append_turns(storage, world_id, 3)
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)
    result = await memory.consolidate(world_id)
    assert result.events_written == 3

    n = await memory.undo(world_id, 2)
    assert n == 2  # turn 2/3 记忆被删
    hits = await memory.search("事件", world_id, top_k=10)
    assert len(hits) == 1  # 只剩 turn 1


async def test_memory_undo_clear_all(storage, world_id, fake_llm):
    """turn_num <= 1：过滤器匹配并清空该世界全部 RAG 记忆。"""
    fake_llm.set_response("standard", _three_events_llm)
    _append_turns(storage, world_id, 3)
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)
    await memory.consolidate(world_id)

    n = await memory.undo(world_id, 1)
    assert n == 3
    assert not (await memory.search("事件", world_id, top_k=10))


async def test_fake_memory_undo(storage, world_id, fake_llm):
    """FakeMemory（假门面）也提供 undo 转发，离线全链路可直接走它。"""
    fake = FakeMemory(storage=storage)
    _append_turns(storage, world_id, 2)
    await fake.consolidate(world_id)
    assert fake._backend.count(world_id) == 2

    n = await fake.undo(world_id, 2)
    assert n == 1
    assert fake._backend.count(world_id) == 1


# ============================================
# 上层编排：rollback_world
# ============================================


async def test_rollback_world_both_sides(storage, world_id, fake_llm):
    """先 SQLite 后 RAG：物理状态与语义记忆同步回归。"""
    storage.create_entity(world_id, "pc_01", "PC", "费莉西蒂", san=58, san_max=70)
    fake_llm.set_response("standard", _three_events_llm)
    for i in range(1, 4):
        storage.commit_turn(
            world_id, i, state_diff={"numeric_changes": {"pc_01.san": -1}}
        )
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)
    await memory.consolidate(world_id)
    assert len(await memory.search("事件", world_id, top_k=10)) == 3

    deleted = await rollback_world(storage, memory, world_id, 2)
    assert deleted == 2  # RAG 删 turn>=2
    # 状态：SQLite turn 2/3 删除，san 回滚到只保留 turn 1 的 -1 → 58-1+2 = 57
    assert storage.get_turn(world_id, 2) is None
    assert storage.get_turn(world_id, 3) is None
    assert storage.get_entity(world_id, "pc_01")["san"] == 57
    # 状态：RAG 只剩 turn 1 记忆
    assert len(await memory.search("事件", world_id, top_k=10)) == 1


async def test_rollback_world_idempotent_retry(storage, world_id, fake_llm):
    """半完成态重试：SQLite 目标轮次已删时跳过物理段、仅补 RAG，不抛错。"""
    fake_llm.set_response("standard", _three_events_llm)
    _append_turns(storage, world_id, 3)
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)
    await memory.consolidate(world_id)

    # 状态：模拟首次回档中途失败——SQLite 段已完成、RAG 未清
    storage.undo_from(world_id, 2)
    deleted = await rollback_world(storage, memory, world_id, 2)
    assert deleted == 2  # 仅补 RAG
    assert len(await memory.search("事件", world_id, top_k=10)) == 1


async def test_worker_no_resurrect_after_rollback(storage, world_id, fake_llm):
    """回档后后台 Worker 再扫描：不把已撤销轮次重新固化写回 RAG。"""
    fake_llm.set_response("standard", _three_events_llm)
    _append_turns(storage, world_id, 3)
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)
    worker = ConsolidationWorker(memory, storage=storage, min_turns=2)

    await worker._process_pending()
    assert len(await memory.search("事件", world_id, top_k=10)) == 3

    await rollback_world(storage, memory, world_id, 2, worker=worker)
    assert storage.get_turn(world_id, 2) is None

    # 状态：SQLite 已无被撤销轮次，Worker 扫描不会产生增量写回
    await worker._process_pending()
    assert len(await memory.search("事件", world_id, top_k=10)) == 1


async def test_worker_shared_lock_identity(storage, world_id, fake_llm):
    """get_world_lock 返回同一世界同一把锁，rollback 与固化共享互斥。"""
    worker = ConsolidationWorker(
        FakeMemory(storage=storage), storage=storage, min_turns=1
    )
    assert worker.get_world_lock(world_id) is worker.get_world_lock(world_id)
