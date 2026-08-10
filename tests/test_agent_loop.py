# -*- coding: utf-8 -*-
"""
@File     :   test_agent_loop.py
@Desc     :   主 Agent Function Calling 闭环测试：schema 注入剥离、ToolRunner 分发与
             state_diff 收集、run_tool_loop 收敛/触顶/失败路径（全程 fake LLM 零网络）
@Note     :   复用 conftest 的 storage/world_id/_tmp_modules/fake_llm 夹具；
             check_and_update_stats 走真实工具实现但注入固定随机源保证确定性
"""

from __future__ import annotations

import random

import pytest

from src.agent import (
    ToolRunner,
    build_default_runner,
    build_main_agent_schemas,
    build_tool_schemas,
    run_tool_loop,
)
from src.agent.schemas import tool_names


# ====================================================================
# 工具 schema 注册
# ====================================================================


def test_main_agent_schemas_includes_present_directive():
    """主 Agent 工具清单：5 原子工具 + present_directive 收尾工具。"""
    schemas = build_main_agent_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert len(schemas) == 6
    assert names[-1] == "present_directive"


def test_build_tool_schemas_has_five_tools():
    schemas = build_tool_schemas()
    assert len(schemas) == 5
    names = {s["function"]["name"] for s in schemas}
    assert names == set(tool_names())


def test_schema_strips_injected_keys():
    stats = next(
        s for s in build_tool_schemas()
        if s["function"]["name"] == "check_and_update_stats"
    )
    params = stats["function"]["parameters"]
    assert "world_id" not in params["properties"]
    assert "turn_num" not in params["properties"]
    assert "entity_id" in params["properties"]
    assert "world_id" not in params["required"]
    assert "entity_id" in params["required"]


def test_schema_each_function_shape():
    for s in build_tool_schemas():
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["description"]
        assert fn["parameters"]["type"] == "object"
        assert "properties" in fn["parameters"]


# ====================================================================
# ToolRunner 分发
# ====================================================================


def _seed_pc(storage, world_id):
    """在测试世界建一个 PC 实体，供 stats/tags 工具操作。"""
    storage.create_entity(
        world_id, "pc_01", "PC", "测试调查员",
        hp=10, hp_max=12, san=60, san_max=99,
        attributes_and_skills={"侦查": 60},
        tags=["清醒"],
    )


async def test_runner_registers_default_tools(storage):
    runner = build_default_runner(storage)
    assert set(runner.names()) == set(tool_names())


async def test_runner_unknown_tool_returns_error(storage, world_id):
    runner = build_default_runner(storage)
    out = await runner.execute("no_such_tool", {}, world_id=world_id, turn_num=1)
    assert out["ok"] is False
    assert "未知工具" in out["error"]


async def test_runner_search_module_hits(storage, world_id):
    runner = build_default_runner(storage)
    out = await runner.execute(
        "search_module", {"query": "测试模组"}, world_id=world_id, turn_num=1
    )
    assert out["ok"] is True
    assert isinstance(out["hits"], list)


async def test_runner_query_memory_without_backend(storage, world_id):
    runner = build_default_runner(storage, memory=None)
    out = await runner.execute(
        "query_memory", {"queries": ["测试"]}, world_id=world_id, turn_num=1
    )
    assert out["ok"] is False  # 未配置记忆后端，返回可读错误而非抛出


async def test_runner_stats_strips_diff(storage, world_id):
    _seed_pc(storage, world_id)
    runner = build_default_runner(storage, rng=random.Random(42))
    runner.reset_diffs()
    runner.reset_checks()
    out = await runner.execute(
        "check_and_update_stats",
        {"entity_id": "pc_01", "skill_or_attribute": "侦查", "difficulty": "regular"},
        world_id=world_id, turn_num=1,
    )
    assert out["ok"] is True
    assert out["summary"]
    assert "state_diff" not in out  # 状态：diff 已抽出，不回填模型
    assert len(runner.collected_diffs) == 1  # 空 diff 也收集（本轮无状态变更）
    # 状态：检定结果保留权威副本（回填模型用于写手记，副本供 Narrator 核对）
    assert "check" in out  # 模型仍可见检定结果
    assert len(runner.collected_checks) == 1
    check = runner.collected_checks[0]
    assert check["entity_id"] == "pc_01"  # 副本带实体标识
    assert isinstance(check["roll_value"], int)
    assert isinstance(check["threshold"], int)
    assert check["success_level"] in {"CRITICAL", "EXTREME", "HARD", "REGULAR", "FAILURE", "FUMBLE"}
    assert check["success_level_label"]
    assert isinstance(check["is_success"], bool)


