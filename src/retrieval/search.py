# -*- coding: utf-8 -*-
"""
@File     :   search.py
@Desc     :   search_module 门面：两阶段检索（标题结构匹配 + BM25 全文），返回最匹配原文
@Note     :   只定位原文不加工答案——跨章节/附录查阅完全交给主 Agent 自行二次检索；
             标题命中大幅加权实现"结构优先"，top_k 默认 2 省 token
"""

from __future__ import annotations

from typing import List, Optional

from ..core.db import get_db
from .index import build_index
from .models import SectionHit
from .tokenizer import norm, tokenize

# 标题命中加权分：远高于 BM25 常规得分，保证结构匹配优先  # 状态：结构优先
_TITLE_BONUS = 10.0


def search_module(
    query: str,
    module_name: Optional[str] = None,
    *,
    world_id: Optional[str] = None,
    top_k: int = 2,
) -> List[SectionHit]:
    """按查询检索模组原文，返回按相关度排序的命中章节（默认最匹配 2 段）。

    module_name 与 world_id 二选一：给了 world_id 则读取其绑定模组，
    两者皆缺省返回空列表。跨章节关联由主 Agent 读到原文后自行二次检索。
    """
    name = _resolve_module_name(module_name, world_id)
    if not name:
        return []
    idx = build_index(name)
    q_tokens = tokenize(query)
    q_norm = norm(query)

    scored: List[tuple[float, int, bool]] = []
    for i, sec in enumerate(idx.sections):
        title_match = bool(q_norm) and q_norm in idx.title_norms[i]
        score = idx.bm25.score(q_tokens, i)
        if title_match:
            score += _TITLE_BONUS
        if score > 0 or title_match:
            scored.append((score, i, title_match))

    # 总分降序，同分时标题命中优先  # 状态：结果排序
    scored.sort(key=lambda x: (-x[0], x[2]))
    return [
        SectionHit(idx.sections[i], round(score, 4), tm)
        for score, i, tm in scored[:top_k]
    ]


def _resolve_module_name(
    module_name: Optional[str], world_id: Optional[str]
) -> str:
    """解析检索目标模组：显式指定优先，其次取 world 绑定；都无返回空串。"""
    if module_name:
        return module_name
    if world_id:
        from ..storage.storage import Storage
        world = Storage(db=get_db()).get_world(world_id)
        if world:
            return world.get("module_name") or ""
    return ""
