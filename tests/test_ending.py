# -*- coding: utf-8 -*-
"""
@File     :   test_ending.py
@Desc     :   结团检测与收尾管线测试：契约扩展 / 归一化 / Director 持久化终局字段 /
             终局收尾（演播+固化+快照+归档）/ 无静默降级失败语义 / 手动结团幂等 /
             Worker 跳过归档世界 / 适配器结团卡片与只读拦截
@Note     :   全程 fake LLM + FakeMemory 零网络零向量库；conftest 每测试独立临时库；
             终局轮复用同一轮不重复落轮（重试幂等）
"""

from __future__ import annotations

import pytest

from src.adapter.cli import CliAdapter
from src.adapter.protocol import InboundMessage, MessageType
from src.agent import (
    Director,
    NarrativeDirective,
    PRESENT_DIRECTIVE_SCHEMA,
    extract_ending,
    prepare_manual_ending,
    run_ending_wrapup,
    run_narrated_turn,
)
from src.core.exceptions import EndingError, MemoryOperationError, NarratorError
from src.core.ids import make_world_id
from src.memory.fake import FakeMemory
from src.memory.worker import ConsolidationWorker

# 与 conftest 中临时模组目录预置的文件名保持一致（可被绑定校验通过）
TEST_MODULE_NAME = "test_module.docx"


def _seed_pc(storage, world_id, hp=10, san=58):
    """在世界内创建一名调查员，供装配快照与终局决策使用。"""
    storage.update_world(world_id, player_ids=["pc_01"])
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=hp, hp_max=12, san=san, san_max=70,
        attributes_and_skills={"侦查": 60},
    )


def _ending_step(ending_type="TD", narrative="终局手记：怪物之心被摧毁，陵墓归于沉寂。"):
    """fake 响应：直接调用 present_directive 交卷并带终局信号（is_ending / ending_type）。"""
    return {
        "text": None,
        "tool_calls": [
            {
                "id": "c_end",
                "name": "present_directive",
                "arguments": {
                    "narrative_directive": narrative,
                    "is_ending": True,
                    "ending_type": ending_type,
                },
            }
        ],
    }


# ====================================================================
# 契约扩展与归一化
# ====================================================================


def test_present_directive_schema_has_ending_fields():
    """PRESENT_DIRECTIVE_SCHEMA 携带 is_ending / ending_type 自描述字段。"""
    props = PRESENT_DIRECTIVE_SCHEMA["function"]["parameters"]["properties"]
    assert "is_ending" in props
    assert props["is_ending"]["type"] == "boolean"
    assert "ending_type" in props
    assert props["ending_type"]["type"] == "string"
    assert set(props["ending_type"]["enum"]) == {"HD", "TD", "BD"}
    # 手记仍为唯一必填——终局字段可选，缺省非终局
    assert PRESENT_DIRECTIVE_SCHEMA["function"]["parameters"]["required"] == [
        "narrative_directive",
    ]


def test_extract_ending_normalization():
    """终局信号提取与归一化：缺失按非终局、类型缺失/非法兜底 TD、合法类型与小写归一保留。"""
    assert extract_ending(None) == (False, "")
    assert extract_ending({}) == (False, "")
    assert extract_ending({"is_ending": False, "ending_type": "HD"}) == (False, "")
    assert extract_ending({"is_ending": True}) == (True, "TD")  # 状态：类型缺失兜底 TD
    assert extract_ending({"is_ending": True, "ending_type": "unknown"}) == (True, "TD")
    assert extract_ending({"is_ending": True, "ending_type": "hd"}) == (True, "HD")  # 状态：小写归一
    assert extract_ending({"is_ending": True, "ending_type": "BD"}) == (True, "BD")


# ====================================================================
# Director 持久化终局字段
# ====================================================================


async def test_run_turn_ending_fields_persisted(storage, world_id, fake_llm):
    """present_directive 带终局信号：契约与落库 context_data 均持久化 is_ending/ending_type。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", _ending_step(ending_type="HD"))
    directive = await Director(storage, llm=fake_llm.call).run_turn(
        world_id, "炸毁封印入口"
    )
    assert directive.is_ending is True
    assert directive.ending_type == "HD"
    turn = storage.get_turn(world_id, directive.turn_num)
    assert turn["context_data"]["is_ending"] is True
    assert turn["context_data"]["ending_type"] == "HD"


async def test_run_turn_non_ending_defaults(storage, world_id, fake_llm):
    """常规轮不触发终局：is_ending 默认 False、ending_type 空串，context_data 不含终局键。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", lambda messages: "继续探索")
    directive = await Director(storage, llm=fake_llm.call).run_turn(world_id, "检查墙壁")
    assert directive.is_ending is False
    assert directive.ending_type == ""
    turn = storage.get_turn(world_id, directive.turn_num)
    assert "is_ending" not in turn["context_data"]


