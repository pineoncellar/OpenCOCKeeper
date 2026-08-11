# -*- coding: utf-8 -*-
"""
@File     :   ending_demo.py
@Desc     :   结团检测与收尾管线全流程真实链路演示——建测试世界 + PC + 预置剧情轮次，
             经手动结团（prepare_manual_ending，BD/TD/HD 可配）或模型驱动终局
             （run_narrated_turn，Director 自主判定 is_ending）进入终局收尾：
             Narrator 终局演播（NARRATOR_ENDING_SYSTEM）→ 终局叙事落库 →
             全盘固化（await consolidate）→ __ENDING__ 快照 → status=ARCHIVED，
             并校验归档只读语义（list_worlds(status="ACTIVE") 过滤）
@Note     :   覆盖单测外的真实链路：extract_ending 归一化、Director 终局字段持久化、
             run_ending_wrapup 顺序（演播→固化→快照→归档）与 worker 共享锁、
             __ENDING__ 快照语义召回、归档世界只读；
             --fake 走 FakeMemory 免向量库速跑（终局演播仍走真实 LLM 接口）；
             默认成功即清理 SQLite 世界并按 world_id 清理 RAG，--keep 保留
运行: .\.venv\Scripts\python.exe scripts\ending_demo.py [--entry manual|model] [--ending TD|BD|HD] [--rounds 3] [--fake] [--keep]
"""

from __future__ import annotations

import argparse

from _common import (
    add_common_args, fail, make_world_id, ok, run_async, section, step, warn,
)

DEFAULT_MODULE = "红蔷薇之馆.pdf"

# 模拟若干轮"已落库"的剧情轮次（玩家行动 + 守秘人叙事 + san 递减 diff），
# 结团时全盘固化会把这些轮次提炼成原子事件写入 RAG
_TURNS = [
    ("调查员进入红蔷薇之馆大厅，闻到一股灰尘味。", "大厅昏暗，壁炉里堆着未燃尽的木柴。"),
    ("调查员推开书房的门，找到一封未署名的信。", "信纸边缘泛黄，落款处只有樱花印章。"),
    ("调查员冲上阁楼，撞见镜中映出绯红竖瞳。", "阁楼弥漫尘土味，镜面倒映空无一物。"),
]

# 模型驱动终局入口使用的玩家行动：引导 Director 判定终局（摧毁心脏 = 收束冒险）
_MODEL_ENDING_ACTION = (
    "调查员举枪对准石棺中那颗仍在搏动的怪物心脏，扣下扳机——"
    "子弹贯穿心脏，可怖之物轰然倒塌。请裁决这场冒险的终局。"
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="结团检测与收尾管线全流程真实链路演示")
    add_common_args(p)
    p.set_defaults(module=DEFAULT_MODULE)
    p.add_argument(
        "--entry", default="manual", choices=["manual", "model"],
        help="终局入口：manual=主动结团（prepare_manual_ending，确定性）；"
             "model=模型驱动（run_narrated_turn，Director 自主判定 is_ending）",
    )
    p.add_argument(
        "--ending", default="BD", choices=["HD", "TD", "BD"],
        help="手动结团结局类型（model 入口由模型决定，忽略此参数；默认 BD）",
    )
    p.add_argument("--rounds", type=int, default=3, help="预置剧情轮数（默认 3）")
    p.add_argument("--fake", action="store_true", help="用 FakeMemory 免向量库速跑（演播仍走真实 LLM）")
    p.add_argument("--keep", action="store_true", help="保留测试数据（默认成功即清理）")
    return p


def _seed_world(storage, world_id: str, module: str, rounds: int) -> int:
    """建世界（绑定模组）+ PC + 多轮带 state_diff 的轮次，模拟已游玩的剧情增量。"""
    from src.storage.diff import empty_diff, record_numeric_change

    storage.ensure_world(
        world_id,
        module_name=module,
        player_ids=["pc_01"],
        global_recap="调查员进入红蔷薇之馆，线索渐渐汇聚。",
    )
    storage.create_entity(
        world_id, "pc_01", "PC", "调查员",
        hp=10, hp_max=12, san=58, san_max=70,
        attributes_and_skills={"侦查": 60, "意志": 70},
        inventory=[{"name": "警棍"}],
    )
    for i in range(1, rounds + 1):
        user, assistant = _TURNS[i - 1]
        diff = empty_diff()
        record_numeric_change(diff, "pc_01.san", -i)
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
# 终局收尾校验
# ====================================================================


