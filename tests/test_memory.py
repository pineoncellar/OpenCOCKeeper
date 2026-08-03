# -*- coding: utf-8 -*-
"""
@File     :   test_memory.py
@Desc     :   记忆模块测试：固化流程（提炼/降级/进度/recap）、搜索（世界隔离/since_turn）、纯函数解析
@Note     :   走 conftest 独立临时库 + FakeLLM 注入；backend 一律用 FakeMemoryBackend，
             不触碰真实 Mem0 与网络
"""

from __future__ import annotations

import pytest

from src.core.exceptions import MemoryOperationError, WorldNotFoundError
from src.core.ids import make_world_id
from src.memory import FakeMemory, FakeMemoryBackend, Memory
from src.memory.interface import _parse_events


# ============================================
# 工具
# ============================================


def _append_turns(storage, world_id: str, count: int) -> None:
    """按轮次 1..count 顺序写入若干未固化轮次。"""
    for i in range(1, count + 1):
        storage.append_turn(
            world_id,
            turn_num=i,
            context_data={"user": f"玩家行动 {i}", "kp": f"守秘人回应 {i}"},
            state_diff={},
        )


def _dynamic_llm(messages) -> str:
    """按 prompt 区分事件提炼与 recap 两条路径，分别返回合法内容。"""
    content = messages[0]["content"]
    if "原子事件" in content:
        return (
            '[{"event": "队伍在耳室发现卡纳的日记", "turn": 1}, '
            '{"event": "费莉西蒂用魔能爆击败石皮兽2号", "turn": 2}]'
        )
    if "前情提要" in content:
        return "队伍进入墓穴，在耳室发现卡纳的日记，随后费莉西蒂击退石皮兽。"
    return "fake-ok"


# ============================================
# 固化
# ============================================


async def test_consolidate_extracts_and_marks(storage, world_id, fake_llm):
    fake_llm.set_response("standard", _dynamic_llm)
    _append_turns(storage, world_id, 2)
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)

    result = await memory.consolidate(world_id)
    assert result.events_written == 2
    assert result.recap_updated is True
    assert result.turns_solidified == [1, 2]
    assert result.ok is True
    # 宏观 recap 已写回 SQLite
    assert storage.get_world(world_id)["global_recap"] == "队伍进入墓穴，在耳室发现卡纳的日记，随后费莉西蒂击退石皮兽。"

    # 进度落库：再次固化无增量，不重复写 RAG
    again = await memory.consolidate(world_id)
    assert again.events_written == 0
    assert again.turns_solidified == []
    assert again.ok is False


async def test_consolidate_bad_json_raises(storage, world_id, fake_llm):
    # LLM 返回不可解析内容 → 抛 MemoryOperationError，且不标记进度、可整批重试
    fake_llm.set_response("standard", "not-a-json")
    _append_turns(storage, world_id, 3)
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)

    with pytest.raises(MemoryOperationError):
        await memory.consolidate(world_id)
    # 副作用未发生：RAG 未写、轮次仍未固化
    assert storage.get_unsolidified_turns(world_id)  # 非空即保留待重试批次


async def test_consolidate_llm_error_raises(storage, world_id, fake_llm):
    # LLM 整体失败 → 抛 MemoryOperationError，旧 recap 未被污染
    storage.update_world(world_id, global_recap="旧前情提要")
    fake_llm.set_response("standard", RuntimeError("boom"))
    _append_turns(storage, world_id, 2)
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)

    with pytest.raises(MemoryOperationError):
        await memory.consolidate(world_id)
    assert storage.get_world(world_id)["global_recap"] == "旧前情提要"
    assert len(storage.get_unsolidified_turns(world_id)) == 2


