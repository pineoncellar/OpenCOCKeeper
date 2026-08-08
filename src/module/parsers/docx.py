# -*- coding: utf-8 -*-
"""
@File     :   docx.py
@Desc     :   Word 模组解析器：提取整篇纯文本（供 LLM 划界与原文切片）
@Note     :   惰性导入 python-docx，未装依赖时抛 UnsupportedFormatError 并提示安装；
             段落间以空行分隔，不关心样式——划界交给 LLM，代码不再猜标题
"""

from __future__ import annotations

from pathlib import Path

from ...core.exceptions import UnsupportedFormatError

# 惰性加载 python-docx；None 未初始化，False 不可用  # 状态：依赖探测
_docx = None


def _get_docx():
    global _docx
    if _docx is None:
        try:
            import docx
            _docx = docx
        except ImportError:
            _docx = False
    if not _docx:
        raise UnsupportedFormatError(
            "解析 Word 需要 python-docx，请先安装：uv add --optional retrieval python-docx"
        )
    return _docx


def read_docx(path: Path) -> str:
    """整篇 Word 读为纯文本：段落间以空行分隔（不区分标题/正文样式）。"""
    docx = _get_docx()
    doc = docx.Document(str(path))
    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(parts)
