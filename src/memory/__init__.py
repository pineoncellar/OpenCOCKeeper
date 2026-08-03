# -*- coding: utf-8 -*-
"""记忆模块 — 只提供"记忆固化"与"语义搜索"两个核心接口，其余细节由调用方编排。

固化（consolidate）: 从 recent_turns 未固化轮次提炼原子事件写入 RAG（微观），
                   融合旧前情提要刷新 global_recap 写回 SQLite（宏观），
                   并把处理过的轮次标记为已固化，保证只处理增量。
搜索（search）:    带 world_id 强制过滤的 Top-K 语义召回，与硬状态彻底解耦；
                  query_memory 为主 Agent 专用入口，支持多变体合并召回。

门面 :class:`Memory` 的 backend 可注入真实 Mem0（backend.py）或
内存假后端（fake.py）；测试 / 本地速跑建议走 FakeMemory 或
Memory + FakeMemoryBackend + FakeLLM 组合。
前置自检见 :func:`preflight`——事件处理流启动时先跑一遍，FAIL 即中止任务。
"""

from src.memory.backend import Mem0Memory, MemoryBackend
from src.memory.fake import FakeMemory, FakeMemoryBackend
from src.memory.interface import ConsolidateResult, Memory, MemoryHit
from src.memory.preflight import PreflightCheck, PreflightReport, preflight

__all__ = [
    "Memory",
    "MemoryHit",
    "ConsolidateResult",
    "MemoryBackend",
    "Mem0Memory",
    "FakeMemory",
    "FakeMemoryBackend",
    "preflight",
    "PreflightCheck",
    "PreflightReport",
]
