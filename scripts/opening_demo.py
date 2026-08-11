# -*- coding: utf-8 -*-
"""
@File     :   opening_demo.py
@Desc     :   开场初始化 Agent（Opening Agent）真实链路模拟——建测试世界 + PC（职业可切换），
             走「开场决策（模组大纲提炼 + PC 背景碰撞）→ Narrator 演播 → Turn 0 三件套落库」
             全流程，观察不同职业（侦探/学者/警察）裁定出的切入场景差异
@Note     :   run_opening_narration 为对外入口——内部完成 Opening Agent 闭环（search_module +
             get_pc_background + present_opening 收尾）、场景报幕并入手记、Narrator 演播、
             落 turn 0 + seed 前情记忆 + mark_turns_solidified([0]) 防二次提炼；
             --module 支持无后缀模糊匹配（自动补 .pdf/.docx）；
             默认成功即清理 SQLite 世界与按 world_id 清理 RAG 测试记忆，--keep 保留。
运行: .\.venv\Scripts\python.exe scripts\opening_demo.py [--occupation 侦探] [--module 追书人] [--keep]
"""

from __future__ import annotations

import argparse
import json

from _common import (
    add_common_args, fail, make_world_id, ok, run_async, section, step, warn,
)

# 世界默认绑定的真实模组（须已放入 data/modules）
DEFAULT_MODULE = "追书人.pdf"

# 职业 -> PC 预设：观察 Opening Agent 据职业裁定不同切入场景（侦探/学者/警察）
_OCCUPATIONS = {
    "侦探": {
        "background": {
            "appearance_desc": "灰呢大衣，袖口磨出毛边，指尖常年沾着烟油。",
            "belief": "真相总要有人去翻出来。",
            "significant_person": "失踪的马瑟斯先生（旧识与导师）",
            "significant_place": "自己的事务所",
            "cherished_possession": "马瑟斯赠的旧怀表",
            "trait": "言语温和但观察敏锐",
            "full_backstory": "费莉西蒂·利丝从小在马瑟斯先生的事务所长大，马瑟斯一年前失踪，她继承了事务所，也继承了这份寻找真相的执念。",
        }
    },
    "学者": {
        "background": {
            "appearance_desc": "深蓝呢外套，夹着旧书，眼镜后的目光沉静。",
            "belief": "知识的传承比性命更重要。",
            "significant_person": "大学导师道格拉斯·金博尔（失踪的旧书主人）",
            "significant_place": "大学图书馆",
            "cherished_possession": "道格拉斯批注过的古书",
            "trait": "谨慎、博学",
            "full_backstory": "作为大学古籍研究员，费莉西蒂与道格拉斯·金博尔有过学术往来，听闻其失踪与旧书失窃，忧心忡忡。",
        }
    },
    "警察": {
        "background": {
            "appearance_desc": "风衣下的警徽，步伐沉稳。",
            "belief": "秩序必须在恐惧之前站稳。",
            "significant_person": "负责失踪案的老搭档",
            "significant_place": "警局档案室",
            "cherished_possession": "父亲留下的旧左轮",
            "trait": "果决、警觉",
            "full_backstory": "费莉西蒂是辖区警探，道格拉斯·金博尔失踪案一直悬而未决，昨晚书房失窃案让她再度被牵入其中。",
        }
    },
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Opening Agent 开场初始化真实链路模拟")
    add_common_args(p)
    # 覆盖公共 --module 默认值为真实模组（支持无后缀模糊匹配）
    p.set_defaults(module=DEFAULT_MODULE)
    p.add_argument(
        "--occupation", default="侦探", choices=list(_OCCUPATIONS),
        help="PC 职业（决定开场切入场景，默认 侦探）",
    )
    p.add_argument("--keep", action="store_true", help="保留测试数据（默认成功即清理）")
    return p


def _seed_world(storage, world_id: str, module: str, occupation: str) -> None:
    """建世界（绑定模组）+ 建一名带职业与背景的 PC。"""
    storage.ensure_world(world_id, module_name=module, player_ids=["pc_01"])
    bg = _OCCUPATIONS[occupation]["background"]
    storage.create_entity(
        world_id,
        entity_id="pc_01",
        entity_type="PC",
        name="费莉西蒂·利丝",
        occupation=occupation,
        hp=11,
        hp_max=11,
        mp=14,
        mp_max=14,
        san=70,
        san_max=70,
        attributes_and_skills={
            "力量": 50, "体质": 60, "敏捷": 60, "意志": 70,
            "外貌": 50, "教育": 70, "体型": 50, "智力": 70,
            "侦查": 75, "聆听": 60, "图书馆利用": 60, "心理学": 50,
        },
        inventory=[{"name": "笔记本"}, {"name": "钢笔"}],
        tags=[],
        background=bg,
    )
    ok(f"测试世界就绪：{world_id}（模组 {module}，PC 费莉西蒂·利丝[{occupation}]）")


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


async def main(args) -> int:
    from src.core.db import get_db
    from src.memory import Memory, Mem0Memory
    from src.storage.storage import Storage

    world_id = args.world_id or make_world_id("opening")
    storage = Storage(db=get_db())

    section("构建记忆后端（真实 Mem0/qdrant）")
    backend = Mem0Memory.from_config()
    memory = Memory(backend=backend, storage=storage, tier=args.tier)

    section(f"建测试世界 {world_id}（绑定模组 {args.module}，PC 职业 {args.occupation}）")
    _seed_world(storage, world_id, args.module, args.occupation)

    try:
        section("Opening Agent 开场决策（模组大纲提炼 + PC 背景碰撞）")
        from src.agent.opening import run_opening_narration

        opened = await run_opening_narration(
            storage, world_id, memory=memory,
        )
        print("\n---- 开场契约（OpeningSetupResult）----")
        print(f"场景报幕: {opened.setup.scene_tag}")
        print(f"大纲提炼: {opened.setup.summary or '（无）'}")
        print("前情记忆:")
        for m in opened.setup.seeded_memories:
            print(f"  · {m}")
        print("\n---- 开场导演手记 ----")
        print(opened.setup.narrative_directive)
        print("\n---- Turn 0 开场白（Narrator 演播）----")
        print(opened.narration)
        print("-----------------------------------------")

        section("Turn 0 三件套落库校验")
        turn0 = storage.get_turn(world_id, 0)
        if turn0 is None:
            fail("Turn 0 未落库")
            return 1
        ok(f"turn 0 已落库：assistant={len(turn0['context_data'].get('assistant') or '')} 字")
        ok(f"directive 权威副本 {len(turn0['context_data'].get('directive') or '')} 字")
        ok(f"solidified={turn0['solidified']}（已标记固化，后台 Worker 不二次提炼）")
        unsolid = storage.get_unsolidified_turns(world_id)
        ok(f"未固化轮次={len(unsolid)}（期望 0）")
        hits = await memory.search("道格拉斯", world_id, top_k=3)
        ok(f"seed 前情记忆可召回 {len(hits)} 条")
        for h in hits[:3]:
            print(f"  [t{h.turn_num}] {h.text[:60]}")
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
    print("\n[PASS] Opening Agent 开场初始化模拟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
