# -*- coding: utf-8 -*-
"""
@File     :   tokenizer.py
@Desc     :   中文分词：优先 jieba（含词典补充防专有名词切碎），缺失时按字符级兜底
@Note     :   jieba 为可选依赖，未装时降级为"中文单字 + 英文单词"切分——
             专有名词（如"狄拉斯-琳"）会被切碎，质量下降但可运行；
             add_terms 用于把章节标题等候选词喂进词典，是检索质量的放大器
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

# 惰性探测 jieba：None 未初始化，False 不可用  # 状态：依赖探测
_jieba = None

# 无 jieba 的兜底切分：英文按单词、中文按单字（务实降级，专有名词会被切碎）
_FALLBACK_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def _get_jieba():
    global _jieba
    if _jieba is None:
        try:
            import jieba
            _jieba = jieba
        except ImportError:
            _jieba = False
    return _jieba or None


def tokenize(text: str) -> List[str]:
    """把文本切成 token 列表；空白 token 一律剔除。"""
    jb = _get_jieba()
    if jb is not None:
        return [t for t in jb.lcut(text) if t.strip()]
    return _FALLBACK_RE.findall(text.lower())


def add_terms(terms: Iterable[str]) -> None:
    """把候选专有名词喂进 jieba 词典，防止被切碎；无 jieba 时静默忽略。"""
    jb = _get_jieba()
    if jb is None:
        return
    for t in terms:
        t = str(t).strip()
        if t:
            jb.add_word(t)


def norm(text: str) -> str:
    """检索用规范化：去全部空白并小写，用于标题子串匹配。"""
    return "".join(text.split()).lower()
