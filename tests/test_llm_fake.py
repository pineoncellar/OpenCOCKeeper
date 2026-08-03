# -*- coding: utf-8 -*-
"""
@File     :   test_llm_fake.py
@Desc     :   FakeLLM 客户端测试：预设匹配、动态生成、异常模拟、调用历史、流式切块
@Note     :   全程零网络，只验证 src.llm.fake 自身行为
"""

from __future__ import annotations

import pytest

from src import llm as llm_module
from src.llm.client import LLMResult
from src.llm.fake import FakeLLM

MSGS = [{"role": "user", "content": "你好"}]


async def test_call_returns_default(fake_llm):
    result = await fake_llm.call("standard", MSGS)
    assert result.is_ok
    assert result.text == "fake-ok"
    assert result.tier == "standard"
    assert result.model_name == "fake-standard"


async def test_call_per_tier_response():
    fake = FakeLLM(responses={"fast": "快速回复", "smart": "聪明回复"})
    fast = await fake.call("fast", MSGS)
    smart = await fake.call("smart", MSGS)
    assert fast.text == "快速回复"
    assert smart.text == "聪明回复"
    # 未配置的 tier 回退默认值
    other = await fake.call("standard", MSGS)
    assert other.text == "fake-ok"


async def test_call_callable_response_by_message():
    def echo(messages):
        return f"echo:{messages[0]['content']}"

    fake = FakeLLM(default_response=echo)
    result = await fake.call("standard", MSGS)
    assert result.text == "echo:你好"


async def test_call_exception_simulates_failure():
    fake = FakeLLM(responses={"standard": ValueError("模拟模型故障")})
    result = await fake.call("standard", MSGS)
    assert not result.is_ok
    assert result.text is None
    assert "模拟模型故障" in (result.error or "")


async def test_call_passthrough_llmresult():
    preset = LLMResult(text="预置结果", tier="standard", model_name="fake-standard", messages=MSGS)
    fake = FakeLLM(responses={"standard": preset})
    result = await fake.call("standard", MSGS)
    assert result is preset


async def test_call_records_history(fake_llm):
    await fake_llm.call("fast", MSGS, temperature=0.1)
    await fake_llm.call("standard", MSGS)
    assert len(fake_llm.calls) == 2
    assert fake_llm.calls[0]["tier"] == "fast"
    assert fake_llm.calls[0]["kwargs"] == {"temperature": 0.1}
    assert fake_llm.calls[0]["messages"] == MSGS


async def test_reset_clears_history(fake_llm):
    await fake_llm.call("standard", MSGS)
    fake_llm.reset()
    assert fake_llm.calls == []


async def test_call_stream_chunks_by_block():
    fake = FakeLLM(responses={"standard": "一二三四五六"}, stream_chunk=2)
    chunks = [c async for c in fake.call_stream("standard", MSGS)]
    assert chunks == ["一二", "三四", "五六"]


async def test_call_stream_failure_yields_nothing():
    fake = FakeLLM(responses={"standard": RuntimeError("boom")})
    chunks = [c async for c in fake.call_stream("standard", MSGS)]
    assert chunks == []


async def test_ask_builds_two_messages():
    fake = FakeLLM(responses={"standard": "ok"})
    result = await fake.ask("standard", "你是助手", "帮我")
    assert result.is_ok
    assert result.text == "ok"
    last = fake.calls[-1]["messages"]
    assert last[0] == {"role": "system", "content": "你是助手"}
    assert last[1] == {"role": "user", "content": "帮我"}


# ====================================================================
# 测试环境自动注入：模块级入口默认走 FakeLLM
# ====================================================================


async def test_module_entry_uses_fake_by_default(fake_llm):
    """未标 real_llm 的测试里，from src import llm 的 call_llm 自动走 fake。"""
    fake_llm.set_response("standard", "auto-fake")
    result = await llm_module.call_llm("standard", MSGS)
    assert result.is_ok
    assert result.text == "auto-fake"
    assert fake_llm.calls and fake_llm.calls[-1]["tier"] == "standard"


async def test_module_stream_uses_fake_by_default(fake_llm):
    fake_llm.set_response("smart", "ABCDEF")
    chunks = [c async for c in llm_module.call_llm_stream("smart", MSGS)]
    assert "".join(chunks) == "ABCDEF"


async def test_module_ask_uses_fake_by_default(fake_llm):
    fake_llm.set_response("fast", "快捷回复")
    result = await llm_module.ask_llm("fast", "系统", "用户")
    assert result.is_ok
    assert result.text == "快捷回复"