async def _verify_ending(storage, memory, world_id: str, ended, entry: str) -> int:
    """校验终局收尾的每一步：演播产出 / 终局轮落库 / 全盘固化 / 快照召回 / 归档只读。"""
    section("终局收尾校验")
    # 终局演播文本产出（NARRATOR_ENDING_SYSTEM 演播）
    ok(f"终局演播文本 {len(ended.narration)} 字（首行场景报幕 + 闭幕/后日谈）")
    # 终局轮落库：assistant 覆盖为终局演播 + 终局字段权威副本
    latest = storage.next_turn_num(world_id) - 1
    turn = storage.get_turn(world_id, latest)
    if turn is None:
        fail(f"终局轮 #{latest} 未落库")
        return 1
    cd = turn["context_data"]
    ok(f"终局轮 #{latest} 落库：assistant={len(cd.get('assistant') or '')} 字")
    ok(f"is_ending={cd.get('is_ending')} ending_type={cd.get('ending_type')}（权威副本）")
    # 世界归档 + 最终 recap + 全轮固化
    world = storage.get_world(world_id)
    if world.get("status") != "ARCHIVED":
        fail(f"世界未归档: status={world.get('status')}（期望 ARCHIVED）")
        return 1
    ok(f"世界状态 {world['status']}（固化成功后归档）")
    ok(f"最终 recap {len(world.get('global_recap') or '')} 字")
    unsolid = storage.get_unsolidified_turns(world_id)
    ok(f"未固化轮次={len(unsolid)}（期望 0，全盘固化完成）")
    # __ENDING__ 快照语义召回（搜结局类型，命中带 __ENDING__ 标记的核心锚点）
    hits = await memory.search(ended.ending_type, world_id, top_k=5)
    ending_hits = [h for h in hits if "__ENDING__" in (h.text or "")]
    ok(f"__ENDING__ 快照召回 {len(ending_hits)} 条（query='{ended.ending_type}'）")
    for h in ending_hits[:2]:
        print(f"  [t{h.turn_num}] {h.text[:80]}")
    # 归档只读语义：ACTIVE 世界列表不再包含该世界（Worker 轮询亦跳过）
    active = [w["world_id"] for w in storage.list_worlds(status="ACTIVE")]
    if world_id in active:
        fail("归档世界仍出现在 ACTIVE 列表")
        return 1
    ok("归档世界已从 ACTIVE 列表剔除（Worker 轮询静默跳过）")
    ok(f"终局入口 [{entry}] -> ending_type={ended.ending_type} 全流程校验通过")
    return 0


# ====================================================================
# 主流程
# ====================================================================


async def main(args) -> int:
    from src.core.db import get_db
    from src.memory import ConsolidationWorker, FakeMemory, Memory
    from src.storage.storage import Storage

    world_id = args.world_id or make_world_id("ending_demo")
    storage = Storage(db=get_db())

    if args.fake:
        memory = FakeMemory(storage=storage)
        step("FakeMemory：免向量库速跑（固化直写内存桶，演播仍走真实 LLM）")
    else:
        from src.memory import Mem0Memory
        memory = Memory(backend=Mem0Memory.from_config(), storage=storage, tier=args.tier)
        step("真实 Memory：LLM 提炼原子事件 + Mem0(qdrant) 写入 + recap 刷新")

    # worker 仅用于演示收尾与后台固化共享同一把 per-world 锁（不实际跑固化轮询）
    worker = ConsolidationWorker(memory, storage=storage, min_turns=1)

    section(f"建测试世界 {world_id}（绑定模组 {args.module}，预置 {args.rounds} 轮剧情）")
    rounds = _seed_world(storage, world_id, args.module, args.rounds)
    pc = storage.get_entity(world_id, "pc_01")
    step(f"{rounds} 轮落库：san={pc['san']}（turn1:-1 / turn2:-2 / turn3:-3）")

    try:
        if args.entry == "model":
            # 模型驱动终局：真实 LLM 走主 Agent 闭环，Director 自主判定 is_ending 并交卷
            section("模型驱动终局（run_narrated_turn，Director 自主判定）")
            from src.agent.pipeline import run_narrated_turn

            turn = await run_narrated_turn(
                storage, world_id, _MODEL_ENDING_ACTION,
                memory=memory, worker=worker,
            )
            if not turn.ended:
                fail("模型未判定终局（is_ending 未触发），未进入收尾管线")
                fail("可改用 --entry manual 走确定性手动结团")
                return 1
            ok(f"Director 判定终局：is_ending=True ending_type={turn.ending_type}")
            return await _verify_ending(storage, memory, world_id, turn, "model")

        # 手动结团：prepare_manual_ending 构造 BD/TD/HD 终局契约 -> run_ending_wrapup 收尾
        section(f"手动结团（prepare_manual_ending ending={args.ending} -> run_ending_wrapup）")
        from src.agent.pipeline import prepare_manual_ending, run_ending_wrapup

        directive = prepare_manual_ending(
            storage, world_id, ending_type=args.ending,
        )
        ok(f"终局契约就绪：turn={directive.turn_num} is_ending={directive.is_ending} "
           f"ending_type={directive.ending_type}")
        ended = await run_ending_wrapup(
            storage, world_id, directive, memory=memory, worker=worker,
        )
        print("\n---- 终局演播文本（Narrator / NARRATOR_ENDING_SYSTEM）----")
        print(ended.narration)
        print("---------------------------------------------------------")
        return await _verify_ending(storage, memory, world_id, ended, "manual")
    finally:
        # 状态：无论成功或失败都清理测试数据，避免残留测试世界；
        # 先关闭 memory 后端释放 qdrant 目录锁，再独立打开 qdrant 清理 RAG
        if not args.keep:
            storage.delete_world(world_id)
            warn(f"已删除 SQLite 世界 {world_id}")
        else:
            warn(f"--keep 已设置，保留 world_id={world_id}")
        await memory.close()
        if not args.keep:
            _purge_rag(world_id)
    print("\n[PASS] 结团检测与收尾管线模拟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
