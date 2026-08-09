# -*- coding: utf-8 -*-
"""
@File     :   loop.py
@Desc     :   主 Agent 工具调度闭环：ToolRunner 注册分发 + run_tool_loop Function Calling 循环
@Note     :   run_tool_loop 把 tools 传给 LLM，模型返回 tool_calls 则逐个执行并回填 tool 消息，
             直到模型不再调用工具（收敛）或达到 max_iterations 触顶；调用方用 final.text
             取决策/叙事输出；world_id / turn_num 由调用方注入，模型不可见；
             state_diff 由 ToolRunner 从工具结果中抽出收集，供协调器合并提交/回档
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.core.log import get_logger
from src.llm import LLMResult

logger = get_logger(__name__)

# 工具执行函数签名：接收模型参数 + 注入参数，返回可 JSON 序列化的 dict
ToolFunc = Callable[..., Dict[str, Any]]

# 检索类工具：信息获取型调用，反复执行易陷入"永远查不够"的循环
_SEARCH_TOOLS = frozenset({"search_module", "query_memory"})

# 检索累计次数达到该阈值后，向消息流注入收敛提示，工程侧强制引导模型交卷
CONVERGE_HINT_AFTER = 4

_CONVERGE_HINT = (
    "你已多次检索。若信息已足够支撑本轮裁决，请立即调用 present_directive 交卷结束本轮；"
    "若仍不足，请基于已有信息做出当前最佳裁决，不要继续重复检索。"
)


# ====================================================================
# ToolRunner — 工具注册表与分发器
# ====================================================================


class ToolRunner:
    """工具注册表：name -> 执行函数。执行时注入运行时参数并收集 state_diff。

    execute 返回给模型可见的结果；工具返回中若含 state_diff 键则抽出
    存入 collected_diffs（不回填模型），供协调器合并提交。
    """

    def __init__(self) -> None:
        self._funcs: Dict[str, ToolFunc] = {}
        # 本轮已执行工具产出的 state_diff 列表（顺序与执行一致）
        self.collected_diffs: List[dict] = []

    def register(self, name: str, fn: ToolFunc) -> None:
        """注册工具：fn 接收 (模型参数..., **inject)，返回 dict。"""
        self._funcs[name] = fn

    def names(self) -> List[str]:
        """已注册工具名（排序），供日志与校验。"""
        return sorted(self._funcs)

    def reset_diffs(self) -> None:
        """清空本轮收集的 state_diff（新一轮决策前调用）。"""
        self.collected_diffs.clear()

    async def execute(self, name: str, arguments: dict, **inject) -> dict:
        """执行指定工具并返回可见结果；未知工具/执行异常返回错误镜像而非抛出。

        inject 为运行时注入参数（world_id / turn_num），与模型参数合并后传给 fn；
        模型填写的同名参数会被注入值覆盖，从根上防止越权填世界。
        """
        fn = self._funcs.get(name)
        if fn is None:
            return {"ok": False, "error": f"未知工具: {name}"}
        merged: dict = dict(arguments or {})
        merged.update(inject)
        try:
            result = fn(**merged)
            if inspect.isawaitable(result):  # 状态：异步工具 await
                result = await result
        except Exception as e:  # noqa: BLE001  单工具失败不阻断闭环，回填错误镜像让模型自纠
            logger.warning(f"工具执行失败 name={name}: {e}")
            return {"ok": False, "error": str(e)}
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        # 状态：抽出 state_diff，落库/回档数据不进模型上下文
        diff = result.pop("state_diff", None)
        if diff:
            self.collected_diffs.append(diff)
        return result


# ====================================================================
# 默认工具实现（挂接 4 个原子工具门面）
# ====================================================================


def build_default_runner(
    storage, memory: Optional[Any] = None, rng: Optional[object] = None
) -> ToolRunner:
    """构造注册了 4 个原子工具的 ToolRunner。

    storage: Storage 门面；memory: Memory 门面（query_memory 用，可为 None）；
    rng: 测试注入的确定骰序（透传给 check_and_update_stats）。
    工具均为纯计算不写库，state_diff 收集在 runner.collected_diffs。
    """
    from src.retrieval import search_module as _search_module
    from src.tools.check_and_update_stats import check_and_update_stats as _stats
    from src.tools.manage_tags import manage_tags as _tags

    runner = ToolRunner()

    def _run_search(**kwargs: Any) -> dict:
        """search_module：按 world_id 解析绑定模组，返回原文命中切片。"""
        hits = _search_module(
            kwargs.get("query") or "",
            world_id=kwargs["world_id"],
            top_k=int(kwargs.get("top_k") or 2),
        )
        return {
            "ok": True,
            "hits": [
                {
                    "title": h.section.title,
                    "source_location": h.section.source_location,
                    "content": h.section.content,
                    "score": h.score,
                }
                for h in hits
            ],
        }

    async def _run_query_memory(**kwargs: Any) -> dict:
        """query_memory：多变体语义召回，返回记忆命中文本。"""
        if memory is None:
            return {"ok": False, "error": "query_memory 未配置记忆后端"}
        hits = await memory.query_memory(kwargs.get("queries") or [], kwargs["world_id"])
        return {
            "ok": True,
            "hits": [
                {
                    "text": h.text,
                    "turn_num": h.turn_num,
                    "location": h.location,
                    "score": h.score,
                }
                for h in hits
            ],
        }

    def _run_stats(**kwargs: Any) -> dict:
        """check_and_update_stats：三段式检定/数值/背包，回填摘要，diff 由 runner 抽走。"""
        result = _stats(storage, kwargs, rng=rng)
        return {
            "ok": result["ok"],
            "summary": result["summary_for_agent"],
            "check": result["check"],
            "stats_changed": result["stats_changed"],
            "inventory_changed": result["inventory_changed"],
            "rule_hints": result["rule_hints"],
            "state_diff": result["state_diff"],
        }

    def _run_tags(**kwargs: Any) -> dict:
        """manage_tags：增删动态状态标签，回填摘要，diff 由 runner 抽走。"""
        result = _tags(storage, kwargs)
        return {
            "ok": result["ok"],
            "summary": result["summary_for_agent"],
            "tags_changed": result["tags_changed"],
            "state_diff": result["state_diff"],
        }

    runner.register("search_module", _run_search)
    runner.register("query_memory", _run_query_memory)
    runner.register("check_and_update_stats", _run_stats)
    runner.register("manage_tags", _run_tags)
    return runner


# ====================================================================
# Function Calling 闭环
# ====================================================================


@dataclass
class ToolLoopResult:
    """一次 Function Calling 闭环的产出。"""

    final: LLMResult                # 收敛轮（无 tool_calls）的最终结果
    tool_calls: List[dict] = field(default_factory=list)  # 全链路执行记录
    iterations: int = 0             # 实际请求轮数
    converged: bool = False         # 是否在轮数内收敛（False 表示触顶中止）
    stop_call: Optional[dict] = None  # 命中 stop_tool_name 的收尾调用（name+arguments）


def _build_assistant_tool_message(tool_calls: List[dict]) -> dict:
    """把归一化的 tool_calls 拼回 OpenAI 传输格式的 assistant 消息。"""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                },
            }
            for tc in tool_calls
        ],
    }


async def run_tool_loop(
    llm,
    tier: str,
    messages: List[dict],
    tools: List[dict],
    runner: ToolRunner,
    *,
    world_id: str,
    turn_num: int,
    max_iterations: int = 8,
    temperature: Optional[float] = None,
    stop_tool_name: Optional[str] = None,
    hint_after: int = CONVERGE_HINT_AFTER,
) -> ToolLoopResult:
    """执行 Function Calling 闭环，返回收敛后的最终 LLMResult 与工具调用记录。

    llm 为可调用对象（对齐 call_llm 签名，测试可注入 fake）：
    await llm(tier, messages, tools=tools, temperature=temperature)

    循环：传 tools 请求 → 模型返回 tool_calls 则执行并回填 → 再次请求，
    直到模型不再调用工具（收敛）或达到 max_iterations；收敛后 final.text
    即主 Agent 的决策/叙事输出。工具执行失败不中断，以错误镜像回填让模型自纠。

    stop_tool_name 非空时，模型一旦调用该工具即视为"交卷"，不执行该工具也不回填，
    收敛返回并把调用参数写入 result.stop_call（供调用方提取契约）。
    """
    if llm is None:
        from src import llm as llm_module

        llm = getattr(llm_module, "call_llm", None)
    if llm is None:
        raise RuntimeError("run_tool_loop 需要可用的 llm 可调用对象")

    current: List[dict] = list(messages)
    executed: List[dict] = []
    search_count = 0
    hinted = False
    for i in range(1, max_iterations + 1):
        # 状态：检索次数超阈值后注入收敛提示，工程侧强制引导模型收尾（不依赖模型自觉）
        if not hinted and search_count >= hint_after:
            current.append({"role": "system", "content": _CONVERGE_HINT})
            hinted = True
        result = await llm(tier, current, tools=tools, temperature=temperature)
        if not result.is_ok:
            return ToolLoopResult(
                final=result, tool_calls=executed, iterations=i, converged=False
            )
        if not result.tool_calls:
            # 状态：模型不再调用工具，收敛
            return ToolLoopResult(
                final=result, tool_calls=executed, iterations=i, converged=True
            )
        # 状态：命中收尾工具（如 present_directive）立即交卷收敛，
        # 不执行该工具也不回填，由调用方从 stop_call 提取交卷参数
        if stop_tool_name:
            stop_calls = [
                tc for tc in result.tool_calls if tc["name"] == stop_tool_name
            ]
            if stop_calls:
                stop = stop_calls[0]
                return ToolLoopResult(
                    final=result,
                    tool_calls=executed,
                    iterations=i,
                    converged=True,
                    stop_call={
                        "name": stop["name"],
                        "arguments": stop.get("arguments") or {},
                    },
                )
        # 状态：回填 assistant tool_calls 消息 + 各工具结果消息
        current.append(_build_assistant_tool_message(result.tool_calls))
        for tc in result.tool_calls:
            if tc["name"] in _SEARCH_TOOLS:  # 状态：累计检索次数用于触发收敛提示
                search_count += 1
            out = await runner.execute(
                tc["name"], tc["arguments"], world_id=world_id, turn_num=turn_num
            )
            current.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": json.dumps(out, ensure_ascii=False),
                }
            )
            executed.append(
                {"name": tc["name"], "arguments": tc["arguments"], "output": out}
            )
    # 状态：达到最大轮数仍未收敛，返回失败结果
    logger.warning(
        f"工具循环触顶未收敛 (tier={tier}, world={world_id}, 轮数={max_iterations})"
    )
    return ToolLoopResult(
        final=LLMResult(
            text=None, tier=tier, model_name="", messages=current,
            success=False,
            error=f"工具循环超过 {max_iterations} 轮未收敛",
        ),
        tool_calls=executed, iterations=max_iterations, converged=False,
    )