async def test_runner_tags_collects_diff(storage, world_id):
    _seed_pc(storage, world_id)
    runner = build_default_runner(storage)
    runner.reset_diffs()
    runner.reset_checks()
    out = await runner.execute(
        "manage_tags",
        {"entity_id": "pc_01", "add_tags": ["流血"], "remove_tags": ["清醒"]},
        world_id=world_id, turn_num=1,
    )
    assert out["ok"] is True
    assert "state_diff" not in out
    assert len(runner.collected_diffs) == 1
    assert "pc_01" in runner.collected_diffs[0]["tags"]
    assert runner.collected_checks == []  # 无检定工具不产生 check


# ====================================================================
# run_tool_loop 闭环
# ====================================================================


def _fake_llm_step(tool_payloads, final_text="收敛：检定成功，剧情推进。"):
    """构造 fake 响应函数：首轮返回 tool_calls，回填后收敛为文本。"""

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return final_text
        return {"text": None, "tool_calls": list(tool_payloads)}

    return step


async def test_tool_loop_converges_with_two_tools(storage, world_id, fake_llm):
    _seed_pc(storage, world_id)
    runner = build_default_runner(storage)
    tools = build_tool_schemas()
    fake_llm.set_response(
        "smart",
        _fake_llm_step(
            [
                {"id": "call_1", "name": "manage_tags",
                 "arguments": {"entity_id": "pc_01", "add_tags": ["流血"]}},
                {"id": "call_2", "name": "check_and_update_stats",
                 "arguments": {"entity_id": "pc_01", "hp_change": -3}},
            ]
        ),
    )
    result = await run_tool_loop(
        fake_llm.call, "smart", [{"role": "user", "content": "调查员搜索房间"}],
        tools, runner, world_id=world_id, turn_num=1,
    )
    assert result.converged is True
    assert result.iterations == 2  # 首轮工具 + 回填后收敛
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0]["name"] == "manage_tags"
    assert result.final.is_ok
    assert result.final.text == "收敛：检定成功，剧情推进。"
    # 两个工具各产出一个 diff（manage_tags 真实增删 + stats hp 变更）
    assert len(runner.collected_diffs) == 2
    assert runner.collected_diffs[1]["numeric_changes"]["pc_01.hp"] == -3
    # hp 变更无检定段 → 无检定结果副本
    assert runner.collected_checks == []


async def test_tool_loop_injects_world_and_turn(world_id, fake_llm):
    # 用自定义 runner 捕获注入参数，验证 world_id/turn_num 确实传给工具
    runner = ToolRunner()
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "echo": kwargs}

    runner.register("spy", spy)
    fake_llm.set_response(
        "smart", _fake_llm_step([{"id": "c", "name": "spy", "arguments": {"x": 1}}])
    )
    result = await run_tool_loop(
        fake_llm.call, "smart", [{"role": "user", "content": "hi"}],
        build_tool_schemas(), runner, world_id=world_id, turn_num=7,
    )
    assert result.converged is True
    assert seen.get("world_id") == world_id
    assert seen.get("turn_num") == 7
    assert seen.get("x") == 1


