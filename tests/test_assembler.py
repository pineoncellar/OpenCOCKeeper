# -*- coding: utf-8 -*-
"""
@File     :   test_assembler.py
@Desc     :   Context Assembler 装配器单测：四段式结构 / 空世界兜底 / 轮数裁剪
@Note     :   装配器纯同步、零 LLM，直接用 storage fixture 与临时库验证，不依赖 FakeLLM
"""

from __future__ import annotations

import pytest

from src.agent.assembler import (
    ContextBundle,
    DEFAULT_SYSTEM,
    assemble,
    render_background,
)
from src.core.exceptions import WorldNotFoundError


def _seed(storage, world_id, *, turns=3):
    """铺设调查员与近程轮次（context_data 含 user/assistant 两键，不维护场景元数据）。"""
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=11, hp_max=12, mp=8, mp_max=9, san=59, san_max=99,
        attributes_and_skills={"侦查": 60},
        inventory=[{"name": "左轮手枪", "ammo": 6}],
        tags=["清醒"],
    )
    for i in range(1, turns + 1):
        storage.append_turn(
            world_id, turn_num=i,
            context_data={"user": f"行动{i}", "assistant": f"回应{i}"},
        )


def test_assemble_basic(storage, world_id):
    """完整装配：四段式字段齐备，snapshot 含调查员硬数据与前情提要。"""
    storage.update_world(
        world_id,
        player_ids=["pc_01"],
        global_recap="调查员抵达阿卡姆，着手调查马车夫失踪案。",
    )
    _seed(storage, world_id)
    bundle = assemble(storage, world_id, action="搜查酒窖")
    assert isinstance(bundle, ContextBundle)
    assert bundle.system == DEFAULT_SYSTEM
    assert "总导演与规则裁决者" in bundle.system
    assert "严禁凭空脑补" in bundle.system
    assert "【前情提要】" in bundle.snapshot and "马车夫失踪案" in bundle.snapshot
    assert "费莉西蒂" in bundle.snapshot and "11/12" in bundle.snapshot
    assert "侦查60" in bundle.snapshot  # 技能表必须渲染进快照，供主 Agent 直接使用
    assert "左轮手枪×6" in bundle.snapshot
    assert "清醒" in bundle.snapshot
    assert "行动2" in bundle.recent and "回应2" in bundle.recent
    assert "【本轮行动】" in bundle.prompt and "搜查酒窖" in bundle.prompt
    assert bundle.pc_count == 1 and bundle.recent_count == 3
    assert bundle.messages[0] == {"role": "system", "content": bundle.system}
    assert bundle.messages[1] == {"role": "user", "content": bundle.prompt}


def test_assemble_empty_world(storage, world_id):
    """空世界兜底：无调查员、无轮次、空 recap 均不抛错，渲染占位文本。"""
    bundle = assemble(storage, world_id)
    assert "（暂无绑定调查员）" in bundle.snapshot
    assert "（暂无前情提要）" in bundle.snapshot
    assert "（暂无历史对话）" in bundle.recent
    assert bundle.pc_count == 0 and bundle.recent_count == 0


def test_assemble_background_not_injected(storage, world_id):
    """背景不再随快照注入：即使 PC 有完整背景，快照【调查员状态】也不含【角色背景】。
    背景为静态人物底稿，改由 get_pc_background 工具按需查询，省 token。
    """
    storage.update_world(world_id, player_ids=["pc_01"])
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        background={
            "appearance_desc": "略卷的蓝褐色长发，总是绑成高马尾",
            "belief": "真正的力量，不在于驱散阴影",
            "full_backstory": "费莉西蒂·利丝的少女时代……",
        },
    )
    bundle = assemble(storage, world_id)
    # 快照仍渲染物理真相（调查员状态），但不含背景小节与背景字段文本
    assert "费莉西蒂" in bundle.snapshot
    assert "【角色背景】" not in bundle.snapshot
    assert "形象描述" not in bundle.snapshot
    assert "真正的力量" not in bundle.snapshot


def test_assemble_pc_shows_occupation(storage, world_id):
    """快照【调查员状态】随名字渲染职业（读取接口之一）。"""
    storage.update_world(world_id, player_ids=["pc_01"])
    storage.create_entity(world_id, "pc_01", "PC", "费莉西蒂", occupation="医生")
    bundle = assemble(storage, world_id)
    assert "- 费莉西蒂（pc_01）[医生]" in bundle.snapshot


def test_render_background_interface():
    """render_background 接口：独立拼装长文段，空/无/占位值均返回空串。"""
    assert render_background(None) == ""
    assert render_background({}) == ""
    text = render_background(
        {"appearance_desc": "蓝发", "belief": "信念", "phobias_manias": "无"}
    )
    assert text == "【形象描述】蓝发\n【思想与信念】信念"


def test_background_semantics_in_query_memory_tool(storage, world_id):
    """背景语义特别提示已从基础提示词卸载，改为并入 query_memory 工具说明（不增系统负担）。"""
    from src.agent.schemas import build_tool_schemas

    bundle = assemble(storage, world_id)
    # 基础提示词不再背负背景语义（卸载成功）
    assert "进入模组剧情之前" not in bundle.system
    # 语义作为工具说明提供给 LLM：query_memory 的 description 含背景边界
    qm = next(
        s for s in build_tool_schemas()
        if s["function"]["name"] == "query_memory"
    )
    desc = qm["function"]["description"]
    assert "进入模组剧情之前" in desc
    assert "【角色背景】" in desc
    assert "人物底稿" in desc


def test_assemble_recent_limit(storage, world_id):
    """近程轮数裁剪：limit 生效且窗口内按正序保留。"""
    storage.update_world(world_id, player_ids=["pc_01"])
    _seed(storage, world_id, turns=15)
    bundle = assemble(storage, world_id, limit=5)
    assert bundle.recent_count == 5
    assert "第 15 轮" in bundle.recent
    assert "第 10 轮" not in bundle.recent


def test_assemble_custom_system(storage, world_id):
    """自定义元认知：system 参数整体覆盖默认模板。"""
    bundle = assemble(storage, world_id, system="自定义指令")
    assert bundle.system == "自定义指令"
    assert DEFAULT_SYSTEM not in bundle.system


def test_assemble_missing_world(storage):
    """世界不存在：抛 WorldNotFoundError。"""
    with pytest.raises(WorldNotFoundError):
        assemble(storage, "world_does_not_exist")
