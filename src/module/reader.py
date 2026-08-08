# -*- coding: utf-8 -*-
"""
@File     :   reader.py
@Desc     :   模组原文读取入口：按文件名读回原始文本，按扩展名分派解析器
@Note     :   只读文件不做任何切分/索引/理解——"原文优先"原则在此贯彻；
             检索索引层（src/retrieval）后续直接消费本函数返回的原文
"""

from __future__ import annotations

from .loader import resolve
from .parsers import get_parser


def read_module(module_name: str) -> str:
    """按文件名读回模组原文文本。

    先经 resolve 完成安全解析（存在性 + 防穿越 + 白名单），
    再按扩展名分派对应解析器。文件缺失/格式不支持抛对应异常，不降级不静默。
    """
    path = resolve(module_name)
    parser = get_parser(path.suffix.lower())
    return parser(path)
