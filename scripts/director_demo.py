# -*- coding: utf-8 -*-
"""
@File     :   director_demo.py
@Desc     :   主 Agent（Director 全流程）真实链路模拟——以《红蔷薇之馆》（白鵺著）为测试基准，
             建测试世界 + PC（PL1 警察预设卡）+ 近程历史 + 预置记忆，
             走「装配 → Function Calling 闭环 → 叙事决策大纲 → 统一落库」全流程，
             观察主 Agent 自主调用 4 原子工具（search_module / query_memory /
             check_and_update_stats / manage_tags）；--probe 直接验证四工具接口
@Note     :   本编排即 Director.run_turn 的展开版，便于打印工具调用明细（Director 封装不暴露明细）；
             叙事润色（Narrator）与后台记忆固化不在本脚本范围；
             默认成功即清理 SQLite 世界与按 world_id 清理 RAG 测试记忆，--keep 保留。
运行: .\.venv\Scripts\python.exe scripts\director_demo.py [--rounds 3] [--probe] [--keep]
"""

from __future__ import annotations

import argparse
import json

from _common import (
    add_common_args, fail, make_world_id, ok, run_async, section, step, warn,
)

# 世界默认绑定的真实模组（须已放入 data/modules）
DEFAULT_MODULE = "红蔷薇之馆.pdf"

# 近程历史（turn 1/2）：模拟玩家从准备间醒来并探索储藏室的已发生剧情
_RECENT = [
    (
        1,
        "调查员在大理石台阶旁的储藏室醒来，检查自己小腿上的玫红色花纹，并尝试推开木窗。",
        "窗户仿佛粘在窗框上一样难以推开，透出玻璃能看到外面密密麻麻的黏稠蜘蛛网，封死了退路。",
    ),
    (
        2,
        "调查员在储藏室架子上翻找可用物资，拿走了一根警棍和一个手电筒。",
        "调查员获得了警棍与手电筒，警棍入手极为顺手，仿佛曾频繁使用过。",
    ),
]

# 预置记忆：模拟之前解锁的关键事实（呼应原模组背景）
_MEMORIES = [
    "调查员回忆起樱田镇最近举办过宝石展，核心展品是一枚被称为恶魔之卵的红宝石'蔷薇之血'。",
    "调查员在客厅茶几旁发现便签，上面提到了R大考古文研院院长白石孝介，以及关键词'阿特拉克-纳克亚'和德国人类学家'埃米尔·布施'。",
    "洋馆主人的女儿'绫小路樱雪'双目失明，被绫小路信一郎戴着一枚散发红色微光的蔷薇胸针。",
]

# 玩家行动剧本（3 轮）：精确诱导主 Agent 触发 4 原子工具
# Turn 3: 诱导 search_module (翻阅书房的拉丁文古籍《阿特拉克-纳克亚纸草》)
# Turn 4: 诱导 query_memory (根据樱雪脖子上的胸针与背部花纹联想记忆)
# Turn 5: 诱导 check_and_update_stats / manage_tags (试图撞开/撬开书房重门或进行侦查/检定)
_ACTIONS = [
    "调查员走进一楼书房，从大书架上抽取了那两卷拉丁文抄本《阿特拉克-纳克亚纸草》，仔细研读上面关于'蛛女之卵'与'附身宿主'的记载。",
    "调查员观察小女孩樱雪脖子上佩戴的红宝石'蔷薇之血'，回想此前在镇上听说的宝石展传闻以及'阿特拉克-纳克亚'的相关记载，确认其物理特征。",
    "调查员决定尝试用侦查技能仔细观察书房暗锁，并试图用力推开书房的大门，同时防备可能袭来的傀儡女仆。",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="主 Agent（Director）全流程真实链路模拟")
    add_common_args(p)
    # add_common_args 的 --module 默认 test_module.docx，此处覆盖为真实模组
    p.set_defaults(module=DEFAULT_MODULE)
    p.add_argument("--rounds", type=int, default=3, help="主流程行动轮数（默认 3）")
    p.add_argument("--probe", action="store_true", help="先直接验证四工具接口再跑闭环")
    p.add_argument("--keep", action="store_true", help="保留测试数据（默认成功即清理）")
    return p


