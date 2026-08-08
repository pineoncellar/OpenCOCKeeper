# -*- coding: utf-8 -*-
"""
@File     :   models.py
@Desc     :   检索层数据模型：Section（平铺章节）与 SectionHit（检索命中契约）
@Note     :   content 一律为未加工的原文切片（物理真相）；不承载树/引用等复杂结构
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Section:
    """模组中的一个可检索区域（章节单元，平铺无层级）。

    source_location 为物理定位：LLM 划界无页码信息时直接用章节标题；
    content 一律为代码按锚点从原文逐字切出的未加工正文。
    """

    section_id: str        # 章节唯一标识（sec_0001）
    title: str             # 章节/场景标题（LLM 划界识别）
    source_location: str   # 物理定位（当前取章节标题）
    content: str           # 未加工原始正文（100% 原文切片）


@dataclass
class SectionHit:
    """search_module 的单条命中：章节原文 + 检索元信息。"""

    section: Section
    score: float          # BM25 综合得分（含标题命中加权）
    title_match: bool     # 是否标题层命中（结构优先）
