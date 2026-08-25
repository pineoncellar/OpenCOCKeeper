# -*- coding: utf-8 -*-
"""
@File     :   client.py
@Desc     :   统一 OpenAI 兼容 API 的异步调用客户端：三级模型分级、超时重试、流式输出
@Note     :   配置经 src.core.config 读取：model_tiers.<tier> 取模型档位，
             providers.ini 对应节取 base_url / api_key（缺失即抛明确错误）；
             公共入口 call_llm / call_llm_stream / ask_llm（见 src.llm.__init__）
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import AsyncGenerator, Dict, Optional

import aiohttp

from src.core.config import ConfigError, get_settings
from src.core.log import get_logger

logger = get_logger(__name__)

# ── 默认参数 ──
DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 2
RETRY_DELAY_BASE = 1.0  # 退避基础秒数
# ask_llm 允许透传给 call_llm 的参数白名单，拼错名字静默忽略而非抛 TypeError
_LLM_KWARGS_ALLOWLIST = frozenset(
    {"timeout", "max_retries", "temperature", "max_tokens", "tools"}
)


# ====================================================================
# LLMResult — 统一 LLM 调用返回值（含追踪元数据）
# ====================================================================


@dataclass
class LLMResult:
    """LLM 调用的完整结果。

    包含响应文本与调用元数据（模型等级、实际模型名、请求消息、成功与否）。
    """
    text: str | None                 # 响应文本（失败为 None）
    tier: str                        # 模型等级: fast / standard / smart
    model_name: str                  # 实际模型名
    messages: list[dict]             # 请求消息列表（完整 prompt）
    reasoning_content: str | None = None  # 推理模式思考内容（DeepSeek 官方多轮必须原样回传，缺失即拒）
    success: bool = True             # 是否成功
    error: str | None = None         # 错误信息
    tool_calls: list[dict] | None = None  # Function Calling 返回的工具调用意图

    @property
    def is_ok(self) -> bool:
        # 有 tool_calls 但无正文是 Function Calling 的正常中间态，同样视为成功
        return self.success and (self.text is not None or self.tool_calls)

    def brief(self) -> dict:
        """轻量摘要，供日志 / 调试使用。"""
        return {
            "tier": self.tier,
            "model": self.model_name,
            "success": self.success,
            "error": self.error,
            "response_length": len(self.text) if self.text else 0,
        }


# ====================================================================
# 内部工具
# ====================================================================


def _build_api_url(base_url: str) -> str:
    """构建完整的 /chat/completions URL。

    处理多种 base_url 格式:
      - "https://api.openai.com/v1"           → "https://api.openai.com/v1/chat/completions"
      - "https://api.openai.com/v1/"           → "https://api.openai.com/v1/chat/completions"
      - "https://api.deepseek.com/chat/completions" → 不变
      - "https://api.siliconflow.cn/v1"        → "https://api.siliconflow.cn/v1/chat/completions"
    """
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _parse_tool_calls(raw: list | None) -> list[dict] | None:
    """把 API 返回的 tool_calls 归一化为 [{id, name, arguments: dict}]；无则返回 None。

    arguments 在传输中是 JSON 字符串，统一解析为 dict 供调度器直接使用；
    解析失败按空 dict 兜底（宁可让工具报参数缺失，也不让调度器崩）。
    """
    if not raw:
        return None
    parsed: list[dict] = []
    for tc in raw:
        fn = tc.get("function") or {}
        args_raw = fn.get("arguments") or "{}"
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except (ValueError, TypeError):
                args = {}  # 状态：JSON 解析失败降级空参数
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {}
        parsed.append(
            {
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": args,
            }
        )
    return parsed or None


def _get_tier_config(tier: str) -> tuple[Dict, Dict]:
    """获取指定层级的完整模型 + 提供商配置。

    返回 ``(model_config: dict, provider_config: dict)``。
    模型配置来自 config.yaml 的 ``model_tiers.<tier>``，
    提供方配置来自 providers.ini 的对应节（provider 字段，不区分大小写）。
    在此统一校验 api_key / base_url 齐备，缺任一抛 ValueError，
    让调用方拿到明确错误，而不是后续下标取值时莫名 KeyError。
    """
    settings = get_settings()
    model_config = settings.get_model_config(tier)  # 未知 tier 时抛 ConfigError
    provider_name = model_config.get("provider")
    provider_config = settings.get_provider(provider_name) if provider_name else None
    if provider_config is None:
        raise ValueError(
            f"未找到提供方 '{provider_name}' 的配置。"
            f"请检查 providers.ini 是否包含 [{str(provider_name).upper()}] 配置节"
        )
    if not provider_config.get("api_key"):
        raise ValueError(f"提供方 '{provider_name}' 未配置 api_key")
    if not provider_config.get("base_url"):
        raise ValueError(f"提供方 '{provider_name}' 未配置 base_url")
    return model_config, provider_config


# ====================================================================
# 公开接口
# ====================================================================


async def call_llm(
    tier: str,
    messages: list[dict],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = MAX_RETRIES,
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    world_id: str = "",
    turn_num: int = 0,
) -> LLMResult:
    """调用 LLM 并返回 LLMResult（响应文本 + 调用元数据）。

    参数:
        tier:        模型等级，必须是 config.yaml 中 model_tiers 的 key
                     （如 "fast" / "standard" / "smart"）
        messages:    消息列表 [{"role": "user", "content": "..."}]
        timeout:     单次请求超时秒数
        max_retries: 失败重试次数
        temperature: 覆盖配置中的 temperature（None=使用配置值）
        max_tokens:  覆盖配置中的 max_tokens（None=使用配置值）
        world_id:    世界 id（可选项，调用链元数据，trace 事件由上层发布）
        turn_num:    轮次号（可选项，随 world_id 一并传递的元数据）

    返回:
        LLMResult 对象（含 text + tier + model_name + messages + success + error）
    """
    try:
        model_config, provider_config = _get_tier_config(tier)
    except (ConfigError, ValueError) as e:
        logger.error(f"call_llm: 配置错误 (tier={tier}): {e}")
        return LLMResult(text=None, tier=tier, model_name="", messages=messages,
                         success=False, error=str(e))

    # 状态：请求/响应的结构化 trace 事件由上层（run_tool_loop / Narrator）发布，
    # 本层不写独立文本日志，避免与结构化 trace 重复
    api_url = _build_api_url(provider_config["base_url"])
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider_config['api_key']}",
    }
    body = {
        "model": model_config["model_name"],
        "messages": messages,
        "temperature": temperature if temperature is not None else model_config.get("temperature", 0.7),
        "max_tokens": max_tokens if max_tokens is not None else model_config.get("max_tokens", 1000),
        "stream": False,
    }
    if tools:  # 状态：提供工具清单时启用 Function Calling
        body["tools"] = tools

    last_error: str | None = None

    for attempt in range(max_retries + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url,
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        # 状态：5xx/429 可重试；其余 4xx 为鉴权/参数错误，重试无益白耗配额
                        retryable = resp.status >= 500 or resp.status == 429
                        logger.warning(
                            f"LLM API {resp.status} (tier={tier}, "
                            f"尝试 {attempt + 1}/{max_retries + 1}): "
                            f"{error_text[:200]}"
                        )
                        if retryable and attempt < max_retries:
                            await asyncio.sleep(RETRY_DELAY_BASE * (attempt + 1))
                            continue
                        return LLMResult(text=None, tier=tier,
                                         model_name=model_config["model_name"],
                                         messages=messages, success=False,
                                         error=f"HTTP {resp.status}")

                    data = await resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        logger.warning(f"LLM API 返回空 choices (tier={tier})")
                        return LLMResult(text=None, tier=tier,
                                         model_name=model_config["model_name"],
                                         messages=messages, success=False,
                                         error="空 choices")

                    message = choices[0].get("message", {})
                    content = message.get("content", "")
                    tool_calls = _parse_tool_calls(message.get("tool_calls"))
                    # 状态：推理模式（DeepSeek 官方等）响应携带 reasoning_content 思考字段，
                    # 必须随 LLMResult 透传，供 Function Calling 回填时原样带回复用；
                    # 否则多轮回传缺失该字段，DeepSeek 官方 API 直接拒绝请求
                    reasoning = message.get("reasoning_content") or None

                    return LLMResult(text=content, tier=tier,
                                     model_name=model_config["model_name"],
                                     messages=messages, success=True,
                                     tool_calls=tool_calls,
                                     reasoning_content=reasoning)

        except asyncio.TimeoutError:
            last_error = f"超时 ({timeout}s)"
            logger.warning(
                f"LLM 超时 (tier={tier}, "
                f"尝试 {attempt + 1}/{max_retries + 1})"
            )
            if attempt < max_retries:
                await asyncio.sleep(RETRY_DELAY_BASE * (attempt + 1))
                continue

        except aiohttp.ClientError as e:
            last_error = f"网络错误: {e}"
            logger.warning(
                f"LLM 网络错误 (tier={tier}, "
                f"尝试 {attempt + 1}/{max_retries + 1}): {e}"
            )
            if attempt < max_retries:
                await asyncio.sleep(RETRY_DELAY_BASE * (attempt + 1))
                continue

        except Exception as e:
            logger.error(f"LLM 未知错误 (tier={tier}): {e}")
            return LLMResult(text=None, tier=tier, model_name=model_config["model_name"],
                             messages=messages, success=False, error=str(e))

    logger.error(f"LLM 最终失败 (tier={tier}): {last_error}")
    return LLMResult(text=None, tier=tier, model_name=model_config["model_name"],
                     messages=messages, success=False, error=last_error)


async def call_llm_stream(
    tier: str,
    messages: list[dict],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = MAX_RETRIES,
    temperature: float | None = None,
    max_tokens: int | None = None,
    world_id: str = "",
    turn_num: int = 0,
) -> AsyncGenerator[str, None]:
    """调用 LLM 并以生成器方式流式返回文本片段。

    与 call_llm 一致带超时重试；重试只覆盖"尚未收到 200、流未开始产出"的
    阶段，一旦流已建立并产出内容，中途异常直接终止——重播会把已输出的
    文本片段重复拼接，破坏叙事连续性。
    超时按 sock_read（两次读取间隔）计时，长叙事不会被总时长上限掐断。
    world_id / turn_num 为调用链元数据，trace 事件由上层发布。

    用法::

        async for chunk in call_llm_stream("standard", messages):
            print(chunk, end="")
    """
    try:
        model_config, provider_config = _get_tier_config(tier)
    except (ConfigError, ValueError) as e:
        logger.error(f"call_llm_stream: 配置错误 (tier={tier}): {e}")
        return

    # 状态：请求/响应的结构化 trace 事件由上层发布，本层不写独立文本日志
    api_url = _build_api_url(provider_config["base_url"])
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider_config['api_key']}",
    }
    body = {
        "model": model_config["model_name"],
        "messages": messages,
        "temperature": temperature if temperature is not None else model_config.get("temperature", 0.7),
        "max_tokens": max_tokens if max_tokens is not None else model_config.get("max_tokens", 1000),
        "stream": True,
    }

    for attempt in range(max_retries + 1):
        started = False  # 状态：是否已收到 200 开始产出；一旦为真即禁止重试
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url,
                    headers=headers,
                    json=body,
                    # 流式长叙事可能远超 total 上限；改按"两次读取间隔"计时，
                    # 既能容忍慢速流，又能在对端长时间停顿时及时断开
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=timeout, sock_read=timeout),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        # 状态：5xx/429 可重试；4xx 鉴权/参数错误重试无益
                        retryable = resp.status >= 500 or resp.status == 429
                        logger.warning(
                            f"LLM 流式 API {resp.status} (tier={tier}, "
                            f"尝试 {attempt + 1}/{max_retries + 1}): "
                            f"{error_text[:200]}"
                        )
                        if retryable and attempt < max_retries:
                            await asyncio.sleep(RETRY_DELAY_BASE * (attempt + 1))
                            continue
                        return

                    started = True  # 状态：流已建立，此后异常直接终止，杜绝重播重复
                    async for raw_line in resp.content:  # 状态：流式读取，遇 [DONE] 结束
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line or line.startswith(":"):
                            continue
                        if line == "data: [DONE]":
                            break
                        if line.startswith("data: "):
                            try:
                                chunk = json.loads(line[6:])
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
                    return  # 状态：流正常读完即结束生成器，否则会落入下一轮重试把同段内容重复输出
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.warning(
                f"LLM 流式失败 (tier={tier}, 尝试 {attempt + 1}/{max_retries + 1}): {e}"
            )
            if not started and attempt < max_retries:  # 状态：仅流开始前允许重试
                await asyncio.sleep(RETRY_DELAY_BASE * (attempt + 1))
                continue
            return
        except Exception as e:
            logger.warning(f"LLM 流式调用失败 (tier={tier}): {e}")
            return
    logger.warning(f"LLM 流式最终失败 (tier={tier})")


# ====================================================================
# 便捷包装 — 单条消息快捷调用
# ====================================================================


async def ask_llm(
    tier: str,
    system_prompt: str,
    user_message: str,
    **kwargs,
) -> LLMResult:
    """便捷版：传入 system prompt 和 user message，返回 LLMResult。

    示例::

        result = await ask_llm("fast", "你是一个助手", "你好")
        if result.is_ok:
            print(result.text)
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    # 只透传白名单内参数，拼错名字静默忽略而不是抛 TypeError
    forwarded = {k: v for k, v in kwargs.items() if k in _LLM_KWARGS_ALLOWLIST}
    return await call_llm(tier, messages, **forwarded)