# ====================================================================
# 终局收尾管线（run_narrated_turn 终局分支）
# ====================================================================


async def test_run_narrated_turn_ending_wraps_up(storage, world_id, fake_llm):
    """终局轮完整收尾：终局演播 -> 叙事落库 -> 全盘固化 -> __ENDING__ 快照 -> 世界归档。"""
    _seed_pc(storage, world_id)
    storage.update_world(world_id, global_recap="调查员深入陵墓，逼近真相。")
    mem = FakeMemory(storage=storage)
    fake_llm.set_response("smart", _ending_step(ending_type="TD"))
    fake_llm.set_response(
        "standard",
        "【陵墓 - 墓室 - 黎明】\n可怖之物重归沉寂，调查员带着缺憾走出陵墓。",
    )
    turn = await run_narrated_turn(
        storage, world_id, "摧毁怪物的心脏", llm=fake_llm.call, memory=mem,
    )
    # 契约与交付物
    assert turn.ended is True
    assert turn.ending_type == "TD"
    assert turn.directive.is_ending is True
    assert turn.recap == "调查员深入陵墓，逼近真相。"
    # 终局叙事覆盖 assistant 落库 + 终局字段权威副本
    end_turn = storage.get_turn(world_id, turn.directive.turn_num)
    assert (
        end_turn["context_data"]["assistant"]
        == "【陵墓 - 墓室 - 黎明】\n可怖之物重归沉寂，调查员带着缺憾走出陵墓。"
    )
    assert end_turn["context_data"]["is_ending"] is True
    # 世界归档 + 全轮固化 + 终局快照
    assert storage.get_world(world_id)["status"] == "ARCHIVED"
    assert storage.get_unsolidified_turns(world_id) == []
    snaps = [i for i in mem.backend._store[world_id] if i.get("tag") == "__ENDING__"]
    assert len(snaps) == 1
    assert snaps[0]["ending_type"] == "TD"
    assert "__ENDING__" in snaps[0]["text"]
    assert "调查员深入陵墓" in snaps[0]["text"]  # 状态：最终 recap 并入快照


async def test_run_narrated_turn_ending_no_memory_raises(storage, world_id, fake_llm):
    """终局但 memory 未配置：抛 EndingError 无静默降级，世界保持 ACTIVE 可重试。"""
    _seed_pc(storage, world_id)
    fake_llm.set_response("smart", _ending_step(ending_type="BD"))
    with pytest.raises(EndingError):
        await run_narrated_turn(
            storage, world_id, "放弃调查", llm=fake_llm.call, memory=None,
        )
    # 终局轮已落库但未归档——修复后经 /world archive 复用同一轮重试
    assert storage.get_world(world_id)["status"] == "ACTIVE"
    assert any(
        t["context_data"].get("is_ending")
        for t in storage.get_recent_turns(world_id)
    )


# ====================================================================
# 无静默降级失败语义（run_ending_wrapup）
# ====================================================================


def _commit_ending_turn(storage, world_id, ending_type="TD", turn_num=1):
    """预落一终局轮，模拟 Director / prepare_manual_ending 已落库。"""
    storage.commit_turn(
        world_id,
        turn_num,
        state_diff={},
        context_data={
            "user": "[KP 主动结团]",
            "assistant": "终局手记",
            "directive": "终局手记",
            "is_ending": True,
            "ending_type": ending_type,
        },
    )
    return NarrativeDirective(
        state_changes={},
        narrative_directive="终局手记",
        turn_num=turn_num,
        converged=False,
        is_ending=True,
        ending_type=ending_type,
    )


class _OkNarrator:
    """演播必成功的假 Narrator。"""

    async def narrate(self, directive, **kwargs):
        return "终局演播文本"


class _BadNarrator:
    """演播必失败的假 Narrator，验证终局失败不归档。"""

    async def narrate(self, directive, **kwargs):
        raise NarratorError("演播崩溃")


class _FailingMemory:
    """固化必失败的假 memory，验证终局失败不归档、不写快照。"""

    def __init__(self):
        self.snapshot_called = False

    async def consolidate(self, world_id, *, up_to_turn=None):
        raise MemoryOperationError("固化失败")

    async def write_ending_snapshot(self, **kwargs):
        self.snapshot_called = True
        raise AssertionError("固化失败不应走到快照")


async def test_ending_wrapup_narration_failure_no_archive(storage, world_id):
    """终局演播失败：抛 EndingError，世界保持 ACTIVE，未写快照。"""
    mem = FakeMemory(storage=storage)
    directive = _commit_ending_turn(storage, world_id)
    with pytest.raises(EndingError):
        await run_ending_wrapup(
            storage, world_id, directive, memory=mem, narrator=_BadNarrator(),
        )
    assert storage.get_world(world_id)["status"] == "ACTIVE"
    assert mem.backend._store.get(world_id, []) == []


