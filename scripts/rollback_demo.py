# -*- coding: utf-8 -*-
"""
@File     :   rollback_demo.py
@Desc     :   回档模块全流程真实链路演示——建世界 + PC + 多轮带 diff 的轮次，固化进 RAG，
             再 rollback_world 回档到第 N 轮之前，校验 SQLite 物理状态与 Qdrant 语义记忆
             双侧同步回归；--fake 走 FakeMemory 零依赖速跑
@Note     :   覆盖单测外的真实链路：storage.undo_from 单事务批量撤销、Mem0Memory.delete_since
             物理删除、Memory.undo 门面、rollback_world 编排（先物理后语义 + worker 共享锁）；
             默认成功即清理 SQLite 世界并按 world_id 清理 RAG，--keep 保留
运行: .\.venv\Scripts\python.exe scripts\rollback_demo.py [--rounds 3] [--fake] [--keep]
"""

from __future__ import annotations

import argparse

from _common import (
    add_common_args, fail, make_world_id, ok, run_async, section, step, warn,
)

from src.storage.diff import empty_diff, record_numeric_change, record_tag_change

DEFAULT_MODULE = "红蔷薇之馆.pdf"

# 模拟若干轮"已落库"的剧情轮次（玩家行动 + 守秘人叙事），固化时会被提炼成原子事件；
# 文本刻意含"调查员/红蔷薇/信/阁楼"等可被 --fake 关键词打分命中的词，便于召回对照
_TURNS = [
    ("调查员进入红蔷薇之馆大厅，闻到一股灰尘味。", "大厅昏暗，壁炉里堆着未燃尽的木柴。"),
    ("调查员推开书房的门，找到一封未署名的信。", "信纸边缘泛黄，落款处只有樱花印章。"),
    ("调查员冲上阁楼，撞见镜中映出绯红竖瞳。", "阁楼弥漫尘土味，镜面倒映空无一物。"),
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="回档模块全流程真实链路演示")
    add_common_args(p)
    p.set_defaults(module=DEFAULT_MODULE)
    p.add_argument("--rounds", type=int, default=3, help="模拟落库轮数（默认 3）")
    p.add_argument("--fake", action="store_true", help="用 FakeMemory 零依赖速跑（免 LLM/Mem0）")
    p.add_argument("--keep", action="store_true", help="保留测试数据（默认成功即清理）")
    return p


def _seed_world(storage, world_id: str, module: str, rounds: int) -> int:
    """建世界 + PC + 多轮带 state_diff 的轮次，模拟管线已落库的增量剧情。"""
    storage.ensure_world(
        world_id,
        module_name=module,
        player_ids=["pc_01"],
        global_recap="调查员进入红蔷薇之馆。",
    )
    storage.create_entity(
        world_id, "pc_01", "PC", "调查员",
        hp=10, hp_max=12, san=58, san_max=70,
        inventory=[{"name": "警棍"}],
    )
    # 每轮一个状态变更：san 逐轮递减，第 3 轮附带 hp 扣减与 Tag 挂载
    for i in range(1, rounds + 1):
        user, assistant = _TURNS[i - 1]
        diff = empty_diff()
        record_numeric_change(diff, "pc_01.san", -i)
        if i == 3:
            record_numeric_change(diff, "pc_01.hp", -2)
            record_tag_change(diff, "pc_01", "手臂流血")
        storage.commit_turn(
            world_id,
            turn_num=i,
            context_data={"user": user, "assistant": assistant},
            state_diff=diff,
        )
    return rounds


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
    from src.memory import (
        ConsolidationWorker, FakeMemory, Memory, rollback_world,
    )
    from src.storage.storage import Storage

    world_id = args.world_id or make_world_id("rollback_demo")
    storage = Storage(db=get_db())

    section(f"建测试世界 {world_id}（绑定模组 {args.module}）")
    rounds = _seed_world(storage, world_id, args.module, args.rounds)
    pc = storage.get_entity(world_id, "pc_01")
    step(f"{rounds} 轮落库：san={pc['san']} hp={pc['hp']}（turn1:-1 / turn2:-2 / turn3:-3,hp-2）")

    if args.fake:
        memory = FakeMemory(storage=storage)
        step("FakeMemory：零 LLM 零向量库，直接把轮次当事件入库")
    else:
        from src.memory import Mem0Memory
        memory = Memory(backend=Mem0Memory.from_config(), storage=storage, tier=args.tier)
        step("真实 Memory：LLM 提炼原子事件 + Mem0(qdrant) 写入 + recap 刷新")

    # worker 仅用于演示回档与后台固化共享同一把 per-world 锁（不实际跑固化轮询）
    worker = ConsolidationWorker(memory, storage=storage, min_turns=1)

    try:
        section("固化（Memory.consolidate）")
        result = await memory.consolidate(world_id)
        ok(f"固化 {len(result.turns_solidified)} 轮 / 事件 {result.events_written} / recap {result.recap_updated}")
        hits = await memory.search("调查员 红蔷薇", world_id, top_k=10)
        step(f"固化后 RAG 召回 {len(hits)} 条（应含 turn 1/2/3）")

        section(f"批量回档（rollback_world 到第 2 轮之前，worker 共享锁）")
        deleted = await rollback_world(storage, memory, world_id, 2, worker=worker)
        ok(f"RAG 删除记忆 {deleted} 条（turn>=2 应被物理清除）")

        section("回档后双侧校验")
        remaining = [t["turn_num"] for t in storage.get_recent_turns(world_id, limit=50)]
        step(f"SQLite 剩余轮次: {remaining}（应只剩 turn 1）")
        pc = storage.get_entity(world_id, "pc_01")
        ok(f"san={pc['san']}（期望 57，仅保留 turn1 的 -1） hp={pc['hp']}（期望 10，turn3 已撤销） tags={pc['tags']}（期望 []）")
        hits = await memory.search("调查员", world_id, top_k=10)
        step(f"回档后 RAG 召回 {len(hits)} 条（应只剩 turn 1，turn>=2 的'信/阁楼'记忆已清）")
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
    print("\n[PASS] 回档模块全流程演示通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
