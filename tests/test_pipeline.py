# -*- coding: utf-8 -*-
"""
@File     :   test_pipeline.py
@Desc     :   管线层测试：场景切换 force 固化触发 / 场景手记 trace 事件携带
@Note     :   场景切换检测仅整块文本差异度比较（程序零解析手记内容），
             worker 未配置或手记未重写时静默跳过；全程 fake LLM 零网络
"""

from __future__ import annotations

import pytest

from src.agent import Director, Narrator, run_narrated_turn
from src.agent.pipeline import SCENE_TRANSITION_RATIO, _is_scene_transition
from src.webui.trace_engine import make_directive_event


class _FakeWorker:
    """记录 trigger_world 调用的桩 worker（force 透传断言用）。"""

    def __init__(self) -> None:
        self.calls: list = []

    def trigger_world(self, world_id: str, *, force: bool = False) -> None:
        self.calls.append((world_id, force))


def _seed_pc(storage, world_id):
    storage.update_world(world_id, player_ids=["pc_01"], global_recap="调查员抵达阿卡姆。")
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=10, hp_max=12, san=58, san_max=70,
        attributes_and_skills={"侦查": 60},
    )


def _directive_with_notes(narrative: str = "### 规则裁决\n- 无", scene_notes: str = ""):
    """首轮直接 present_directive 交卷；scene_notes 非空才携带（模拟模型漏填）。"""

    def step(messages):
        args = {"narrative_directive": narrative}
        if scene_notes:
            args["scene_notes"] = scene_notes
        return {"text": None, "tool_calls": [
            {"id": "c1", "name": "present_directive", "arguments": args}]}

    return step


async def _run_turn(storage, world_id, fake_llm, *, scene_notes: str, worker):
    """装配 director+narrator 跑一轮常规管线，返回 NarratedTurn。"""
    fake_llm.set_response("smart", _directive_with_notes(scene_notes=scene_notes))
    fake_llm.set_response("standard", "大厅内弥漫着陈旧的尘土气息……")
    director = Director(storage, llm=fake_llm.call, tier="smart")
    narrator = Narrator(llm=fake_llm.call)
    return await run_narrated_turn(
        storage, world_id, "行动",
        director=director, narrator=narrator, worker=worker,
    )


# ====================================================================
# 场景切换判定纯函数
# ====================================================================


def test_scene_transition_pure_rewrite():
    """旧手记被整块重写（场景转换）→ 判定为切换。"""
    assert _is_scene_transition("酒馆：酒保防备，隐瞒密室存在", "码头仓库：工头催促卸货")


def test_scene_transition_pure_evolution():
    """同场景增量修改 → 非切换。"""
    assert not _is_scene_transition("酒馆：酒保防备", "酒馆：酒保仍防备，玩家起疑")


def test_scene_transition_pure_edge_cases():
    """完全相同 / 任一侧为空 / 双侧为空 → 均非切换。"""
    assert not _is_scene_transition("场景A", "场景A")
    assert not _is_scene_transition("", "新场景")
    assert not _is_scene_transition("旧场景", "")
    assert not _is_scene_transition("", "")
    assert isinstance(SCENE_TRANSITION_RATIO, float) and 0 < SCENE_TRANSITION_RATIO < 1


# ====================================================================
# run_narrated_turn 场景切换 force 固化触发
# ====================================================================


async def test_scene_transition_triggers_force_solidify(storage, world_id, fake_llm):
    """新旧手记整块重写（场景转换）→ worker.trigger_world(force=True)。"""
    _seed_pc(storage, world_id)
    storage.update_world(world_id, scene_notes="酒馆：酒保防备，隐瞒密室存在")
    worker = _FakeWorker()
    turn = await _run_turn(
        storage, world_id, fake_llm,
        scene_notes="码头仓库：工头催促卸货，暗中监视", worker=worker,
    )
    assert worker.calls == [(world_id, True)]
    assert storage.get_world(world_id)["scene_notes"] == "码头仓库：工头催促卸货，暗中监视"
    assert turn.directive.scene_notes == "码头仓库：工头催促卸货，暗中监视"


async def test_scene_notes_evolution_skips_force(storage, world_id, fake_llm):
    """手记近似（同场景演进）→ 不触发 force 固化。"""
    _seed_pc(storage, world_id)
    storage.update_world(world_id, scene_notes="酒馆：酒保防备，隐瞒密室存在")
    worker = _FakeWorker()
    await _run_turn(
        storage, world_id, fake_llm,
        scene_notes="酒馆：酒保仍防备，密室继续隐瞒，玩家已起疑", worker=worker,
    )
    assert worker.calls == []


async def test_no_worker_skips_force(storage, world_id, fake_llm):
    """worker 未配置 → 静默跳过 force 触发，本轮仍正常交付。"""
    _seed_pc(storage, world_id)
    turn = await _run_turn(
        storage, world_id, fake_llm, scene_notes="码头仓库：新场景", worker=None,
    )
    assert turn.narration
    assert turn.directive.scene_notes == "码头仓库：新场景"


async def test_scene_notes_missing_keeps_old_no_force(storage, world_id, fake_llm):
    """交卷漏填手记 → 手记为空非切换，旧手记保留且不触发 force。"""
    _seed_pc(storage, world_id)
    storage.update_world(world_id, scene_notes="旧场景手记")
    worker = _FakeWorker()
    await _run_turn(storage, world_id, fake_llm, scene_notes="", worker=worker)
    assert worker.calls == []
    assert storage.get_world(world_id)["scene_notes"] == "旧场景手记"


# ====================================================================
# trace 事件携带场景手记
# ====================================================================


def test_make_directive_event_carries_scene_notes():
    """导演手记事件：scene_notes 非空才入事件，缺省不带（前端兜底空串）。"""
    ev = make_directive_event("导演手记", scene_notes="场景手记", world_id="w", turn_num=1)
    assert ev.event_type == "directive"
    assert ev.data["directive"] == "导演手记"
    assert ev.data["scene_notes"] == "场景手记"
    ev2 = make_directive_event("导演手记", world_id="w", turn_num=1)
    assert "scene_notes" not in ev2.data