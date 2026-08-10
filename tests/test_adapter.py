# -*- coding: utf-8 -*-
"""
@File     :   test_adapter.py
@Desc     :   适配器层测试：消息协议 / 命令路由 / 世界管理 / 回合管线 / 回档 / Worker 挂接
@Note     :   全程 fake LLM 零网络；conftest 每测试独立临时库 + FakeLLM 注入；
             adapter 命令路由基于 storage/memory/worker 直连，不依赖真实网络
"""

from __future__ import annotations

import asyncio

import pytest

from src.adapter.base import AbstractAdapter
from src.adapter.cli import CliAdapter
from src.adapter.protocol import InboundMessage, MessageType, OutboundMessage
from src.core.ids import make_world_id
from src.memory.fake import FakeMemory
from src.memory.worker import ConsolidationWorker

# 与 conftest 中临时模组目录预置的文件名保持一致（可被绑定校验通过）
TEST_MODULE_NAME = "test_module.docx"


# ============================================
# 消息协议
# ============================================


def test_protocol_factories():
    """入站/出站工厂：player_input / system_cmd / narrative / system_msg 类型与字段正确。"""
    pi = InboundMessage.player_input("调查员搜查房间", session_id="cli-default", world_id="world_001_x")
    assert pi.type == MessageType.PLAYER_INPUT
    assert pi.text == "调查员搜查房间"
    assert pi.world_id == "world_001_x"

    sc = InboundMessage.system_cmd("/status", session_id="cli-default")
    assert sc.type == MessageType.SYSTEM_CMD
    assert sc.text == "/status"

    n = OutboundMessage.narrative("叙事文本", session_id="cli-default")
    assert n.type == MessageType.NARRATIVE
    assert n.text == "叙事文本"

    s = OutboundMessage.system_msg("提示", level="warn", session_id="cli-default")
    assert s.type == MessageType.SYSTEM_MSG
    assert s.data["level"] == "warn"


# ============================================
# parse / 命令路由
# ============================================


async def test_parse_classifies_command_and_input(storage, world_id, fake_llm):
    """parse：/ 开头归 SYSTEM_CMD，其余归 PLAYER_INPUT，且映射会话当前世界。"""
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    adapter._world_id = world_id

    cmd = await adapter.parse("/status")
    assert cmd.type == MessageType.SYSTEM_CMD
    assert cmd.world_id == world_id

    action = await adapter.parse("调查员搜查房间")
    assert action.type == MessageType.PLAYER_INPUT
    assert action.text == "调查员搜查房间"
    assert action.world_id == world_id


async def test_handle_unknown_command(storage, fake_llm):
    """未知命令返回 warn 提示，不抛异常。"""
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    out = await adapter.handle(InboundMessage.system_cmd("/nope", session_id="s"))
    assert out.type == MessageType.SYSTEM_MSG
    assert out.data["level"] == "warn"


async def test_player_input_without_world(storage, fake_llm):
    """未选择世界时玩家输入被拦截并提示，不触发管线。"""
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    out = await adapter.handle(InboundMessage.player_input("调查员搜查房间", session_id="s"))
    assert out.type == MessageType.SYSTEM_MSG
    assert "未选择世界" in out.text


# ============================================
# 世界管理命令
# ============================================


async def test_world_start_creates_and_switches(storage, fake_llm):
    """/world start <模组名>：创建世界绑定模组文件，并切换为当前世界。"""
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    out = await adapter.handle(
        InboundMessage.system_cmd(f"/world start {TEST_MODULE_NAME}", session_id="s")
    )
    assert out.type == MessageType.SYSTEM_MSG
    assert "世界已创建" in out.text
    assert adapter._world_id is not None
    world = storage.get_world(adapter._world_id)
    assert world is not None
    assert world["module_name"] == TEST_MODULE_NAME


async def test_world_start_requires_module(storage, fake_llm):
    """/world start 不带模组名时给出用法提示，不创建世界。"""
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    out = await adapter.handle(InboundMessage.system_cmd("/world start", session_id="s"))
    assert out.type == MessageType.SYSTEM_MSG
    assert out.data["level"] == "warn"
    assert "用法" in out.text
    assert storage.list_worlds() == []


async def test_world_start_bad_module_rejected(storage, fake_llm):
    """/world start 绑定不存在的模组文件：抛错并返回 error 提示，不创建世界。"""
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    out = await adapter.handle(
        InboundMessage.system_cmd("/world start missing_module.pdf", session_id="s")
    )
    assert out.type == MessageType.SYSTEM_MSG
    assert out.data["level"] == "error"
    assert storage.list_worlds() == []


async def test_world_list_and_use(storage, world_id, fake_llm):
    """/world list 列出世界；/world use 切换当前世界。"""
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    listed = await adapter.handle(InboundMessage.system_cmd("/world list", session_id="s"))
    assert listed.type == MessageType.SYSTEM_MSG
    assert world_id in listed.text

    used = await adapter.handle(InboundMessage.system_cmd(f"/world use {world_id}", session_id="s"))
    assert "已切换" in used.text
    assert adapter._world_id == world_id


async def test_world_use_unknown(storage, fake_llm):
    """/world use 不存在的世界返回 warn。"""
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    out = await adapter.handle(
        InboundMessage.system_cmd("/world use world_999_nope", session_id="s")
    )
    assert out.type == MessageType.SYSTEM_MSG
    assert out.data["level"] == "warn"


# ============================================
# 状态查询
# ============================================


async def test_status_without_world(storage, fake_llm):
    """/status 未选择世界时提示。"""
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    out = await adapter.handle(InboundMessage.system_cmd("/status", session_id="s"))
    assert out.type == MessageType.SYSTEM_MSG
    assert "未选择世界" in out.text


