# -*- coding: utf-8 -*-
"""
@File     :   sections.py
@Desc     :   锚点切片：按 LLM 划界给出的标题与起始锚点，在原文上逐字切片成平铺 Section
@Note     :   content 一律取自原文（find 定位后切片），绝不用 LLM 复述；
             锚点找不到的章节并入前一段；全部找不到则整篇一段兜底
"""

from __future__ import annotations

from typing import List, Sequence

from .models import Section

# 无任何锚点命中时整篇一段的标题
_FALLBACK_TITLE = "全文"


def split_sections(text: str, metas: Sequence[dict]) -> List[Section]:
    """按划界元数据在原文上切片，返回平铺 Section 列表。

    metas 形如 [{"title": "...", "start_anchor": "..."}]，来自 LLM 划界器；
    对每个锚点用 text.find 定位起始字节，相邻两锚点之间的原文即上一节 content。
    首个锚点之前的正文归入引言；锚点找不到的章节被丢弃（其内容并入前一段）。
    """
    located: List[tuple[int, str]] = []
    for m in metas:
        anchor = str(m.get("start_anchor") or "").strip()
        if not anchor:
            continue
        idx = text.find(anchor)
        if idx >= 0:
            located.append((idx, str(m.get("title") or anchor)))

    if not located:
        # 无任何锚点命中：整篇一段兜底  # 状态：兜底
        return [Section("sec_0001", _FALLBACK_TITLE, _FALLBACK_TITLE, text)]

    located.sort(key=lambda x: x[0])
    # 同一位置只保留首个标题（去重，避免一个锚点切出多节）
    dedup: List[tuple[int, str]] = []
    seen_pos = set()
    for pos, title in located:
        if pos not in seen_pos:
            seen_pos.add(pos)
            dedup.append((pos, title))

    sections: List[Section] = []
    first_pos = dedup[0][0]
    if first_pos > 0:
        sections.append(Section("sec_0000", "引言", "引言", text[:first_pos]))
    for k, (pos, title) in enumerate(dedup):
        end = dedup[k + 1][0] if k + 1 < len(dedup) else len(text)
        sections.append(Section(f"sec_{k + 1:04d}", title, title, text[pos:end]))
    return sections
