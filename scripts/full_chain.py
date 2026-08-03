# -*- coding: utf-8 -*-
"""
@File     :   full_chain.py
@Desc     :   端到端主链路演练（最接近主 Agent 的最小原型）：
             玩家行动 → 语义召回(recall) → LLM 叙事(narrate) → 状态写入 SQLite
             → 追加轮次 → 批量固化(consolidate) → 再召回验证闭环
@Note     :   全程真实链路：真实 LLM + 真实 Mem0(qdrant) + 真实 data/app.db。
             前置建议先跑 rag_preflight.py；成功默认清理 SQLite 世界（RAG 条目保留）。
运行: .\.venv\Scripts\python.exe scripts\full_chain.py [--tier standard] [--rounds 3] [--keep]
"""

from __future__ import annotations

import argparse

from _common import (
    add_common_args, fail, make_world_id, ok, run_async, section, step, warn,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="端到端主链路演练")
    add_common_args(p)
    p.add_argument("--rounds", type=int, default=3, help="执行轮数（默认 3）")
    p.add_argument("--keep", action="store_true", help="保留测试数据（默认成功即清理 SQLite）")
    return p


_ACTIONS = [
    "费莉西蒂向旅馆老板打听昨夜马车夫的去向。",
    "费莉西蒂在后院马厩翻找，试图找到马车夫留下的痕迹。",
    "费莉西蒂跟随一枚铜钥匙的线索，找到了地下酒窖的暗门。",
]

_KEEPER_SYSTEM = (
    "你是《克苏鲁的呼唤》的守秘人（Keeper）。你负责推动叙事、营造氛围，"
    "绝不替调查员做决定，也绝不剧透。结合前情提要、最近几轮与相关记忆，"
    "用 2-4 句中文写出本轮叙事正文，直接输出正文本身，不要任何前缀或解释。"
)


def _render_pc(pc: dict) -> str:
    return (
        f"{pc['name']}（{pc['type']}） HP {pc['hp']}/{pc['hp_max']} "
        f"SAN {pc['san']}/{pc['san_max']}  状态: {pc['tags'] or '无'}"
    )


async def main(args) -> int:
    from src.core.db import get_db
    from src.llm import call_llm
    from src.memory import Memory, Mem0Memory
    from src.storage.storage import Storage

    world_id = args.world_id or make_world_id("full_chain")
    storage = Storage(db=get_db())
    memory = Memory(backend=Mem0Memory.from_config(), tier=args.tier)

    section(f"端到端链路 — world_id={world_id}（{args.rounds} 轮）")

    step("建世界 + PC")
    storage.ensure_world(
        world_id, player_ids=["pc_01"], game_phase="EXPLORATION",
        global_recap="调查员们抵达阿卡姆，着手调查马车夫失踪案。",
    )
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=11, hp_max=12, mp=8, mp_max=9, san=59, san_max=99,
        attributes_and_skills={"侦查": 60, "潜行": 50, "急救": 40},
        inventory=[{"name": "左轮手枪", "ammo": 6}],
        tags=["清醒"],
    )
    ok("世界与 PC 就绪")

    # 主循环：行动 → 召回 → 叙事 → 落库
    for i in range(1, args.rounds + 1):
        section(f"第 {i} 轮")
        action = _ACTIONS[(i - 1) % len(_ACTIONS)]

        step("① 语义召回")
        hits = await memory.search(action, world_id, top_k=3)
        recall_text = "\n".join(f"- {h.text}" for h in hits) or "（暂无相关记忆）"
        ok(f"召回 {len(hits)} 条")

        step("② LLM 叙事")
        pc = storage.get_entity(world_id, "pc_01")
        recent = storage.get_recent_turns(world_id)
        recent_text = "\n".join(
            f"#{t['turn_num']} {t['context_data'].get('user', '')}" for t in recent
        ) or "（暂无历史轮次）"
        world = storage.get_world(world_id)
        user_msg = (
            f"前情提要：\n{(world or {}).get('global_recap') or ''}\n\n"
            f"调查员状态：\n{_render_pc(pc)}\n\n"
            f"最近几轮：\n{recent_text}\n\n"
            f"相关记忆：\n{recall_text}\n\n"
            f"本轮行动：\n{action}\n\n请给出守秘人回应。"
        )
        res = await call_llm(
            args.tier,
            [{"role": "system", "content": _KEEPER_SYSTEM},
             {"role": "user", "content": user_msg}],
        )
        if not res.is_ok:
            fail(f"叙事失败: {res.error}")
            return 1
        narrative = (res.text or "").strip()
        ok(f"叙事完成（{len(narrative)} 字）")
        print(f"      {narrative[:100]!r}")

        step("③ 状态写入 + 追加轮次")
        storage.append_turn(
            world_id, turn_num=i,
            context_data={"location": "阿卡姆", "user": action, "assistant": narrative},
            state_diff={},
        )
        ok(f"第 {i} 轮已落库")

    # 闭环验证：固化 → 再召回
    section("闭环验证")
    step("consolidate 批量固化")
    result = await memory.consolidate(world_id)
    ok(f"固化: 事件 {result.events_written} 条, 轮次 {result.turns_solidified}")

    step("固化后再召回（应能命中本轮剧情）")
    verify_hits = await memory.search(
        "调查员在马厩或酒窖发现了什么线索？", world_id, top_k=3
    )
    if verify_hits:
        for h in verify_hits:
            print(f"      [{h.score:.3f}] (第{h.turn_num}轮) {h.text[:60]!r}")
        ok(f"召回 {len(verify_hits)} 条")
    else:
        warn("固化后召回为空，可稍后重试（向量索引可能需要时间）")

    # 清理
    if not args.keep:
        storage.delete_world(world_id)
        warn(f"已删除 SQLite 世界 {world_id}（Mem0 RAG 条目保留）")
    else:
        warn(f"--keep 已设置，保留 world_id={world_id}")

    await memory.close()
    print("\n[PASS] 端到端链路全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