async def test_ending_wrapup_consolidate_failure_no_archive(storage, world_id):
    """全盘固化失败：抛 EndingError，世界保持 ACTIVE，快照不执行。"""
    mem = _FailingMemory()
    directive = _commit_ending_turn(storage, world_id, ending_type="BD")
    with pytest.raises(EndingError):
        await run_ending_wrapup(
            storage, world_id, directive, memory=mem, narrator=_OkNarrator(),
        )
    assert storage.get_world(world_id)["status"] == "ACTIVE"
    assert mem.snapshot_called is False


# ====================================================================
# 手动结团（/world archive 契约构造，重试幂等）
# ====================================================================


def test_manual_ending_creates_bd_turn(storage, world_id):
    """prepare_manual_ending：构造 BD 软结局契约并新落一终局轮。"""
    directive = prepare_manual_ending(storage, world_id, ending_type="BD")
    assert directive.is_ending is True
    assert directive.ending_type == "BD"
    turn = storage.get_turn(world_id, directive.turn_num)
    assert turn is not None
    assert turn["context_data"]["is_ending"] is True
    assert turn["context_data"]["user"] == "[KP 主动结团]"


def test_manual_ending_reuses_existing_turn(storage, world_id):
    """重试幂等：已存在未归档终局轮时复用同一轮，不重复落轮。"""
    d1 = prepare_manual_ending(storage, world_id, ending_type="BD")
    d2 = prepare_manual_ending(storage, world_id, ending_type="BD")
    assert d2.turn_num == d1.turn_num
    assert storage.next_turn_num(world_id) == d1.turn_num + 1  # 状态：未新增轮次


def test_manual_ending_archived_raises(storage, world_id):
    """已归档世界重复结团：抛 EndingError 拦截。"""
    storage.update_world(world_id, status="ARCHIVED")
    with pytest.raises(EndingError):
        prepare_manual_ending(storage, world_id)


# ====================================================================
# Worker 跳过归档世界
# ====================================================================


async def test_worker_skips_archived_worlds(storage, world_id, fake_llm):
    """Worker 扫描仅针对 ACTIVE 世界，归档世界不被固化（普通轮询静默跳过）。"""
    wid_active2 = make_world_id(901, "active2")
    storage.ensure_world(wid_active2, module_name=TEST_MODULE_NAME)
    wid_archived = make_world_id(902, "archived")
    storage.ensure_world(wid_archived, module_name=TEST_MODULE_NAME)
    storage.update_world(wid_archived, status="ARCHIVED")
    for w in (world_id, wid_active2, wid_archived):
        storage.commit_turn(w, 1, state_diff={}, context_data={"user": "行动"})

    mem = FakeMemory(storage=storage)
    worker = ConsolidationWorker(mem, storage=storage, min_turns=1)
    await worker._process_pending()

    # 活跃两世界被固化，归档世界轮次仍未固化
    assert storage.get_unsolidified_turns(world_id) == []
    assert storage.get_unsolidified_turns(wid_active2) == []
    assert storage.get_unsolidified_turns(wid_archived) != []


# ====================================================================
# 存储层 status 生命周期
# ====================================================================


def test_storage_world_status_lifecycle(storage, world_id):
    """status 默认 ACTIVE；update_world 置 ARCHIVED；list_worlds 按状态过滤；非法值拒绝。"""
    assert storage.get_world(world_id)["status"] == "ACTIVE"
    storage.update_world(world_id, status="ARCHIVED")
    assert storage.get_world(world_id)["status"] == "ARCHIVED"
    assert [w["world_id"] for w in storage.list_worlds(status="ARCHIVED")] == [world_id]
    assert storage.list_worlds(status="ACTIVE") == []
    with pytest.raises(ValueError):
        storage.update_world(world_id, status="NOPE")
    with pytest.raises(ValueError):
        storage.ensure_world("world_x_bad", module_name=TEST_MODULE_NAME, status="NOPE")


# ====================================================================
# 适配器消费：主动结团 / 终局卡片 / 只读拦截
# ====================================================================