async def test_status_shows_world_and_pc(storage, world_id, fake_llm):
    """/status 显示世界模组、最新轮次与 PC 实体。"""
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=8, hp_max=12, san=58, san_max=70,
        attributes_and_skills={"侦查": 60},
    )
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    adapter._world_id = world_id
    out = await adapter.handle(InboundMessage.system_cmd("/status", session_id="s"))
    assert out.type == MessageType.SYSTEM_MSG
    assert "费莉西蒂" in out.text
    assert "HP 8/12" in out.text


# ============================================
# 回合管线
# ============================================


async def _step_directive(narrative="### 规则裁决\n- 侦查成功"):
    """首轮直接 present_directive 交卷的 fake 响应（工具名须与 schemas 一致）。"""

    def step(messages):
        return {"text": None, "tool_calls": [
            {"id": "c1", "name": "present_directive",
             "arguments": {"narrative_directive": narrative}}]}

    return step


async def _step_stats_then_directive(narrative="### 规则裁决\n- 侦查成功（18/60）"):
    """首轮 check_and_update_stats 扣血（含检定），回填后 present_directive 交卷，产生 hp diff。"""

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [
                {"id": "c2", "name": "present_directive",
                 "arguments": {"narrative_directive": narrative}}]}
        return {"text": None, "tool_calls": [
            {"id": "c1", "name": "check_and_update_stats",
             "arguments": {"entity_id": "pc_01", "skill_or_attribute": "侦查", "hp_change": -3}}]}

    return step


async def test_player_input_runs_pipeline(storage, world_id, fake_llm):
    """玩家输入在有世界时触发回合管线，返回玩家视角叙事，并落库轮次。"""
    fake_llm.set_response("smart", await _step_directive())
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    adapter._world_id = world_id

    out = await adapter.handle(InboundMessage.player_input("调查员试图撬开暗门", session_id="s"))
    assert out.type == MessageType.NARRATIVE
    assert out.text  # 叙事非空
    assert storage.next_turn_num(world_id) == 2  # 状态：已落库 1 轮


async def test_pipeline_hook_triggers_worker(storage, world_id, fake_llm):
    """落库后 on_turn_committed 钩子触发 worker.trigger_world（事件触发通道接通）。"""
    fake_llm.set_response("smart", await _step_directive())
    fired = []

    class _WorkerStub:
        def trigger_world(self, wid, *, force=False):
            fired.append((wid, force))

    adapter = CliAdapter(storage=storage, llm=fake_llm.call, worker=_WorkerStub())
    adapter._world_id = world_id
    out = await adapter.handle(InboundMessage.player_input("调查员搜查房间", session_id="s"))
    assert out.type == MessageType.NARRATIVE
    assert fired == [(world_id, False)]


# ============================================
# 回档命令
# ============================================


async def test_rollback_requires_world(storage, fake_llm):
    """/rollback 未选择世界时提示。"""
    adapter = CliAdapter(storage=storage, llm=fake_llm.call)
    out = await adapter.handle(InboundMessage.system_cmd("/rollback", session_id="s"))
    assert out.type == MessageType.SYSTEM_MSG
    assert "未选择世界" in out.text


async def test_rollback_lists_and_executes(storage, world_id, fake_llm):
    """/rollback 无参列出最近轮次；/rollback <N> 双侧回档（SQLite + RAG）。"""
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=10, hp_max=12, san=58, san_max=70,
        attributes_and_skills={"侦查": 60},
    )
    fake = FakeMemory(storage=storage)  # 状态：假门面内部自带 FakeMemoryBackend
    fake_llm.set_response("smart", await _step_stats_then_directive())
    adapter = CliAdapter(storage=storage, memory=fake, llm=fake_llm.call)
    adapter._world_id = world_id

    hp_before = storage.get_entity(world_id, "pc_01")["hp"]  # 10
    # 跑一轮带扣血的回合，制造可回档的 hp diff
    await adapter.handle(InboundMessage.player_input("调查员撞破暗门", session_id="s"))
    assert storage.get_entity(world_id, "pc_01")["hp"] == hp_before - 3

    listed = await adapter.handle(InboundMessage.system_cmd("/rollback", session_id="s"))
    assert listed.type == MessageType.SYSTEM_MSG
    assert "最新轮次" in listed.text

    rolled = await adapter.handle(InboundMessage.system_cmd("/rollback 1", session_id="s"))
    assert rolled.type == MessageType.SYSTEM_MSG
    assert "已回档" in rolled.text
    # 物理状态回到回档前快照（本轮 hp 变更被撤销）
    assert storage.get_entity(world_id, "pc_01")["hp"] == hp_before
    assert storage.next_turn_num(world_id) == 1  # 状态：轮次已回退到空


# ============================================
# Worker 生命周期（runtime 编排）
# ============================================


async def test_worker_hook_binding(storage, world_id, fake_llm):
    """ConsolidationWorker 挂接：触发后进入待处理状态，不阻塞且失败不抛出。"""
    fake_llm.set_response("smart", await _step_directive())
    memory = FakeMemory(storage=storage)
    worker = ConsolidationWorker(memory=memory, storage=storage, worlds=[world_id])

    task = asyncio.create_task(worker.start())
    try:
        await asyncio.sleep(0)  # 状态：让 worker 进入等待循环
        adapter = CliAdapter(storage=storage, memory=memory, worker=worker, llm=fake_llm.call)
        adapter._world_id = world_id
        out = await adapter.handle(InboundMessage.player_input("调查员搜查房间", session_id="s"))
        assert out.type == MessageType.NARRATIVE
        assert worker.is_running  # 状态：事件触发不破坏 worker 生命周期
    finally:
        await worker.stop()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
