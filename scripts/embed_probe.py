# -*- coding: utf-8 -*-
"""
@File     :   embed_probe.py
@Desc     :   研究脚本（非正式链路）：验证"语义召回能否命中不含检索词但语义相关的记忆"
@Note     :   不写入任何项目表/真实世界；只做两件事：
              1) 用真实 bge-m3 embedding 端点算 query 与记忆文本的余弦相似度矩阵 + 排名；
              2) 可选：用独立临时 world_id 走真实 Mem0 端到端召回（会往 qdrant 写几条测试记忆）。
              运行: .\.venv\Scripts\python.exe scripts\embed_probe.py [--mem0] [--offline]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import urllib.request

from _common import fail, make_world_id, ok, run_async, section, step, warn


# ============================================
# 文本集合
# ============================================

QUERIES = [
    "颜料",
    "颜料的气味",
    "画画和油画相关的记忆",
    "谁平时喜欢画画或接触油画",
]

TARGETS = [
    ("T1-目标(无'颜料'二字)", "塞恩先生告诉调查员他平时喜欢画画，最近在学油画"),
    ("T2-对照(含'颜料'二字)", "伯纳黛特的手上沾着蓝色颜料，散发着松节油的气味"),
    ("T3-干扰-酒类", "费莉西蒂在酒窖里找到了几瓶陈年葡萄酒"),
    ("T4-干扰-战斗", "石皮兽从墙缝中钻出，向调查员们扑来"),
    ("T5-弱相关-画室", "马车夫失踪案的关键线索指向旧城区的画室"),
]


# ============================================
# 工具函数
# ============================================


def _cos(a: list, b: list) -> float:
    """两个向量余弦相似度。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


async def _embed(texts: list, settings) -> list:
    """向嵌入端点批量请求，返回向量列表（与 preflight 同路径）。"""
    rag = settings.get("memory.rag") or {}
    provider = str(rag.get("embedder_provider", ""))
    model = str(rag.get("embedder_model", ""))
    prov = settings.get_provider(provider)
    if not prov:
        raise RuntimeError(f"嵌入提供方 '{provider}' 未在 providers.ini 配置")
    url = prov.get("base_url", "").rstrip("/") + "/embeddings"
    body = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {prov.get('api_key', '')}",
        },
    )

    def _do() -> dict:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    data = await asyncio.get_running_loop().run_in_executor(None, _do)
    return [d["embedding"] for d in data["data"]]


# ============================================
# 主流程
# ============================================


async def main(args) -> int:
    from src.core.config import get_settings

    settings = get_settings()

    section("① 余弦相似度矩阵（真实 bge-m3）")
    texts = QUERIES + [t for _, t in TARGETS]
    try:
        vecs = await _embed(texts, settings)
    except Exception as e:  # noqa: BLE001
        fail(f"embedding 请求失败: {e}")
        return 1

    n_q, n_t = len(QUERIES), len(TARGETS)
    q_vecs, t_vecs = vecs[:n_q], vecs[n_q:]
    dims = len(q_vecs[0])
    ok(f"embedding 成功，维度 {dims}")

    print(f"\n  {'query':<22} | {'T1目标':>8} {'T2含颜料':>8} {'T3酒':>6} {'T4战斗':>6} {'T5画室':>6} | T1排名")
    print("  " + "-" * 78)
    for i, (q, qv) in enumerate(zip(QUERIES, q_vecs)):
        scores = [_cos(qv, tv) for tv in t_vecs]
        # 排名：目标 T1 在所有 5 条记忆中的相似度排序（1=最相关）
        rank = 1 + sum(1 for j, s in enumerate(scores) if j != 0 and s > scores[0])
        print(
            f"  {q:<22} | {scores[0]:>8.3f} {scores[1]:>8.3f} "
            f"{scores[2]:>6.3f} {scores[3]:>6.3f} {scores[4]:>6.3f} | 第{rank}名"
        )

    # 目标记忆自身的对比线索：T1 是否明显高于完全无关的 T3/T4
    s1 = _cos(q_vecs[0], t_vecs[0])
    s3 = _cos(q_vecs[0], t_vecs[2])
    s4 = _cos(q_vecs[0], t_vecs[3])
    print(f"\n  '颜料'  vs 目标T1: {s1:.3f}   vs 干扰T3: {s3:.3f}   vs 干扰T4: {s4:.3f}")
    gap = s1 - max(s3, s4)
    ok(f"  目标与最相关干扰的相似度差距: {gap:+.3f}（{'可区分' if gap > 0.05 else '区分度弱，需谨慎' if gap > 0 else '目标低于干扰，可能漏召回'}）")

    # mem0 search 默认 threshold=0.1：提示绝对分是否够
    if s1 < 0.1:
        warn(f"  注意: 目标T1 相似度 {s1:.3f} 低于 mem0 默认 threshold 0.1，可能被过滤")
    else:
        ok(f"  目标T1 相似度 {s1:.3f} 高于 mem0 默认 threshold 0.1")

    if not args.mem0:
        section("② 端到端 Mem0 召回（已跳过，加 --mem0 开启）")
        return 0

    # ===== 端到端：真实 mem0 写入 + 召回 =====
    section("② 端到端 Mem0 召回（独立临时 world_id）")
    from src.memory.backend import Mem0Memory

    backend = Mem0Memory.from_config(settings)
    world_id = make_world_id("embed_probe")
    step(f"临时 world_id = {world_id}")
    step("写入 5 条记忆（目标 + 干扰）…")
    backend.add_events(
        [{"text": t} for _, t in TARGETS],
        world_id=world_id,
        batch_turn_nums=[1, 2, 3, 4, 5],
        location="研究用临时世界",
    )
    ok("写入完成")

    for q in QUERIES:
        hits = backend.search_topk(q, world_id=world_id, top_k=5)
        names = {t: label for label, t in TARGETS}
        ranked = [
            f"{names.get(h.text, h.text[:12])}({h.score:.3f})" for h in hits
        ]
        t1_rank = next(
            (i + 1 for i, h in enumerate(hits) if h.text == TARGETS[0][1]), "未召回"
        )
        print(f"  query={q!r:<20} 命中顺序: {ranked}")
        print(f"        -> 目标T1 排名: {t1_rank}")

    warn(f"Mem0 无按世界删除接口，临时世界 '{world_id}' 的测试记忆将保留在 qdrant")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="语义召回命中率研究脚本")
    p.add_argument("--mem0", action="store_true", help="执行真实 Mem0 端到端召回（会写测试记忆到 qdrant）")
    raise SystemExit(run_async(main(p.parse_args())))
