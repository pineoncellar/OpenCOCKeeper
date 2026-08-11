# -*- coding: utf-8 -*-
"""
@File     :   rules.py
@Desc     :   规则库检索门面：跨 data/rules/*.md 按 Markdown 标题切片建 BM25 索引，search_rule 返回 Top-K 规则原文
@Note     :   纯只读、零 LLM——规则库 Markdown 标题自带结构，直接按 #/## 切片，不引入模组式 LLM 划界器；
             复用 search._score_sections 打分（标题命中加权 + BM25 全文），索引结构对齐 ModuleIndex；
             RULES_DIR 为模块级变量，测试可 monkeypatch 指向临时目录整体替换
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from ..core.config import PROJECT_ROOT
from .bm25 import BM25
from .models import Section, SectionHit
from .search import _score_sections
from .tokenizer import add_terms, norm, tokenize

# 规则库固定检索路径（data/rules/*.md），相对项目根解析  # 状态：路径固定
RULES_DIR: Path = PROJECT_ROOT / "data" / "rules"

# 匹配 Markdown 标题行：1~6 级 #，后接标题文本  # 状态：切片锚点
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# 无任何标题时整篇一段的标题
_FALLBACK_TITLE = "全文"


@dataclass
class RuleIndex:
    """规则库检索索引：跨全部规则文件的平铺章节 + BM25。

    字段名与 ModuleIndex 对齐（sections/bm25/doc_tokens/title_norms），
    使 search._score_sections 可直接复用而无需适配。
    """

    sections: List[Section]
    bm25: BM25
    doc_tokens: List[List[str]] = field(default_factory=list)
    title_norms: List[str] = field(default_factory=list)


# 内存缓存：缓存键 -> RuleIndex；缓存键含目录路径与各文件 (名, 大小, mtime) 指纹
_RULE_CACHE: dict[Tuple, RuleIndex] = {}


### 切片与索引构建 ###


def split_markdown_sections(text: str, source: str) -> List[Section]:
    """按 Markdown 标题行把单文件正文切成平铺 Section（含标题行本身，物理切片）。

    source 为物理定位（规则文件名，如 战斗.md）；title 取标题文本；
    标题间正文整段保留，首个标题前的内容并入引言，无标题则整篇一段兜底。
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [Section("sec_0001", _FALLBACK_TITLE, source, text.strip())]
    sections: List[Section] = []
    if matches[0].start() > 0:
        sections.append(Section("sec_0000", "引言", source, text[: matches[0].start()].strip()))
    for k, m in enumerate(matches):
        title = m.group(2).strip()
        start = m.start()
        end = matches[k + 1].start() if k + 1 < len(matches) else len(text)
        sections.append(
            Section(
                f"sec_{k + 1:04d}", title, source, text[start:end].strip(),
            )
        )
    return sections


def _scan_rule_files() -> List[Path]:
    """扫描规则库目录下全部 .md 文件（按名排序），目录不存在返回空列表。"""
    d = RULES_DIR
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.md") if p.is_file() and not p.name.startswith("."))


def _cache_key(files: List[Path]) -> Tuple:
    """构建缓存键：目录路径 + 各文件 (名, 大小, mtime) 指纹，源变化即失效。"""
    fp = tuple(
        (p.name, p.stat().st_size, round(p.stat().st_mtime, 6)) for p in files
    )
    return (str(RULES_DIR), fp)


def build_rule_index() -> RuleIndex:
    """构建/取回规则库索引：跨全部文件切片合并 + BM25 建索引，按指纹缓存。"""
    files = _scan_rule_files()
    key = _cache_key(files)
    cached = _RULE_CACHE.get(key)
    if cached is not None:
        return cached
    sections: List[Section] = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        sections.extend(split_markdown_sections(text, p.name))
    doc_tokens = [tokenize(s.content) for s in sections]
    bm = BM25()
    bm.build(doc_tokens)
    title_norms = [norm(s.title) for s in sections]
    # 标题词喂进分词词典，后续检索不把规则术语切碎  # 状态：词典增强
    add_terms(s.title for s in sections if s.title)
    idx = RuleIndex(sections=sections, bm25=bm, doc_tokens=doc_tokens, title_norms=title_norms)
    _RULE_CACHE[key] = idx
    return idx


def clear_rule_cache() -> None:
    """清空规则库索引缓存（测试用 / 强制重建）。"""
    _RULE_CACHE.clear()


### 检索门面 ###


def search_rule(query: str, top_k: int = 3) -> List[SectionHit]:
    """按自然语言 query 检索规则库原文，返回按相关度排序的命中段落（默认最匹配 3 段）。

    同步门面——供脚本/测试使用；运行中的事件循环内（主 Agent 工具）无 asyncio.run 冲突，
    直接调用即可（规则库构建零 LLM），search_rule_async 仅为 API 对称保留。
    只定位原文不加工答案，跨章节规则关联交由主 Agent 读到原文后自行二次检索。
    """
    idx = build_rule_index()
    if not idx.sections:
        return []
    q_tokens = tokenize(query)
    q_norm = norm(query)
    return _score_sections(idx, q_tokens, q_norm, top_k)


async def search_rule_async(query: str, top_k: int = 3) -> List[SectionHit]:
    """异步检索门面（API 对称）：规则库构建零异步 IO，直接转同步实现。"""
    return search_rule(query, top_k=top_k)
