# -*- coding: utf-8 -*-
"""
@File     :   structure.py
@Desc     :   LLM 划界器：导入期单次调用 LLM 识别章节标题与起始锚点（不改动正文任何字）
@Note     :   红线——LLM 只输出 title + start_anchor，正文由代码按锚点在原文切片；
             长文本分块调用后合并去重；任何失败一律降级为空列表（调用方整篇一段兜底）
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Dict, List

from ..core.config import get_settings
from ..core.prompts import get_prompt

# 划界 System 指令（要求 LLM 只输出 JSON）；正文外置 prompts.yaml（retrieval.structure_system）
_SYSTEM_PROMPT = get_prompt("retrieval.structure_system")

# 兼容 LLM 偶尔用 ```json 围栏包裹输出
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _settings() -> Dict:
    """读取划界相关配置（缺省启用、fast 档、8000 字分块）。"""
    s = get_settings()
    return {
        "enabled": bool(s.get("module.structure.enabled", True)),
        "tier": str(s.get("module.structure.llm_tier", "fast")),
        "chunk_chars": int(s.get("module.structure.chunk_chars", 8000)),
    }


def is_enabled() -> bool:
    """是否启用 LLM 划界；禁用时走"整篇一段"兜底（不调 LLM）。"""
    return _settings()["enabled"]


# ============================================
# 异步核心（测试/未来 async 链路直用）
# ============================================


async def delineate(text: str) -> List[dict]:
    """对纯文本执行 LLM 划界，返回 [{"title","start_anchor"}]。

    分块调用后合并去重；任何失败/空结果均返回空列表，由调用方整篇一段兜底。
    """
    cfg = _settings()
    if not cfg["enabled"] or not text.strip():
        return []
    metas: List[dict] = []
    for chunk in _chunk_text(text, cfg["chunk_chars"]):
        metas.extend(await _delineate_chunk(chunk, cfg["tier"]))
    return _merge_metas(metas)


def delineate_sync(text: str) -> List[dict]:
    """同步包装：在无事件循环的上下文（脚本/导入/测试）下执行划界。"""
    return asyncio.run(delineate(text))


# ============================================
# 分块 / 解析 / 合并
# ============================================


def _chunk_text(text: str, chunk_chars: int) -> List[str]:
    """按字符数切块，块间留 10% 重叠（不超过块长一半）防止标题恰好落在切缝上。"""
    if len(text) <= chunk_chars:
        return [text]
    overlap = min(max(200, int(chunk_chars * 0.1)), chunk_chars // 2)
    step = chunk_chars - overlap
    return [text[i:i + chunk_chars] for i in range(0, len(text), step)]


async def _delineate_chunk(chunk: str, tier: str) -> List[dict]:
    """单块划界：调用 LLM 并解析 JSON；调用异常或解析失败返回空。"""
    from src import llm as llm_module  # 动态取命名空间，测试可 monkeypatch
    # 状态：system 与 user 模板均动态读配置（热重载生效）
    prompt = get_prompt("retrieval.structure_user", chunk=chunk)
    try:
        result = await llm_module.call_llm(
            tier,
            [
                {"role": "system", "content": get_prompt("retrieval.structure_system")},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception:  # noqa: BLE001  调用异常不阻断导入，降级空结果
        return []
    if not result.success or not result.text:
        return []
    return _parse_metas(result.text)


def _parse_metas(raw: str) -> List[dict]:
    """解析 LLM 输出的 JSON；剥围栏、容错、字段校验，失败返回空。"""
    text = raw.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    sections = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(sections, list):
        return []
    metas: List[dict] = []
    for item in sections:
        if isinstance(item, dict) and item.get("title") and item.get("start_anchor"):
            metas.append(
                {
                    "title": str(item["title"]).strip(),
                    "start_anchor": str(item["start_anchor"]).strip(),
                }
            )
    return metas


def _merge_metas(metas: List[dict]) -> List[dict]:
    """合并多块结果：按 start_anchor 去重，保留首个。"""
    seen = set()
    out: List[dict] = []
    for m in metas:
        key = m.get("start_anchor")
        if key and key not in seen:
            seen.add(key)
            out.append(m)
    return out