def _accept_directive(**kwargs) -> dict:
    """present_directive 兜底 handler：正常路径由 stop 收敛拦截，此函数仅防失效。"""
    return {"ok": True, "accepted": True}


def _seed_world(storage, memory_backend, world_id: str, module: str) -> None:
    """建世界 + PC + 近程历史 + 预置记忆（完全契合《红蔷薇之馆》PL1 警察/PL3 状态）"""
    storage.ensure_world(
        world_id,
        module_name=module,
        player_ids=["pc_01"],
        global_recap="调查员在樱田镇山上的绫小路洋馆醒来，身上带有玫红色蛛网花纹，洋馆被细密蛛网封锁，正在寻找逃脱之法。",
    )

    # 按照原模组 PL1 预设卡数据配置（警察：力量50, 敏捷55, 侦查75, 斗殴70, 射击70，初始失忆）
    storage.create_entity(
        world_id,
        entity_id="pc_01",
        entity_type="PC",
        name="调查员",  # 初始失忆状态
        hp=10,
        hp_max=10,
        mp=11,
        mp_max=11,
        san=55,
        san_max=99,
        attributes_and_skills={
            "力量": 50,
            "体质": 50,
            "敏捷": 55,
            "意志": 55,
            "侦查": 75,
            "聆听": 75,
            "斗殴": 70,
            "手枪": 70,
            "急救": 60,
        },
        inventory=[{"name": "警棍"}, {"name": "手电筒"}],
        tags=["失忆", "小腿花纹"],
    )

    for turn_num, user, assistant in _RECENT:
        storage.append_turn(
            world_id,
            turn_num=turn_num,
            context_data={"user": user, "assistant": assistant},
        )

    events = [{"text": m} for m in _MEMORIES]
    memory_backend.add_events(
        events, world_id=world_id, batch_turn_nums=[2]
    )
    ok(f"红蔷薇之馆测试世界就绪：PC + 近程 {len(_RECENT)} 轮 + 预置记忆 {len(_MEMORIES)} 条")


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
# 四工具接口直接探针
# ====================================================================


async def _probe_tools(storage, memory, world_id: str) -> None:
    """针对《红蔷薇之馆》的四工具探针验证"""
    section("四工具接口直接探针（红蔷薇之馆）")
    from src.agent.loop import build_default_runner
    runner = build_default_runner(storage, memory=memory)
    probes = [
        # 探针 1：模组检索《阿特拉克-纳克亚纸草》或'蔷薇之血'
        ("search_module", {"query": "阿特拉克-纳克亚纸草 蛛女之卵 宿主", "top_k": 1}),
        # 探针 2：记忆检索红宝石相关记忆
        ("query_memory", {"queries": ["红宝石蔷薇之血与白石孝介的记忆"]}),
        # 探针 3：属性/技能检定（侦查）
        ("check_and_update_stats", {"entity_id": "pc_01", "skill_or_attribute": "侦查"}),
        # 探针 4：Tag 管理（增加'警觉'Tag）
        ("manage_tags", {"entity_id": "pc_01", "add_tags": ["警觉"]}),
    ]
    for name, args in probes:
        out = await runner.execute(name, args, world_id=world_id, turn_num=1)
        mark = ok if out.get("ok") else fail
        mark(f"{name} -> {json.dumps(out, ensure_ascii=False)[:160]}")
        if not out.get("ok"):
            warn(f"{name} 探针失败，请检查配置")


# ====================================================================
# 单轮闭环（展开 Director.run_turn 以便打印工具明细）
# ====================================================================


