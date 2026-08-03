# -*- coding: utf-8 -*-
"""
@File     :   fake.py
@Desc     :   测试/本地速跑用的假 LLM 客户端：零网络零延迟，按 tier 或消息内容返回预设响应
@Note     :   接口对齐 src.llm.client 的 call / call_stream / ask；
             responses 值可为 str / LLMResult / Exception / 可调用对象；
             每次调用都会记入 calls，便于断言调用次数与入参
"""

from __future__ import annotations

from typing import AsyncGenerator, Callable, Dict, List, Optional, Union

from .client import LLMResult

# 预设响应类型：纯文本 / 现成结果 / 异常(模拟失败) / 按消息动态生成
FakeResponse = Union[str, LLMResult, Exception, Callable[[List[dict]], str]]


class FakeLLM:
    """零成本的假 LLM，用于测试与本地快速体验。

    典型用法::

        fake = FakeLLM(responses={"standard": "我是预设回复"})
        result = await fake.call("standard", [{"role": "user", "content": "hi"}])

    预设匹配优先按 tier 精确查找，未命中时回退默认值；
    值若为 Exception 实例，则返回失败态 LLMResult 而非抛异常，方便测试容错分支。
    """

    def __init__(
        self,
        responses: Optional[Dict[str, FakeResponse]] = None,
        default_response: FakeResponse = "fake-ok",
        stream_chunk: int = 4,
    ) -> None:
        self._responses: Dict[str, FakeResponse] = dict(responses or {})
        self._default_response: FakeResponse = default_response
        self._stream_chunk = max(1, int(stream_chunk))
        # 调用历史：[{"tier", "messages", "kwargs"}]，供断言调用次数与入参
        self.calls: List[dict] = []

    def model_name(self, tier: str) -> str:
        """伪造的模型名，便于调用方区分 fake 与真实链路。"""
        return f"fake-{tier}"

    def set_response(self, tier: str, response: FakeResponse) -> None:
        """为指定 tier 设置或覆盖预设响应。"""
        self._responses[tier] = response

    def set_default(self, response: FakeResponse) -> None:
        """设置未命中 tier 时的默认响应。"""
        self._default_response = response

    def reset(self) -> None:
        """清空调用历史。"""
        self.calls.clear()

    def _resolve(self, tier: str, messages: List[dict]) -> FakeResponse:
        # 优先精确 tier，其次默认值（可调用则按消息动态生成）
        resp = self._responses.get(tier)
        if resp is None:
            resp = self._default_response
        if callable(resp) and not isinstance(resp, type):
            resp = resp(messages)
        return resp

    def _record(self, tier: str, messages: List[dict], kwargs: dict) -> None:
        self.calls.append({"tier": tier, "messages": messages, "kwargs": dict(kwargs)})

    async def call(self, tier: str, messages: List[dict], **kwargs) -> LLMResult:
        """对齐 call_llm：返回 LLMResult，零网络零延迟。"""
        self._record(tier, messages, kwargs)
        resp = self._resolve(tier, messages)
        if isinstance(resp, Exception):
            return LLMResult(
                text=None, tier=tier, model_name=self.model_name(tier),
                messages=messages, success=False, error=str(resp),
            )
        if isinstance(resp, LLMResult):
            return resp
        return LLMResult(
            text=str(resp), tier=tier, model_name=self.model_name(tier),
            messages=messages, success=True,
        )

    async def call_stream(self, tier: str, messages: List[dict], **kwargs) -> AsyncGenerator[str, None]:
        """对齐 call_llm_stream：按块切分预设文本逐段 yield，模拟流式。"""
        result = await self.call(tier, messages, **kwargs)
        if not result.is_ok or not result.text:
            return
        text: str = result.text
        for i in range(0, len(text), self._stream_chunk):
            yield text[i : i + self._stream_chunk]

    async def ask(self, tier: str, system_prompt: str, user_message: str, **kwargs) -> LLMResult:
        """对齐 ask_llm：拼 system + user 两条消息后调用 call。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        return await self.call(tier, messages, **kwargs)