async def test_tool_loop_hits_max_iterations(storage, world_id, fake_llm):
    runner = build_default_runner(storage)
    tools = build_tool_schemas()

    def always_tool(messages):
        return {"text": None, "tool_calls": [
            {"id": "c", "name": "manage_tags",
             "arguments": {"entity_id": "ghost", "add_tags": ["x"]}},
        ]}

    fake_llm.set_response("smart", always_tool)
    result = await run_tool_loop(
        fake_llm.call, "smart", [{"role": "user", "content": "hi"}],
        tools, runner, world_id=world_id, turn_num=1, max_iterations=3,
    )
    assert result.converged is False
    assert result.iterations == 3
    assert not result.final.is_ok
    assert "未收敛" in result.final.error
    # 幽灵实体触发 EntityNotFoundError，但以错误镜像回填不中断循环
    assert result.tool_calls[-1]["output"]["ok"] is False


async def test_tool_loop_unknown_tool_then_converges(storage, world_id, fake_llm):
    runner = build_default_runner(storage)
    tools = build_tool_schemas()
    fake_llm.set_response(
        "smart",
        _fake_llm_step([{"id": "c", "name": "no_such_tool", "arguments": {}}]),
    )
    result = await run_tool_loop(
        fake_llm.call, "smart", [{"role": "user", "content": "hi"}],
        tools, runner, world_id=world_id, turn_num=1,
    )
    assert result.converged is True
    assert result.tool_calls[0]["output"] == {"ok": False, "error": "未知工具: no_such_tool"}


async def test_tool_loop_llm_failure_returns_early(world_id, fake_llm):
    runner = build_default_runner(None)
    tools = build_tool_schemas()
    fake_llm.set_response("smart", RuntimeError("boom"))
    result = await run_tool_loop(
        fake_llm.call, "smart", [{"role": "user", "content": "hi"}],
        tools, runner, world_id=world_id, turn_num=1,
    )
    assert result.converged is False
    assert result.iterations == 1
    assert not result.final.is_ok
    assert "boom" in result.final.error


async def test_tool_loop_injects_converge_hint_after_many_searches(world_id, fake_llm):
    """检索类工具反复调用超阈值后，收敛提示并入首条 system 引导模型收尾。"""
    runner = ToolRunner()
    runner.register("search_module", lambda **kw: {"ok": True, "echo": kw})
    tools = build_tool_schemas()

    def always_search(messages):
        return {"text": None, "tool_calls": [
            {"id": "c", "name": "search_module", "arguments": {"query": "x"}}]}

    fake_llm.set_response("smart", always_search)
    result = await run_tool_loop(
        fake_llm.call, "smart",
        [{"role": "system", "content": "你是守秘人"}, {"role": "user", "content": "hi"}],
        tools, runner, world_id=world_id, turn_num=1,
        max_iterations=6, hint_after=2,
    )
    assert result.converged is False
    # 提示并入首条 system；不得在 tool 消息后新增独立 system（避免模型误读复读）
    assert "present_directive" in result.final.messages[0]["content"]
    assert not any(
        i > 0 and m.get("role") == "system"
        for i, m in enumerate(result.final.messages)
    )


async def test_tool_loop_stop_tool_converges_and_extracts(storage, world_id, fake_llm):
    """stop_tool_name 命中即交卷：收尾工具不执行、不回填，参数写入 stop_call。"""
    _seed_pc(storage, world_id)
    runner = build_default_runner(storage)
    tools = build_main_agent_schemas()

    def step(messages):
        if any(m["role"] == "tool" for m in messages):
            return {"text": None, "tool_calls": [
                {"id": "c2", "name": "present_directive",
                 "arguments": {"narrative_directive": "### 规则裁决\n- 侦查成功"}}]}
        return {"text": None, "tool_calls": [
            {"id": "c1", "name": "manage_tags",
             "arguments": {"entity_id": "pc_01", "add_tags": ["流血"]}}]}

    fake_llm.set_response("smart", step)
    result = await run_tool_loop(
        fake_llm.call, "smart", [{"role": "user", "content": "搜索房间"}],
        tools, runner, world_id=world_id, turn_num=1,
        stop_tool_name="present_directive",
    )
    assert result.converged is True
    assert result.stop_call is not None
    assert result.stop_call["name"] == "present_directive"
    assert result.stop_call["arguments"]["narrative_directive"] == "### 规则裁决\n- 侦查成功"
    # 只执行了首轮 manage_tags，收尾工具不执行
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "manage_tags"
