# -*- coding: utf-8 -*-
"""
@File     :   memory_consolidate.py
@Desc     :   真实 RAG 固化 + 召回链路：写入多轮剧情 → Memory.consolidate 固化
             （LLM 提炼原子事件 → Mem0 写入 + 宏观 recap 写回 SQLite）→ search 语义召回验证
@Note     :   走真实 Mem0(qdrant 本地) + 真实 LLM；运行前建议先 rag_preflight.py 体检。
             Mem0 无按世界删除接口，RAG 条目会保留；--cleanup 只删 SQLite 世界（RAG 变孤儿）。
运行: .\.venv\Scripts\python.exe scripts\memory_consolidate.py [--tier standard] [--cleanup]
"""

from __future__ import annotations

import argparse

from _common import (
    add_common_args, fail, make_world_id, ok, run_async, section, step, warn,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="真实 RAG 固化 + 召回链路")
    add_common_args(p)
    p.add_argument("--cleanup", action="store_true",
                   help="测试后删除 SQLite 测试世界（RAG 条目保留）")
    p.add_argument("--query", default=None, help="自定义召回查询（缺省用内置示例）")
    return p


# 若干轮带剧情的轮次（内容互不相同，便于验证语义召回）
_ROUNDS = [
    (1, "旅店大厅", "费莉西蒂向旅馆老板打听昨夜马车夫的去向，得知对方清晨就驾车出了城。"),
    (2, "后院马厩", "费莉西蒂在马厩草料堆下发现一枚沾着暗红色污渍的铜钥匙。"),
    (3, "地下酒窖", "费莉西蒂用铜钥匙打开了酒窖暗门，看到墙上刻着一行陌生的符文。"),
]


async def main(args) -> int:
    from src.core.config import get_settings
    from src.core.db import get_db
    from src.memory import Memory, Mem0Memory, preflight
    from src.storage.storage import Storage

    # 0) 前置自检（含真实 embedding 连通）
    section("RAG 前置自检")
    report = await preflight(get_settings(), check_embedding=True)
    for c in report.checks:
        mark = {"OK": "OK ", "SKIP": "SKIP", "FAIL": "FAIL"}.get(c.status, c.status)
        print(f"  [{mark}] {c.name}: {c.detail}")
    if not report.all_ok:
        fail("环境未就绪，中止固化链路")
        return 1

    world_id = args.world_id or make_world_id("mem_consolidate")
    storage = Storage(db=get_db())
    memory = Memory(backend=Mem0Memory.from_config(), tier=args.tier)

    section(f"固化链路 — world_id={world_id}")
    step("建世界 + PC")
    storage.ensure_world(
        world_id, player_ids=["pc_01"], game_phase="EXPLORATION",
        global_recap="调查员们抵达阿卡姆，开始调查马车夫失踪案。",
    )
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=10, hp_max=12, san=58, san_max=99, tags=["警觉"],
    )
    ok("世界与 PC 就绪")

    # 1) 写入多轮剧情
    step("写入 3 轮剧情")
    for turn_num, location, text in _ROUNDS:
        storage.append_turn(
            world_id,
            turn_num=turn_num,
            context_data={
                "location": location,
                "user": text,
                "assistant": f"（守秘人叙事）{text}",
            },
            state_diff={},
        )
        ok(f"第 {turn_num} 轮已写入（{location}）")

    # 2) 固化
    step("Memory.consolidate（真实 LLM 提炼 + Mem0 写入 + recap 刷新）")
    result = await memory.consolidate(world_id)
    ok(f"固化完成: 事件 {result.events_written} 条, recap={result.recap_updated}, "
       f"轮次 {result.turns_solidified}")

    world = storage.get_world(world_id)
    recap = (world or {}).get("global_recap") or ""
    step("宏观 recap（写回 SQLite）")
    print(f"      recap: {recap[:120]!r}")
    if not recap:
        fail("global_recap 为空，宏观固化未生效")
        return 1
    ok(f"recap 已更新（{len(recap)} 字）")

    # 3) 语义召回
    step("Memory.search 语义召回")
    query = args.query or "马厩里发现了什么可疑物品？"
    hits = await memory.search(query, world_id, top_k=3)
    if not hits:
        warn("召回为空——可稍后重试（向量库可能仍在构建）或检查 embedding 端点")
    for h in hits:
        print(f"      [{h.score:.3f}] (第{h.turn_num}轮) {h.text[:60]!r}")
    ok(f"召回 {len(hits)} 条")

    # 4) 清理
    if args.cleanup:
        storage.delete_world(world_id)
        warn(f"已删除 SQLite 世界 {world_id}；注意 Mem0 中该世界 RAG 条目无法按世界删除，已保留")
    else:
        warn(f"保留数据以便复查：world_id={world_id}（RAG 条目 + SQLite 世界均保留）")

    await memory.close()
    print("\n[PASS] 固化 + 召回链路通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
