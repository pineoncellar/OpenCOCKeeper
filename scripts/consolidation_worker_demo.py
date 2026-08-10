# -*- coding: utf-8 -*-
"""
@File     :   consolidation_worker_demo.py
@Desc     :   后台固化 Worker 真实链路演示——建测试世界 + 追加未固化轮次（模拟管线落库），
             构造 ConsolidationWorker 走「事件触发 trigger_world」固化，观察阈值判定、
             stats 统计与固化后的语义召回；--fake 走 FakeMemory 零依赖速跑
@Note     :   复用 director_demo 的清理约定——默认成功即删除 SQLite 世界并按 world_id
             清理 RAG（先关 memory 后端释放 qdrant 锁再独立清理），--keep 保留；
             真实模式需 Mem0/embedding 就绪，--fake 免 LLM 免向量库
运行: .\.venv\Scripts\python.exe scripts\consolidation_worker_demo.py [--rounds 5] [--fake] [--keep]
"""

from __future__ import annotations

import argparse
import asyncio

from _common import (
    add_common_args, fail, make_world_id, ok, run_async, section, step, warn,
)

# 世界默认绑定的真实模组（须已放入 data/modules）
DEFAULT_MODULE = "红蔷薇之馆.pdf"

# 模拟若干轮"已落库"的剧情轮次（玩家行动 + 守秘人叙事），固化时会被提炼成原子事件
_ROUNDS = [
    ("调查员在储藏室发现一枚沾满干涸血迹的旧钥匙，并听见走廊尽头传来沙沙声。",
     "钥匙的齿痕磨损严重，血迹早已发黑；走廊尽头的沙沙声忽近忽远。"),
    ("调查员用钥匙打开了书房的门，在书桌抽屉里找到一封未署名的信。",
     "信纸边缘泛黄，字迹工整，落款处只有一枚樱花图案的印章。"),
    ("调查员阅读信件后脸色大变，将信重新塞回抽屉，快步走向二楼。",
     "信中提到'蜘蛛之母的卵即将孵化'，并警告不可直视阁楼上的那面镜子。"),
    ("调查员在二楼楼梯口遇见双目失明的绫小路樱雪，她怀中抱着一个陶瓷娃娃。",
     "樱雪轻声询问来客是否看见了'会走动的娃娃'，瓷娃娃的双眼似乎转向了调查员。"),
    ("调查员没有理会樱雪的提问，径直冲上阁楼，撞见镜中映出的一双绯红竖瞳。",
     "阁楼弥漫着陈旧的尘土味，镜面里倒映的却是空无一物的房间。"),
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="后台固化 Worker 真实链路演示")
    add_common_args(p)
    p.set_defaults(module=DEFAULT_MODULE)
    p.add_argument("--rounds", type=int, default=3, help="模拟落库轮数（默认 3，最多 5）")
    p.add_argument("--fake", action="store_true", help="用 FakeMemory 零依赖速跑（免 LLM/Mem0）")
    p.add_argument("--keep", action="store_true", help="保留测试数据（默认成功即清理）")
    return p


def _seed_world(storage, world_id: str, module: str, rounds: int) -> int:
    """建世界并追加未固化轮次，返回实际写入的轮次数。"""
    storage.ensure_world(world_id, module_name=module, player_ids=[])
    count = min(rounds, len(_ROUNDS))
    for i in range(1, count + 1):
        user, assistant = _ROUNDS[i - 1]
        storage.append_turn(
            world_id,
            turn_num=i,
            context_data={"user": user, "assistant": assistant},
        )
    return count


def _purge_rag(world_id: str) -> None:
    """按 world_id 过滤删除该世界 RAG 测试记忆（qdrant 直连，不依赖 Mem0 删除接口）。"""
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qm
        from src.core.config import get_settings

        rag = get_settings().get("memory.rag") or {}
        client = QdrantClient(path="data/mem0/qdrant")
        client.delete(
            rag.get("collection_name", "open_coc_keeper_mem"),
            points_selector=qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="user_id", match=qm.MatchValue(value=world_id)
                    )
                ]
            ),
        )
        ok("已按 world_id 清理 RAG 测试记忆")
    except Exception as e:  # noqa: BLE001
        warn(f"RAG 清理失败（可手动清理）: {e}")