async def test_consolidate_recap_failure_raises_without_side_effects(storage, world_id, fake_llm):
    # 事件提炼成功但 recap 生成空白 → 抛错，且 RAG 事件未写入（副作用后置）
    backend = FakeMemoryBackend()

    def llm(messages):
        if "前情提要" in messages[0]["content"]:
            return "   "  # 状态：recap 空白触发失败
        return '[{"event": "队伍发现卡纳的日记", "turn": 1}]'

    fake_llm.set_response("standard", llm)
    _append_turns(storage, world_id, 1)
    memory = Memory(backend=backend, storage=storage)

    with pytest.raises(MemoryOperationError):
        await memory.consolidate(world_id)
    assert backend.count(world_id) == 0  # RAG 未写入
    assert storage.get_world(world_id)["global_recap"] == ""
    assert len(storage.get_unsolidified_turns(world_id)) == 1


async def test_consolidate_up_to_turn(storage, world_id, fake_llm):
    fake_llm.set_response("standard", _dynamic_llm)
    _append_turns(storage, world_id, 5)
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)

    first = await memory.consolidate(world_id, up_to_turn=3)
    assert first.turns_solidified == [1, 2, 3]
    # 剩余 4、5 仍可继续固化
    rest = await memory.consolidate(world_id)
    assert rest.turns_solidified == [4, 5]


async def test_consolidate_no_unsolidified_returns_empty(storage, world_id):
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)
    result = await memory.consolidate(world_id)
    assert result.events_written == 0
    assert result.turns_solidified == []
    assert result.ok is False


async def test_consolidate_requires_world(storage):
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)
    with pytest.raises(WorldNotFoundError):
        await memory.consolidate(make_world_id(900, "nope"))


# ============================================
# 搜索
# ============================================


async def test_search_is_world_isolated(storage, world_id):
    other = make_world_id(900, "other")
    storage.ensure_world(other)
    backend = FakeMemoryBackend()
    backend.add_events(
        [{"text": "队伍发现卡纳的日记"}], world_id=world_id, batch_turn_nums=[1]
    )
    backend.add_events(
        [{"text": "另一世界的无关内容"}], world_id=other, batch_turn_nums=[1]
    )
    memory = Memory(backend=backend, storage=storage)

    hits = await memory.search("卡纳", world_id)
    assert [h.text for h in hits] == ["队伍发现卡纳的日记"]


async def test_search_since_turn(storage, world_id):
    backend = FakeMemoryBackend()
    backend.add_events(
        [{"text": "旧事件"}, {"text": "新事件", "turn": 5}],
        world_id=world_id,
        batch_turn_nums=[1],
    )
    memory = Memory(backend=backend, storage=storage)

    hits = await memory.search("事件", world_id, since_turn=3)
    assert [h.turn_num for h in hits] == [5]


async def test_search_top_k_limit(storage, world_id):
    backend = FakeMemoryBackend()
    backend.add_events(
        [
            {"text": "线索一"},
            {"text": "线索二"},
            {"text": "线索三"},
        ],
        world_id=world_id,
        batch_turn_nums=[1],
    )
    memory = Memory(backend=backend, storage=storage)

    hits = await memory.search("线索", world_id, top_k=2)
    assert len(hits) == 2


async def test_search_requires_world(storage):
    memory = Memory(backend=FakeMemoryBackend(), storage=storage)
    with pytest.raises(WorldNotFoundError):
        await memory.search("任何", make_world_id(900, "nope"))


# ============================================
# 纯函数解析
# ============================================


def test_parse_events_variants():
    assert _parse_events('```json\n[{"event": "A", "turn": 3}]\n```') == [
        {"text": "A", "turn": 3}
    ]
    assert _parse_events('["a", "b"]') == [{"text": "a"}, {"text": "b"}]
    assert _parse_events("not-json") == []


# ============================================
# 假门面速跑
# ============================================


async def test_fake_memory_end_to_end(storage, world_id):
    _append_turns(storage, world_id, 2)
    memory = FakeMemory(storage=storage)

    result = await memory.consolidate(world_id)
    assert result.events_written == 2
    assert memory.backend.count(world_id) == 2

    hits = await memory.search("玩家行动", world_id)
    assert hits
    assert "玩家行动 1" in hits[0].text
