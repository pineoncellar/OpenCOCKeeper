# -*- coding: utf-8 -*-
"""
@File     :   pdf.py
@Desc     :   PDF 模组解析器：提取整篇纯文本（供 LLM 划界与原文切片）
@Note     :   惰性导入 pypdf，未装依赖时抛 UnsupportedFormatError 并提示安装；
             扫描版 PDF 的 extract_text 可能为空，此时划界退化为整篇一段
"""

from __future__ import annotations

from pathlib import Path

from ...core.exceptions import UnsupportedFormatError

# 惰性加载 pypdf；None 未初始化，False 不可用  # 状态：依赖探测
_pypdf = None


def _get_pypdf():
    global _pypdf
    if _pypdf is None:
        try:
            import pypdf
            _pypdf = pypdf
        except ImportError:
            _pypdf = False
    if not _pypdf:
        raise UnsupportedFormatError(
            "解析 PDF 需要 pypdf，请先安装：uv add --optional retrieval pypdf"
        )
    return _pypdf


def read_pdf(path: Path) -> str:
    """整本 PDF 读为纯文本：逐页提取，页间以空行分隔。"""
    pdf = _get_pypdf()
    reader = pdf.PdfReader(str(path))
    parts = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001  个别页解析失败不拖垮整本，按空页继续
            text = ""
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)