# ====================================================================
# 主流程
# ====================================================================


async def main(args) -> int:
    from src.core.db import get_db
    from src.memory import ConsolidationWorker, FakeMemory, Memory
    from src.storage.storage import Storage

    world_id = args.world_id or make_world_id("worker_demo")
    storage = Storage(db=get_db())

    section(f"建测试世界 {world_id}（绑定模组 {args.module}）")
    rounds = _seed_world(storage, world_id, args.module, args.rounds)
    ok(f"{rounds} 轮未固化轮次就绪")

    if args.fake:
        memory = FakeMemory(storage=storage)
        step("FakeMemory：零 LLM 零向量库，直接把轮次当事件入库")
    else:
        from src.memory import Mem0Memory
        backend = Mem0Memory.from_config()
        memory = Memory(backend=backend, storage=storage, tier=args.tier)
        step("真实 Memory：LLM 提炼原子事件 + Mem0(qdrant) 写入 + recap 刷新")

    worker = ConsolidationWorker(
        memory, storage=storage,
        min_turns=2, min_interval=60, batch_limit=50,
    )

    try:
        section("事件触发（trigger_world，非阻塞）")
        worker._running = True  # 状态：手动置运行态演示事件通道（不跑后台轮询循环）
        worker.trigger_world(world_id)
        for _ in range(20):  # 状态：等待后台固化任务完成
            if world_id in worker._last_results:
                break
            await asyncio.sleep(0.05)
        worker._running = False
        if world_id in worker._last_results:
            ok(f"固化结果: {worker._last_results[world_id]}")
        else:
            warn("trigger_world 后未见固化结果，请检查上方日志")

        section("阈值判定回读")
        pending = storage.get_unsolidified_turns(world_id)
        step(f"固化后剩余未固化轮次: {len(pending)}（应为 0）")

        section("Worker stats")
        for k, v in worker.stats.items():
            step(f"{k}: {v}")

        section("固化后语义召回验证")
        hits = await memory.search("旧钥匙 书房 阁楼 镜子", world_id, top_k=3)
        if hits:
            for h in hits:
                ok(f"  [{h.score:.2f}] {h.text}")
        else:
            warn("未召回任何记忆（真实模式可调整 query 表述；--fake 模式按关键词打分）")

        section("物理回档（rollback_world，先物理后语义）")
        from src.memory import rollback_world
        deleted = await rollback_world(storage, memory, world_id, 2, worker=worker)
        ok(f"RAG 删除记忆条数: {deleted}")
        remaining = [t["turn_num"] for t in storage.get_recent_turns(world_id, limit=50)]
        step(f"SQLite 剩余轮次: {remaining}（回档到第 2 轮之前，应只含 turn 1）")
        hits_after = await memory.search("旧钥匙", world_id, top_k=3)
        if hits_after:
            ok(f"回档后 turn>=2 记忆已清、turn 1 的旧钥匙记忆保留（召回 {len(hits_after)} 条）")
        else:
            warn("回档后未召回任何记忆（--fake 模式可调整 query）")
    finally:
        # 状态：无论成功或失败都清理测试数据，避免残留测试世界；
        # 先关 memory 后端释放 qdrant 目录锁，再独立打开 qdrant 清理 RAG，
        # 否则同进程双重打开本地 qdrant 会触发文件锁冲突
        await memory.close()
        if not args.keep:
            storage.delete_world(world_id)
            warn(f"已删除 SQLite 世界 {world_id}")
            _purge_rag(world_id)
        else:
            warn(f"--keep 已设置，保留 world_id={world_id}")
    print("\n[PASS] 后台固化 Worker 演示通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
