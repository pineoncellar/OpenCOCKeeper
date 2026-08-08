# -*- coding: utf-8 -*-
"""
@File     :   parsers/__init__.py
@Desc     :   模组解析器注册表：扩展名 -> 解析器（path -> 纯文本），新增格式在此登记
@Note     :   v1 仅 PDF 与 Word；解析器只出纯文本，结构划界由检索层 LLM 完成
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ...core.exceptions import UnsupportedFormatError
from .docx import read_docx
from .pdf import read_pdf

# 扩展名（小写） -> 解析器可调用对象；v1 仅支持 PDF 与 Word
PARSERS: dict[str, Callable[[Path], str]] = {
    ".pdf": read_pdf,
    ".docx": read_docx,
}

# 受支持扩展名白名单（loader.list_modules 收录依据）
SUPPORTED_EXTS = frozenset(PARSERS)

# 扩展名 -> 归一化格式名
_FORMAT_BY_EXT = {
    ".pdf": "pdf",
    ".docx": "docx",
}


def normalize_format(ext: str) -> str:
    """扩展名（小写）归一化为格式名，未知扩展名原样返回便于排查。"""
    return _FORMAT_BY_EXT.get(ext.lower(), ext.lower().lstrip("."))


def get_parser(ext: str) -> Callable[[Path], str]:
    """按扩展名取解析器；白名单外抛 UnsupportedFormatError。"""
    parser = PARSERS.get(ext.lower())
    if parser is None:
        raise UnsupportedFormatError(
            f"不支持的模组格式: {ext!r}，当前支持 {sorted(SUPPORTED_EXTS)}"
        )
    return parser
