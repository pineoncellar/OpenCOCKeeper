# -*- coding: utf-8 -*-
"""
@File     :   retrieval/__init__.py
@Desc     :   检索索引层对外入口：模组检索 search_module + 规则库检索 search_rule + 索引构建 + 数据模型
@Note     :   模组输入是 src.module 的原文（LLM 划界切片），规则库输入是 data/rules/*.md（标题切片），
             输出统一为 SectionHit 契约（原文片段而非答案）；search_rule 纯只读零 LLM
"""

from .index import ModuleIndex, build_index, build_index_async, clear_cache
from .models import Section, SectionHit
from .rules import (
    RuleIndex,
    build_rule_index,
    clear_rule_cache,
    search_rule,
    search_rule_async,
    split_markdown_sections,
)
from .search import search_module, search_module_async

__all__ = [
    "search_module",
    "search_module_async",
    "build_index",
    "build_index_async",
    "clear_cache",
    "ModuleIndex",
    "search_rule",
    "search_rule_async",
    "build_rule_index",
    "clear_rule_cache",
    "split_markdown_sections",
    "RuleIndex",
    "Section",
    "SectionHit",
]
