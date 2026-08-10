# -*- coding: utf-8 -*-
"""
@File     :   test_worker.py
@Desc     :   后台固化 Worker 测试：生命周期 / 阈值判定 / 时间兜底 / force / 失败重试不丢轮
             / trigger_world 事件触发 / pipeline on_turn_committed 钩子
@Note     :   全程 FakeMemory + 真实 Storage（tmp 库）与 FakeLLM，不触碰真实 Mem0 与网络；
             worker 阈值显式传入覆盖默认，不依赖 config 环境
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.agent import Director, Narrator, run_narrated_turn
from src.core.exceptions import MemoryOperationError
from src.memory import ConsolidationWorker, FakeMemory
from src.memory.interface import ConsolidateResult

_HANDOFF = "### 规则裁决\n- 侦查成功（18/60），SAN -1，挂载 Tag [手臂流血]。"


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


def _make_worker(storage, memory=None, **kw):
    """构造 worker：缺省 FakeMemory + 显式阈值，避免依赖 config。"""
    if memory is None:
        memory = FakeMemory(storage=storage)
    return ConsolidationWorker(memory, storage=storage, **kw)


def _unsolidified_count(storage, world_id: str) -> int:
    return len(storage.get_unsolidified_turns(world_id))


def _seed_pc(storage, world_id, san=58):
    storage.update_world(
        world_id, player_ids=["pc_01"], global_recap="调查员抵达阿卡姆。"
    )
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=10, hp_max=12, san=san, san_max=70,
        attributes_and_skills={"侦查": 60},
    )


def _step_stats_then_directive():
    """主 Agent 模拟：先检定扣血，回填后 present_directive 交卷。"""
    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [
                {"id": "c2", "name": "present_directive",
                 "arguments": {"narrative_directive": _HANDOFF}}]}
        return {"text": None, "tool_calls": [
            {"id": "c1", "name": "check_and_update_stats",
             "arguments": {"entity_id": "pc_01", "skill_or_attribute": "侦查",
                           "san_change": -1}}]}

    return step


class _FlakyMemory:
    """第一次固化抛错、之后成功的假门面，验证失败不丢轮可重试。"""

    def __init__(self, storage):
        self._storage = storage
        self.fail_first = True
        self.calls = 0

    async def consolidate(self, world_id, *, up_to_turn=None):
        self.calls += 1
        if self.fail_first:
            raise MemoryOperationError("boom")  # 状态：模拟固化失败
        turns = self._storage.get_unsolidified_turns(world_id)
        nums = [t["turn_num"] for t in turns]
        self._storage.mark_turns_solidified(world_id, nums)
        return ConsolidateResult(
            world_id=world_id,
            turns_solidified=nums,
            events_written=len(nums),
            recap_updated=True,
        )


# ============================================
# 生命周期
# ============================================


async def test_worker_lifecycle(storage, world_id, fake_llm):
    """start/stop/is_running 与重复 start 防御。"""
    worker = _make_worker(storage, min_turns=1)
    assert worker.is_running is False
    task = asyncio.create_task(worker.start())
    await asyncio.sleep(0.02)  # 状态：让轮询循环进入等待
    assert worker.is_running is True
    await worker.start()  # 状态：重复 start 直接返回，不炸
    await worker.stop()
    await asyncio.sleep(0)  # 状态：让取消传播、循环退出
    assert worker.is_running is False
    assert task.done()


# ============================================
# 阈值判定
# ============================================


async def test_worker_threshold_skips_below_min(storage, world_id, fake_llm):
    """未固化轮数不足 min_turns：跳过固化，不标记进度。"""
    _append_turns(storage, world_id, 2)
    worker = _make_worker(storage, min_turns=5)
    await worker._process_pending()
    assert _unsolidified_count(storage, world_id) == 2
    assert worker._consolidated_count == 0


async def test_worker_threshold_consolidates_at_min(storage, world_id, fake_llm):
    """未固化轮数达到 min_turns：固化并清空进度。"""
    _append_turns(storage, world_id, 3)
    worker = _make_worker(storage, min_turns=3)
    await worker._process_pending()
    assert _unsolidified_count(storage, world_id) == 0
    assert worker._consolidated_count == 1
    assert worker._last_results[world_id]["turns"] == [1, 2, 3]


async def test_worker_time_based_fallback(storage, world_id, fake_llm):
    """轮数不足但距上次固化超时：时间兜底触发。"""
    _append_turns(storage, world_id, 2)
    worker = _make_worker(storage, min_turns=5, min_interval=10)
    worker._last_consolidated[world_id] = time.time() - 1000  # 状态：久未固化
    await worker._process_pending()
    assert _unsolidified_count(storage, world_id) == 0


async def test_worker_force_ignores_threshold(storage, world_id, fake_llm):
    """force=True 无视阈值强制固化（场景切换等强信号预留）。"""
    _append_turns(storage, world_id, 1)
    worker = _make_worker(storage, min_turns=5)
    await worker._maybe_consolidate(world_id, force=True)
    assert _unsolidified_count(storage, world_id) == 0


# ============================================
# 失败语义与事件触发
# ============================================


async def test_worker_failure_retry_no_loss(storage, world_id, fake_llm):
    """consolidate 失败：不标记进度、不计数；修复后下轮整批重试成功。"""
    _append_turns(storage, world_id, 3)
    flaky = _FlakyMemory(storage)
    worker = _make_worker(storage, memory=flaky, min_turns=3)
    await worker._process_pending()
    assert _unsolidified_count(storage, world_id) == 3  # 状态：失败未丢轮
    assert worker._consolidated_count == 0
    flaky.fail_first = False
    await worker._process_pending()
    assert _unsolidified_count(storage, world_id) == 0  # 状态：重试成功
    assert worker._consolidated_count == 1


async def test_worker_trigger_world_consolidates(storage, world_id, fake_llm):
    """trigger_world 事件驱动：立即安排固化，不阻塞调用方。"""
    _append_turns(storage, world_id, 3)
    worker = _make_worker(storage, min_turns=3)
    worker._running = True  # 状态：手动置运行态以测触发（不真正跑轮询循环）
    worker.trigger_world(world_id)
    await asyncio.sleep(0)  # 状态：让后台固化任务执行
    assert _unsolidified_count(storage, world_id) == 0
    worker._running = False


async def test_worker_trigger_world_idle_ignored(storage, world_id, fake_llm):
    """worker 未运行：trigger_world 忽略，不创建后台任务。"""
    _append_turns(storage, world_id, 3)
    worker = _make_worker(storage, min_turns=3)
    worker.trigger_world(world_id)
    await asyncio.sleep(0)
    assert _unsolidified_count(storage, world_id) == 3  # 状态：未固化


# ============================================
# pipeline 钩子
# ============================================


async def test_run_narrated_turn_hook(storage, world_id, fake_llm):
    """on_turn_committed 钩子：落库后收到 (world_id, turn_num)。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", _step_stats_then_directive())
    fake_llm.set_response("standard", "大厅内弥漫着陈旧的尘土气息……")
    director = Director(storage, llm=fake_llm.call, tier="smart")
    narrator = Narrator(llm=fake_llm.call)
    calls = []

    async def hook(wid, tn):
        calls.append((wid, tn))

    turn = await run_narrated_turn(
        storage, world_id, "调查员推门进入旅馆大厅",
        director=director, narrator=narrator, on_turn_committed=hook,
    )
    assert calls == [(world_id, turn.directive.turn_num)]


async def test_run_narrated_turn_hook_failure_ok(storage, world_id, fake_llm):
    """钩子抛异常：仅记日志，管线仍正常交付。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", _step_stats_then_directive())
    fake_llm.set_response("standard", "大厅内弥漫着陈旧的尘土气息……")
    director = Director(storage, llm=fake_llm.call, tier="smart")
    narrator = Narrator(llm=fake_llm.call)

    async def bad_hook(wid, tn):
        raise RuntimeError("hook down")

    turn = await run_narrated_turn(
        storage, world_id, "调查员推门进入旅馆大厅",
        director=director, narrator=narrator, on_turn_committed=bad_hook,
    )
    assert turn.narration
    assert turn.directive.converged is True
