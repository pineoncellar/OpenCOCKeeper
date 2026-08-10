# -*- coding: utf-8 -*-
"""
@File     :   narrator_demo.py
@Desc     :   润色节点 + 串行管线真实链路演示——复用 director_demo 的《红蔷薇之馆》测试世界与
             行动剧本，走 run_narrated_turn 全流程：Director 裁决（装配 → Function Calling
             闭环 → 叙事决策大纲）→ Narrator 演播（手记 + checks 权威区 → 玩家视角文本）
             → 玩家叙事覆盖 assistant 落库、手记转存 directive、checks 保留，
             并观察 Narrator 演播风格（首行场景报幕 / 零元语言 / 篇幅克制）
@Note     :   依赖 director_demo 的种子与清理函数（同目录复用，不重复造轮子）；
             真实链路：真实 LLM + 真实 Mem0(qdrant) + 真实 data/app.db；
             Director 用 --tier 档 + temperature 0.4（决策稳定），Narrator 走
             config.context.narrator（默认 standard / 0.7，文学档）；
             默认成功即清理 SQLite 世界与按 world_id 清理 RAG 测试记忆，--keep 保留。
运行: .\.venv\Scripts\python.exe scripts\narrator_demo.py [--rounds 3] [--keep]
"""

from __future__ import annotations

import argparse
import json

from _common import (
    add_common_args, fail, make_world_id, ok, run_async, section, step, warn,
)
from director_demo import (
    DEFAULT_MODULE, _ACTIONS, _MEMORIES, _RECENT, _purge_rag, _seed_world,
)

# Director 决策温度（低温度保规则裁决稳定；Narrator 温度走 config，不覆盖）
_DIRECTOR_TEMPERATURE = 0.4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Narrator + 串行管线全流程真实链路模拟")
    add_common_args(p)
    # add_common_args 的 --module 默认 test_module.docx，此处覆盖为真实模组
    p.set_defaults(module=DEFAULT_MODULE)
    p.add_argument("--rounds", type=int, default=3, help="主流程行动轮数（默认 3）")
    p.add_argument("--keep", action="store_true", help="保留测试数据（默认成功即清理）")
    return p


# ====================================================================
# 单轮串行管线（run_narrated_turn 展开演示）
# ====================================================================


async def _run_narrated_turn(
    storage, world_id: str, turn_num: int, action: str, tier: str
):
    from src.agent import run_narrated_turn

    section(f"第 {turn_num} 轮行动：{action}")

    step("Director 裁决（装配 → 闭环 → 契约）")
    step("Narrator 演播（手记 + checks 权威区 → 玩家文本）")
    step("玩家视角叙事落库（assistant=叙事 / directive=手记 / checks 保留）")
    narrated = await run_narrated_turn(
        storage, world_id, action,
        tier=tier, temperature=_DIRECTOR_TEMPERATURE,
    )
    directive = narrated.directive
    ok(f"turn={directive.turn_num} converged={directive.converged} "
       f"state_changes={json.dumps(directive.state_changes, ensure_ascii=False)}")
    for c in directive.checks:
        ok(f"check {c.get('entity_id')} {c.get('skill_or_attribute')} "
           f"{c.get('roll_value')}/{c.get('threshold')} {c.get('success_level_label')}")

    print("\n---- 导演手记（Narrator 输入）----")
    print(directive.narrative_directive)
    print("-----------------------------------")
    print("\n---- 玩家视角叙事（Narrator 输出）----")
    print(narrated.narration)
    print("--------------------------------------")
    print(f"      （叙事 {len(narrated.narration)} 字）")

    # 状态：落库校验——玩家叙事已覆盖 assistant，手记权威副本转存 directive
    rec = storage.get_turn(world_id, turn_num)
    cd = rec["context_data"]
    ok(f"落库 assistant={len(cd.get('assistant') or '')} 字 | "
       f"directive={len(cd.get('directive') or '')} 字 | "
       f"checks={len(cd.get('checks') or [])} 条")
    return narrated


async def main(args) -> int:
    from src.core.db import get_db
    from src.memory import Memory, Mem0Memory
    from src.storage.storage import Storage

    world_id = args.world_id or make_world_id("narrator")
    storage = Storage(db=get_db())

    section("构建记忆后端（真实 Mem0/qdrant）")
    backend = Mem0Memory.from_config()
    memory = Memory(backend=backend, storage=storage, tier=args.tier)

    section(f"建测试世界 {world_id}（绑定模组 {args.module}）")
    _seed_world(storage, backend, world_id, args.module)

    turn_base = len(_RECENT)  # 已铺 2 轮历史，主流程从 turn 3 开始
    try:
        for i in range(1, args.rounds + 1):
            action = _ACTIONS[(i - 1) % len(_ACTIONS)]
            narrated = await _run_narrated_turn(
                storage, world_id, turn_base + i, action, args.tier
            )
            if narrated is None:
                return 1
    finally:
        # 状态：无论成功或失败都清理测试数据；先关 memory 后端释放 qdrant 锁，
        # 再独立打开 qdrant 清理 RAG（同进程双重打开本地 qdrant 会文件锁冲突）
        if not args.keep:
            storage.delete_world(world_id)
            warn(f"已删除 SQLite 世界 {world_id}")
        else:
            warn(f"--keep 已设置，保留 world_id={world_id}")
        await memory.close()
        if not args.keep:
            _purge_rag(world_id)
    print("\n[PASS] Narrator + 串行管线全流程模拟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