async def _run_turn(storage, memory, world_id: str, turn_num: int, action: str, tier: str):
    from src.agent.assembler import assemble
    from src.agent.directive import (
        PRESENT_DIRECTIVE_NAME, extract_narrative_directive,
    )
    from src.agent.loop import build_default_runner, run_tool_loop
    from src.agent.schemas import build_main_agent_schemas
    from src.tools.commit import apply_turn_change

    section(f"第 {turn_num} 轮行动：{action}")
    step("① 装配上下文")
    bundle = assemble(storage, world_id, action=action)
    ok(f"ContextBundle pc={bundle.pc_count} recent={bundle.recent_count}")

    step("② Function Calling 闭环（4 原子工具 + present_directive 收尾）")
    runner = build_default_runner(storage, memory=memory)
    runner.register(PRESENT_DIRECTIVE_NAME, _accept_directive)
    runner.reset_diffs()
    result = await run_tool_loop(
        None, tier, bundle.messages, build_main_agent_schemas(), runner,
        world_id=world_id, turn_num=turn_num, temperature=0.4,
        stop_tool_name=PRESENT_DIRECTIVE_NAME,
    )
    # 状态：无论成败都先打印工具调用明细，便于诊断触顶/循环问题
    for tc in result.tool_calls:
        brief = json.dumps(tc["output"], ensure_ascii=False)[:200]
        step(f"工具 {tc['name']}({json.dumps(tc['arguments'], ensure_ascii=False)})")
        print(f"        -> {brief}")
    if not result.tool_calls:
        warn("模型未调用任何工具")
    if not result.final.is_ok:
        fail(f"闭环失败: {result.final.error}")
        return None

    step("③ 契约化与落库")
    if result.stop_call:
        narrative = extract_narrative_directive(
            result.stop_call["arguments"], fallback=(result.final.text or "").strip()
        )
        ok(f"模型调用 {result.stop_call['name']} 交卷")
    elif result.final.text:
        narrative = result.final.text.strip()
        warn("模型未调收尾工具，以最终文本降级为导演手记")
    else:
        fail("模型未产出任何决策文本")
        return None
    record = apply_turn_change(
        storage, world_id, turn_num,
        diffs=runner.collected_diffs,
        context_data={"user": action, "assistant": narrative},
    )
    ok(f"落库 turn={record['turn_num']} state_diff={json.dumps(record['state_diff'], ensure_ascii=False)}")
    print("\n---- 叙事决策大纲 ----")
    print(narrative)
    print("----------------------")
    return record


async def main(args) -> int:
    from src.core.db import get_db
    from src.memory import Memory, Mem0Memory
    from src.storage.storage import Storage

    world_id = args.world_id or make_world_id("director")
    storage = Storage(db=get_db())

    section("构建记忆后端（真实 Mem0/qdrant）")
    backend = Mem0Memory.from_config()
    memory = Memory(backend=backend, storage=storage, tier=args.tier)

    section(f"建测试世界 {world_id}（绑定模组 {args.module}）")
    _seed_world(storage, backend, world_id, args.module)

    if args.probe:
        await _probe_tools(storage, memory, world_id)

    turn_base = len(_RECENT)  # 已铺 2 轮历史，主流程从 turn 3 开始
    try:
        for i in range(1, args.rounds + 1):
            action = _ACTIONS[(i - 1) % len(_ACTIONS)]
            record = await _run_turn(
                storage, memory, world_id, turn_base + i, action, args.tier
            )
            if record is None:
                return 1
    finally:
        # 状态：无论成功或失败都清理测试数据，避免残留测试世界；
        # 先关闭 memory 后端释放 qdrant 目录锁，再独立打开 qdrant 清理 RAG，
        # 否则同进程双重打开本地 qdrant 会触发文件锁冲突
        if not args.keep:
            storage.delete_world(world_id)
            warn(f"已删除 SQLite 世界 {world_id}")
        else:
            warn(f"--keep 已设置，保留 world_id={world_id}")
        await memory.close()
        if not args.keep:
            _purge_rag(world_id)
    print("\n[PASS] 主 Agent 全流程模拟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
