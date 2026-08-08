# -*- coding: utf-8 -*-
"""
@File     :   bm25.py
@Desc     :   纯 Python BM25+ 全文打分（零依赖），用于章节正文的全文检索
@Note     :   模组规模小（几千到几万字），内存即时打分足够，不引入数据库全文索引；
             BM25+ 的 delta 项缓解长文档惩罚，避免大章节被系统性压低
"""

from __future__ import annotations

import math
from collections import Counter
from typing import List, Sequence


class BM25:
    """BM25+ 打分器；build 后对给定查询 token 逐篇打分。

    build(docs)：docs 为"每篇一个 token 列表"的二维结构；
    score(query_tokens, doc_idx)：返回该篇对查询的得分（非负）。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75, delta: float = 1.0) -> None:
        self.k1 = k1
        self.b = b
        self.delta = delta
        self._docs: List[List[str]] = []
        self._doc_lens: List[int] = []
        self._avgdl: float = 0.0
        self._df: Counter = Counter()
        self._n: int = 0

    def build(self, docs: Sequence[List[str]]) -> None:
        """依据文档集构建倒排统计：文档频率、平均长度。"""
        self._docs = [list(d) for d in docs]
        self._n = len(self._docs)
        self._doc_lens = [len(d) for d in self._docs]
        self._avgdl = sum(self._doc_lens) / max(1, self._n)
        df: Counter = Counter()
        for d in self._docs:
            for w in set(d):
                df[w] += 1
        self._df = df

    def score(self, query_tokens: Sequence[str], doc_idx: int) -> float:
        """单篇得分：对查询中命中的词累加 BM25+ 贡献。"""
        if not (0 <= doc_idx < self._n) or not self._docs:
            return 0.0
        dl = self._doc_lens[doc_idx]
        if dl <= 0:
            return 0.0
        freq = Counter(self._docs[doc_idx])
        total = 0.0
        for q in set(query_tokens):
            tf = freq.get(q)
            if not tf:
                continue
            idf = math.log(1 + (self._n - self._df[q] + 0.5) / (self._df[q] + 0.5))
            tf_norm = (tf * (self.k1 + 1)) / (
                tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
            )
            total += idf * (tf_norm + self.delta)
        return total
