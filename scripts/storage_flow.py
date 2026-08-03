# -*- coding: utf-8 -*-
"""
@File     :   storage_flow.py
@Desc     :   存储层真实链路演练：建世界 → 建实体 → 数值增减 → Tag → 轮次(+state_diff)
             → 近程窗口 → 回档 undo_turn → 历史冷备，全程走真实 data/app.db（独立 world_id）
@Note     :   默认每轮用真实 LLM 生成守秘人叙事写入 context_data（验证 LLM↔存储打通）；
             --no-llm 时用占位文本，纯存储链路调试。成功默认删除测试世界，--keep 保留。
运行: .\.venv\Scripts\python.exe scripts\storage_flow.py [--tier standard] [--no-llm] [--keep]
"""

from __future__ import annotations

import argparse

from _common import (
    add_common_args, fail, make_world_id, ok, run_async, section, step, warn,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="存储层真实链路演练")
    add_common_args(p)
    p.add_argument("--no-llm", action="store_true", help="不调用真实 LLM，用占位叙事文本")
    p.add_argument("--keep", action="store_true", help="保留测试数据（默认成功即清理）")
    return p


async def _narrate(args, action: str) -> str:
    from src.llm import ask_llm

    res = await ask_llm(
        args.tier,
        "你是《克苏鲁的呼唤》守秘人，只输出叙事正文，2-3 句，不要解释。",
        action,
    )
    if not res.is_ok:
        warn(f"LLM 叙事失败({res.error})，回退占位文本")
        return f"[守秘人叙事] {action}（LLM 不可用）"
    return res.text or ""


async def main(args) -> int:
    from src.core.db import get_db
    from src.storage.diff import (
        empty_diff, record_numeric_change, record_tag_change,
    )
    from src.storage.storage import Storage

    world_id = args.world_id or make_world_id("storage_flow")
    storage = Storage(db=get_db())

    section(f"存储链路演练 — world_id={world_id}")

    # 1) 建世界
    step("ensure_world")
    storage.ensure_world(
        world_id,
        player_ids=["pc_01"],
        game_phase="EXPLORATION",
        global_recap="初始前情：调查员们抵达阿卡姆的清晨，雾气弥漫。",
    )
    ok("世界已创建")

    # 2) 建实体
    step("create_entity × 2")
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=12, hp_max=12, mp=9, mp_max=9, san=60, san_max=99,
        attributes_and_skills={"侦查": 60, "潜行": 50, "急救": 40},
        inventory=[{"name": "左轮手枪", "ammo": 6}],
        tags=["清醒"],
    )
    storage.create_entity(
        world_id, "npc_01", "NPC", "旅馆老板",
        hp=10, hp_max=10, san=70, san_max=99,
        tags=["健谈"],
    )
    ok("PC/NPC 已创建")

    # 3) 数值增减 + Tag
    step("adjust_stat / add_tag / remove_tag")
    new_hp = storage.adjust_stat(world_id, "pc_01", "hp", -3)
    storage.add_tag(world_id, "pc_01", "手臂流血")
    storage.remove_tag(world_id, "pc_01", "清醒")
    if new_hp != 9:
        fail(f"adjust_stat 结果异常: hp={new_hp}（期望 9）")
        return 1
    ok("hp 12 → 9，tag: 清醒 → 手臂流血")

    # 4) 写两轮（含 state_diff）
    for turn_num in (1, 2):
        step(f"append_turn 第 {turn_num} 轮")
        action = (
            "费莉西蒂用手帕捂住流血的手臂，向旅馆老板打听昨夜马车夫的去向。"
            if turn_num == 1
            else "费莉西蒂顺着老板的指引走向后院马厩，闻到一股刺鼻的煤油味。"
        )
        narrative = await _narrate(args, action)
        diff = empty_diff()
        record_numeric_change(diff, "pc_01.hp", -1)
        if turn_num == 2:
            record_tag_change(diff, "pc_01", "警觉")
        storage.append_turn(
            world_id,
            turn_num=turn_num,
            context_data={
                "location": "旅店大厅" if turn_num == 1 else "后院马厩",
                "user": action,
                "assistant": narrative,
            },
            state_diff=diff,
        )
        ok(f"第 {turn_num} 轮已写入（叙事 {len(narrative)} 字）")

    # 5) 近程上下文
    step("get_recent_turns")
    recent = storage.get_recent_turns(world_id)
    ok("近程窗口 " + ", ".join(f"#{t['turn_num']}" for t in recent))

    # 6) 回档第 2 轮
    step("undo_turn 第 2 轮")
    storage.undo_turn(world_id, 2)
    pc = storage.get_entity(world_id, "pc_01")
    reverted_tags = pc["tags"]
    if "警觉" in reverted_tags:
        fail(f"回档后 Tag 未还原: {reverted_tags}")
        return 1
    ok(f"回档成功，pc tags 现值: {reverted_tags}, hp={pc['hp']}")

    # 7) 历史冷备
    step("append_history / query_history")
    storage.append_history(world_id, "user", "测试历史：调查员行动记录")
    storage.append_history(world_id, "assistant", "测试历史：守秘人叙事记录")
    history = storage.query_history(world_id, limit=10)
    ok(f"历史共 {len(history)} 条")

    # 8) 清理
    if not args.keep:
        storage.delete_world(world_id)
        ok(f"已清理测试世界 {world_id}")
    else:
        warn(f"--keep 已设置，保留 world_id={world_id} 供人工检查")

    print("\n[PASS] 存储链路全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
