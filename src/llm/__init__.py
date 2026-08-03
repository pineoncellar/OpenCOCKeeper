# -*- coding: utf-8 -*-
"""LLM 调用层 — 统一封装对 OpenAI 兼容 API 的异步调用。

新架构下该包与 ``core`` / ``memory`` / ``storage`` 并列，提供:

- :func:`call_llm`      — 非流式调用，返回 :class:`LLMResult`
- :func:`call_llm_stream` — 流式调用，异步生成器逐段返回文本
- :func:`ask_llm`       — 单条消息快捷调用

模型分级（fast / standard / smart）与提供方密钥配置见 ``config.yaml`` +
``providers.ini``，均由 :func:`src.core.config.get_settings` 统一读取。
"""

from src.llm.client import LLMResult, ask_llm, call_llm, call_llm_stream
from src.llm.fake import FakeLLM

__all__ = [
    "LLMResult",
    "call_llm",
    "call_llm_stream",
    "ask_llm",
    "FakeLLM",
]
