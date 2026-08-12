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

from src.core.log import get_logger, get_llm_trace_logger
from src.llm import LLMResult

logger = get_logger(__name__)
# 独立 LLM 交互 trace logger：工具调用请求/结果随完整 prompt 一并落 llm-<date>.log
llm_trace = get_llm_trace_logger()


def _brief(value: Any, limit: int = 60) -> str:
    """把任意值折叠成单行短摘要（去空白 + 截断），避免长参数/叙事刷屏日志。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _brief_args(arguments: Optional[dict], limit: int = 60) -> str:
    """工具参数摘要：逐键截断合并；空参数返回占位。"""
    if not arguments:
        return "(无参数)"
    return " ".join(f"{k}={_brief(v, limit)}" for k, v in arguments.items())


# 工具执行函数签名：接收模型参数 + 注入参数，返回可 JSON 序列化的 dict
ToolFunc = Callable[..., Dict[str, Any]]

# 检索类工具：信息获取型调用，反复执行易陷入"永远查不够"的循环
_SEARCH_TOOLS = frozenset({"search_module", "query_memory", "search_rule"})

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
        # 本轮已执行检定的程序权威结果（掷骰值/成功等级等），供 Narrator 演播
        self.collected_checks: List[dict] = []

    def register(self, name: str, fn: ToolFunc) -> None:
        """注册工具：fn 接收 (模型参数..., **inject)，返回 dict。"""
        self._funcs[name] = fn

    def names(self) -> List[str]:
        """已注册工具名（排序），供日志与校验。"""
        return sorted(self._funcs)

    def reset_diffs(self) -> None:
        """清空本轮收集的 state_diff（新一轮决策前调用）。"""
        self.collected_diffs.clear()

    def reset_checks(self) -> None:
        """清空本轮收集的检定结果（新一轮决策前调用）。"""
        self.collected_checks.clear()

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
        # 状态：每调一个工具即打日志（工具名 + 参数摘要），便于 CLI/文件侧调试闭环
        logger.info("工具调用 name=%s %s", name, _brief_args(arguments))
        # 状态：完整参数落 llm trace 文件（调试提示词/Function Calling 用）
        llm_trace.debug(
            "工具调用 name=%s\nargs=%s",
            name, json.dumps(arguments, ensure_ascii=False, indent=2),
        )
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
        # 状态：额外保留检定结果权威副本（仍回填模型用于写手记，但 Narrator
        # 以本副本为准，杜绝模型改写/遗漏骰值与成功等级）
        check = result.get("check")
        if check:
            self.collected_checks.append(check)
        logger.debug(
            "工具返回 name=%s ok=%s diff=%s check=%s",
            name, result.get("ok"), bool(diff), bool(check),
        )
        llm_trace.debug(
            "工具返回 name=%s\nresult=%s",
            name, json.dumps(result, ensure_ascii=False, indent=2, default=str),
        )
        return result


# ====================================================================
# 默认工具实现（挂接原子工具门面）
# ====================================================================


async def _run_search_tool(**kwargs: Any) -> dict:
    """search_module 工具实现：按 world_id 解析绑定模组，返回原文命中切片。

    async 版本——运行中事件循环内首次划界走 search_module_async，
    避免 delineate_sync 的 asyncio.run 冲突；主 Agent 与开场 Agent 共用。
    """
    from src.retrieval import search_module_async as _search_module

    hits = await _search_module(
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


def _run_pc_background_tool(storage, **kwargs: Any) -> dict:
    """get_pc_background 工具实现：按需查 PC 入模组前背景（只读，无 state_diff）。

    storage 为存储门面（唯一外部依赖），主 Agent 与开场 Agent 共用。
    """
    from src.tools.get_pc_background import get_pc_background as _bg

    result = _bg(storage, kwargs)
    return {
        "ok": result["ok"],
        "backgrounds": result["backgrounds"],
        "summary": result["summary_for_agent"],
    }


async def _run_search_rule_tool(**kwargs: Any) -> dict:
    """search_rule 工具实现：固定检索 data/rules 规则库，返回未加工的规则原文切片。

    纯只读工具——不返回 state_diff、不写 SQLite、不依赖 world_id，
    仅把检索到的规则段落打包为 tool_response 送回主 Agent 循环。
    """
    from src.retrieval import search_rule_async as _search_rule

    hits = await _search_rule(
        kwargs.get("query") or "",
        top_k=int(kwargs.get("top_k") or 3),
    )
    return {
        "ok": True,
        "hits": [
            {
                "source": h.section.source_location,
                "title": h.section.title,
                "content": h.section.content,
                "score": h.score,
            }
            for h in hits
        ],
    }


def build_default_runner(
    storage, memory: Optional[Any] = None, rng: Optional[object] = None
) -> ToolRunner:
    """构造注册了 6 个原子工具的 ToolRunner。

    storage: Storage 门面；memory: Memory 门面（query_memory 用，可为 None）；
    rng: 测试注入的确定骰序（透传给 check_and_update_stats）。
    除 search_module/query_memory/search_rule 三个检索工具外均为纯计算不写库，
    state_diff 收集在 runner.collected_diffs；search_rule 为纯只读零副作用。
    """
    from src.tools.check_and_update_stats import check_and_update_stats as _stats
    from src.tools.manage_tags import manage_tags as _tags

    runner = ToolRunner()

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
        check = result["check"]
        if check is not None:
            # 状态：检定结果补上实体标识，Narrator 可区分"谁的检定"
            check = {**check, "entity_id": kwargs.get("entity_id")}
        return {
            "ok": result["ok"],
            "summary": result["summary_for_agent"],
            "check": check,
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

    runner.register("search_module", _run_search_tool)
    runner.register("query_memory", _run_query_memory)
    runner.register("check_and_update_stats", _run_stats)
    runner.register("manage_tags", _run_tags)
    runner.register("get_pc_background", lambda **kw: _run_pc_background_tool(storage, **kw))
    runner.register("search_rule", _run_search_rule_tool)
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


def _append_converge_hint(messages: List[dict]) -> List[dict]:
    """把收敛提示并入消息流首条 system 的末尾；无 system 时降级追加一条 system。

    实测教训：在 tool 结果之后新增独立的中间 system 消息，会让部分模型把它误读为
    对话末尾待续写内容，直接复读提示开头几个字（如'信息'/'若'）就收敛，产生残缺大纲。
    因此提示必须并进首条 system，保持在标准位置。
    """
    merged = list(messages)
    for i, m in enumerate(merged):
        if m.get("role") == "system":
            base = str(m.get("content") or "")
            content = f"{base}\n\n{_CONVERGE_HINT}" if base else _CONVERGE_HINT
            merged[i] = {**m, "content": content}
            return merged
    merged.append({"role": "system", "content": _CONVERGE_HINT})
    return merged


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
        # 状态：检索次数超阈值后把收敛提示并入首条 system，强制引导模型收尾
        if not hinted and search_count >= hint_after:
            current = _append_converge_hint(current)
            hinted = True
        logger.debug("工具闭环 LLM 请求 第 %d/%d 轮 tier=%s", i, max_iterations, tier)
        result = await llm(tier, current, tools=tools, temperature=temperature)
        if not result.is_ok:
            return ToolLoopResult(
                final=result, tool_calls=executed, iterations=i, converged=False
            )
        if not result.tool_calls:
            # 状态：模型不再调用工具，收敛
            logger.info(
                "工具闭环收敛 world=%s turn=%s 轮数=%d 工具调用=%d",
                world_id, turn_num, i, len(executed),
            )
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
                logger.info(
                    "工具闭环收尾工具命中 world=%s turn=%s name=%s 轮数=%d 工具调用=%d",
                    world_id, turn_num, stop["name"], i, len(executed),
                )
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
        logger.info(
            "工具闭环 第 %d/%d 轮返回 %d 个工具调用 world=%s turn=%s",
            i, max_iterations, len(result.tool_calls), world_id, turn_num,
        )
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
