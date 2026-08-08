# -*- coding: utf-8 -*-
"""
@File     :   retrieval/__init__.py
@Desc     :   模组检索索引层对外入口：search_module 检索门面 + 索引构建 + 数据模型
@Note     :   输入是 src.module 的原文，输出是 SectionHit 契约（原文片段而非答案）；
             主 Agent 的 search_module 原子工具未来直接挂本门面
"""

from .index import ModuleIndex, build_index, clear_cache
from .models import Section, SectionHit
from .search import search_module

__all__ = [
    "search_module",
    "build_index",
    "clear_cache",
    "ModuleIndex",
    "Section",
    "SectionHit",
]
