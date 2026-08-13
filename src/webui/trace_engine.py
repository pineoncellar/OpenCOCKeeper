#- encoding: utf-8 -#
#
# @File     :   trace_engine.py
# @Desc     :   内存 Trace 事件总线 — 桥接 llm_trace 与 WebUI SSE 的实时通道
# @Note     :   生产端（llm/client.py + agent/loop.py）通过 publish 入队，
#              消费端（SSE 端点）通过 subscribe 持续 yield；asyncio.Queue
#              解耦文件 IO 与文件锁，实现秒级实时推送。全局单例 get_trace_bus()
#

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator, List, Optional


# ====================================================================
# TraceEvent — 一次 LLM 交互或工具调用的结构化记录
# ====================================================================


@dataclass
class TraceEvent:
    """一次 LLM 交互或工具调用的结构化记录，供 SSE 推送与前端渲染。

    event_type 取值:
      llm_request   — LLM 请求发送前（含完整 messages 与 tools）
      llm_response  — LLM 响应接收后（含 content 与 tool_calls）
      tool_call     — 工具调用前（含 name 与 arguments）
      tool_result   — 工具执行返回后（含 result 摘要）
      converge      — 闭环收敛/收尾工具命中
      directive     — Director 交卷提取的导演手记（narrative_directive）
      narration     — Narrator 演播的玩家可见叙事文本
    """
    timestamp: str
    event_type: str
    world_id: str
    turn_num: int
    data: dict = field(default_factory=dict)


# ====================================================================
# TraceBus — 内存事件总线（asyncio.Queue 实现）
# ====================================================================


class TraceBus:
    """内存事件总线：生产者入队，消费者出队，多路广播。

    使用 asyncio.Queue 解耦文件 IO，生产端 publish 不阻塞，消费端
    subscribe 可同时被多个 SSE 连接消费（各连接独立迭代器）。
    maxsize 防内存堆积，超限丢弃最早事件。
    """

    def __init__(self, maxsize: int = 500) -> None:
        self._queue: asyncio.Queue[TraceEvent] = asyncio.Queue(maxsize)
        self._snapshot: List[TraceEvent] = []  # 状态：非破坏性快照，供初始加载
        self._max_snapshot: int = 200

    async def publish(self, event: TraceEvent) -> None:
        """发布事件：入队 + 写入快照环；队列满时丢弃最早事件。"""
        self._snapshot.append(event)
        if len(self._snapshot) > self._max_snapshot:
            self._snapshot = self._snapshot[-self._max_snapshot:]
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # 状态：队列满，丢弃最早一条腾空间
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except asyncio.QueueEmpty:
                pass

    async def subscribe(self) -> AsyncGenerator[TraceEvent, None]:
        """持续 yield 新事件，队列为空时 await。"""
        while True:
            yield await self._queue.get()

    def get_snapshot(self, limit: int = 200) -> List[TraceEvent]:
        """获取当前快照（非破坏性读取），供前端初始加载历史事件。"""
        return self._snapshot[-limit:]


# 全局单例
_TRACE_BUS: Optional[TraceBus] = None


def get_trace_bus() -> TraceBus:
    """获取全局 TraceBus 单例（惰性初始化）。"""
    global _TRACE_BUS
    if _TRACE_BUS is None:
        _TRACE_BUS = TraceBus()
    return _TRACE_BUS


# ====================================================================
# 便捷工厂
# ====================================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def make_llm_request_event(
    tier: str, messages: list, tools: Optional[list],
    world_id: str = "", turn_num: int = 0,
) -> TraceEvent:
    """构造 LLM 请求事件：记录完整 messages 与 tools 清单。"""
    return TraceEvent(
        timestamp=_now(),
        event_type="llm_request",
        world_id=world_id,
        turn_num=turn_num,
        data={
            "tier": tier,
            "messages": messages,
            "tool_names": [t.get("function", {}).get("name", "") for t in tools]
            if tools else None,
        },
    )


def make_llm_response_event(
    result, tier: str,
    world_id: str = "", turn_num: int = 0,
) -> TraceEvent:
    """构造 LLM 响应事件：记录响应摘要与 tool_calls。"""
    return TraceEvent(
        timestamp=_now(),
        event_type="llm_response",
        world_id=world_id,
        turn_num=turn_num,
        data={
            "tier": tier,
            "success": result.success,
            "content": result.text,
            "tool_calls": result.tool_calls,
            "error": result.error,
        },
    )


def make_tool_call_event(
    name: str, arguments: dict,
    world_id: str = "", turn_num: int = 0,
) -> TraceEvent:
    """构造工具调用事件：记录工具名与参数。"""
    return TraceEvent(
        timestamp=_now(),
        event_type="tool_call",
        world_id=world_id,
        turn_num=turn_num,
        data={"name": name, "arguments": arguments},
    )


def make_tool_result_event(
    name: str, result: dict,
    world_id: str = "", turn_num: int = 0,
) -> TraceEvent:
    """构造工具返回事件：记录执行结果摘要。"""
    return TraceEvent(
        timestamp=_now(),
        event_type="tool_result",
        world_id=world_id,
        turn_num=turn_num,
        data={"name": name, "ok": result.get("ok"), "result": result},
    )


def make_converge_event(
    reason: str, tool_calls_count: int,
    world_id: str = "", turn_num: int = 0,
) -> TraceEvent:
    """构造收敛事件：记录收敛原因与工具调用总数。"""
    return TraceEvent(
        timestamp=_now(),
        event_type="converge",
        world_id=world_id,
        turn_num=turn_num,
        data={"reason": reason, "tool_calls_count": tool_calls_count},
    )


def make_directive_event(
    directive: str,
    world_id: str = "", turn_num: int = 0,
) -> TraceEvent:
    """构造导演手记事件：记录 Director 交卷提取的叙事决策大纲（Markdown 自由文本）。"""
    return TraceEvent(
        timestamp=_now(),
        event_type="directive",
        world_id=world_id,
        turn_num=turn_num,
        data={"directive": directive},
    )


def make_narration_event(
    narration: str,
    world_id: str = "", turn_num: int = 0,
) -> TraceEvent:
    """构造演播文本事件：记录 Narrator 输出的玩家可见叙事（终局/常规通用）。"""
    return TraceEvent(
        timestamp=_now(),
        event_type="narration",
        world_id=world_id,
        turn_num=turn_num,
        data={"narration": narration},
    )