async def test_adapter_world_archive_command(storage, world_id, fake_llm):
    """/world archive 主动结团：BD 软结局 -> 收尾归档 -> 终局卡片 -> 会话退回主菜单。"""
    storage.update_world(world_id, global_recap="调查告一段落。")
    mem = FakeMemory(storage=storage)
    adapter = CliAdapter(storage=storage, memory=mem, llm=fake_llm.call)
    adapter._world_id = world_id
    fake_llm.set_response(
        "standard", "【旧宅 - 走廊 - 黎明】\n故事在此落幕，余波未平。"
    )
    out = await adapter.handle(
        InboundMessage.system_cmd("/world archive", session_id="s")
    )
    assert out.type == MessageType.NARRATIVE
    assert "结 团 战 报" in out.text
    assert "BD" in out.text
    assert storage.get_world(world_id)["status"] == "ARCHIVED"
    assert adapter._world_id is None  # 状态：结团后会话退回主菜单
    assert any(
        i.get("tag") == "__ENDING__" for i in mem.backend._store[world_id]
    )


async def test_adapter_archived_world_blocks_player_input(storage, world_id, fake_llm):
    """归档世界只读：玩家输入被拦截，不触发管线、不新增轮次。"""
    adapter = CliAdapter(
        storage=storage, memory=FakeMemory(storage=storage), llm=fake_llm.call,
    )
    adapter._world_id = world_id
    storage.update_world(world_id, status="ARCHIVED")
    out = await adapter.handle(
        InboundMessage.player_input("继续调查", session_id="s")
    )
    assert out.type == MessageType.SYSTEM_MSG
    assert out.data["level"] == "warn"
    assert "已归档" in out.text
    assert storage.next_turn_num(world_id) == 1  # 状态：未新增轮次


async def test_adapter_archived_world_rollback_reactivates(storage, world_id, fake_llm):
    """归档世界 /rollback：撤销终局轮并自动解除归档恢复活跃（结团可反悔）。"""
    # 直接构造"已结团"状态：终局轮 + __ENDING__ 快照 + ARCHIVED
    storage.commit_turn(
        world_id, 1, state_diff={},
        context_data={
            "user": "[KP 主动结团]", "assistant": "终局手记",
            "is_ending": True, "ending_type": "BD",
        },
    )
    mem = FakeMemory(storage=storage)
    await mem.write_ending_snapshot(
        world_id, recap="最终 recap", ending_type="BD", narration="终局演播", turn_num=1,
    )
    storage.update_world(world_id, status="ARCHIVED")
    adapter = CliAdapter(storage=storage, memory=mem, llm=fake_llm.call)
    adapter._world_id = world_id

    out = await adapter.handle(
        InboundMessage.system_cmd("/rollback 1", session_id="s")
    )
    assert out.type == MessageType.SYSTEM_MSG
    assert "结团已撤销" in out.text
    assert storage.get_world(world_id)["status"] == "ACTIVE"  # 状态：解除归档
    assert storage.get_turn(world_id, 1) is None              # 终局轮已撤销
    # RAG 侧 __ENDING__ 快照被同步物理清除（turn>=1）
    assert all(i.get("tag") != "__ENDING__" for i in mem.backend._store[world_id])


async def test_adapter_archived_world_noop_rollback_keeps_archived(storage, world_id, fake_llm):
    """归档世界无操作回滚（/rollback latest+1）：终局轮保留，不解除归档。"""
    storage.commit_turn(
        world_id, 1, state_diff={},
        context_data={
            "user": "[KP 主动结团]", "assistant": "终局手记",
            "is_ending": True, "ending_type": "BD",
        },
    )
    mem = FakeMemory(storage=storage)
    storage.update_world(world_id, status="ARCHIVED")
    adapter = CliAdapter(storage=storage, memory=mem, llm=fake_llm.call)
    adapter._world_id = world_id

    out = await adapter.handle(
        InboundMessage.system_cmd("/rollback 2", session_id="s")
    )
    assert out.type == MessageType.SYSTEM_MSG
    assert "未撤销终局轮" in out.text
    assert storage.get_world(world_id)["status"] == "ARCHIVED"  # 状态：无操作不解除归档
    assert storage.get_turn(world_id, 1) is not None            # 终局轮保留


async def test_adapter_player_input_ending_resets_pointer(storage, world_id, fake_llm):
    """模型驱动终局：管线返回 ended 后适配器渲染结算卡片并重置会话世界指针。"""
    _seed_pc(storage, world_id)
    storage.update_world(world_id, global_recap="调查告一段落。")
    mem = FakeMemory(storage=storage)
    adapter = CliAdapter(storage=storage, memory=mem, llm=fake_llm.call)
    adapter._world_id = world_id
    fake_llm.set_response("smart", _ending_step(ending_type="TD"))
    fake_llm.set_response(
        "standard", "【红蔷薇之馆 - 大门前 - 黄昏】\n尘埃落定，缺憾长存。"
    )
    out = await adapter.handle(
        InboundMessage.player_input("摧毁怪物的心脏", session_id="s")
    )
    assert out.type == MessageType.NARRATIVE
    assert "TD" in out.text
    assert storage.get_world(world_id)["status"] == "ARCHIVED"
    assert adapter._world_id is None
