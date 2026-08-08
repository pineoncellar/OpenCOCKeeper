# -*- coding: utf-8 -*-
"""
@File     :   index.py
@Desc     :   检索索引构建：原文 -> LLM 划界(或 JSON 缓存) -> 锚点切片 -> BM25，按 mtime 缓存
@Note     :   模块级内存索引 + 划界产物 JSON 缓存（data/modules/.cache/<name>.json）；
             源文件 mtime 变化即重新划界（替换文件 = 替换模组）；
             LLM 划界失败降级为整篇一段，正文始终来自原文切片
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..module import loader as module_loader
from ..module.loader import resolve
from ..module.parsers import normalize_format
from ..module.reader import read_module
from .bm25 import BM25
from .models import Section
from .sections import split_sections
from .tokenizer import add_terms, norm, tokenize


@dataclass
class ModuleIndex:
    """一个模组的完整检索索引（内存态，随文件 mtime 自动重建）。"""

    module_name: str
    fmt: str                  # 归一化格式名（pdf / docx）
    path: Path                # 模组文件绝对路径
    sections: List[Section]   # 全部平铺章节
    bm25: BM25                # 章节正文全文打分器
    doc_tokens: List[List[str]]  # 每节分词结果（与 sections 对齐）
    title_norms: List[str]    # 每节标题的规范化文本，供结构匹配子串命中


# 内存缓存：module_name -> (文件 mtime, ModuleIndex)；mtime 变化即重建  # 状态：缓存失效
_CACHE: Dict[str, Tuple[float, ModuleIndex]] = {}


def build_index(module_name: str) -> ModuleIndex:
    """构建或取回指定模组的检索索引；文件被替换（mtime 变）时自动重建。

    流程：读原文 -> 取划界元数据（JSON 缓存优先，否则 LLM 划界并写缓存）
          -> 锚点切片 -> 分词 -> BM25。
    """
    path = resolve(module_name)
    mtime = path.stat().st_mtime
    cached = _CACHE.get(module_name)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    text = read_module(module_name)
    metas = _load_or_delineate(module_name, path, mtime, text)
    sections = split_sections(text, metas)
    doc_tokens = [tokenize(s.content) for s in sections]
    bm = BM25()
    bm.build(doc_tokens)
    title_norms = [norm(s.title) for s in sections]
    # 标题词喂进分词词典，后续检索不再把专有名词切碎  # 状态：词典增强
    add_terms(s.title for s in sections if s.title)
    idx = ModuleIndex(
        module_name=module_name,
        fmt=normalize_format(path.suffix.lower()),
        path=path,
        sections=sections,
        bm25=bm,
        doc_tokens=doc_tokens,
        title_norms=title_norms,
    )
    _CACHE[module_name] = (mtime, idx)
    return idx


def clear_cache(module_name: Optional[str] = None) -> None:
    """清空内存索引缓存（测试用 / 强制重建）；module_name 缺省时全量清空。"""
    if module_name is None:
        _CACHE.clear()
    else:
        _CACHE.pop(module_name, None)


# ============================================
# LLM 划界产物缓存（JSON，随源 mtime 失效）
# ============================================


def _cache_dir() -> Path:
    """划界缓存目录：data/modules/.cache（跟随 loader.MODULES_DIR，测试可整体替换）。"""
    return module_loader.MODULES_DIR / ".cache"


def _cache_path(module_name: str) -> Path:
    return _cache_dir() / f"{module_name}.json"


def _load_or_delineate(
    module_name: str, path: Path, mtime: float, text: str
) -> List[dict]:
    """取划界元数据：JSON 缓存命中且源 mtime 一致则复用，否则 LLM 划界并写缓存。

    划界禁用时不读写缓存（每次走整篇一段兜底，开销极小）；
    LLM 失败（返回空）也写入缓存，避免每次重建都重复调用失败。
    """
    from . import structure
    if not structure.is_enabled():
        return []
    cp = _cache_path(module_name)
    if cp.is_file():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            if data.get("source_mtime") == mtime:
                return data.get("sections") or []
        except (ValueError, OSError):
            pass  # 缓存损坏则忽略，重新划界  # 状态：缓存容错
    metas = structure.delineate_sync(text)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(
        json.dumps({"source_mtime": mtime, "sections": metas}, ensure_ascii=False),
        encoding="utf-8",
    )
    return metas
