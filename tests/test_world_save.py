# -*- coding: utf-8 -*-
"""
@File     :   test_world_save.py
@Desc     :   世界存档/恢复测试：save 全量导出（四表+RAG+trace）/ 存档名校验 / 列表 /
             restore 回滚（改名恢复 / 覆盖已存在 / 存档缺失）/ 适配器命令路由
@Note     :   全程 FakeMemory + 真实 Storage（tmp 库）；backup 根目录 monkeypatch 到 tmp，
             trace 由 conftest autouse 隔离到 tmp；不触碰真实 data/backups 与 logs/traces
"""

from __future__ import annotations

import json

import pytest

from src.adapter.web.adapter import WebAdapter
from src.adapter.protocol import InboundMessage
from src.memory.fake import FakeMemory
from src.tools import world_save as ws


@pytest.fixture
def backup_root(tmp_path, monkeypatch):
    """把存档根目录重定向到独立临时目录。"""
    root = tmp_path / "backups"
    monkeypatch.setattr(ws, "_backup_root", lambda: root)
    return root


async def _seed_world(storage, world_id, mem, *, extra_entity=False):
    """写入世界数据：PC + 轮次 + 历史冷备 + RAG 记忆。"""
    storage.create_entity(
        world_id, "pc_01", "PC", "费莉西蒂",
        hp=10, san=58, attributes_and_skills={"侦查": 60}, background={"belief": "守护"},
    )
    if extra_entity:
        storage.create_entity(world_id, "npc_01", "NPC", "托马斯")
    storage.append_turn(
        world_id, turn_num=1,
        context_data={"user": "调查员行动", "kp": "守秘人回应"},
        state_diff={},
    )
    storage.append_history(world_id, "user", "调查员行动")
    await mem.seed_events(
        world_id, ["费莉西蒂在伦敦经营事务所。", "托马斯委托调查窃书案。"], turn_num=0,
    )


def _seed_trace(world_id):
    from src.webui.trace_engine import make_llm_request_event
    from src.webui.trace_store import get_trace_store

    store = get_trace_store()  # 状态：autouse 隔离到 tmp
    store.append(make_llm_request_event("smart", [], None, world_id=world_id, turn_num=1))
    return store


# ====================================================================
# 保存
# ====================================================================


async def test_save_world_exports_snapshot(storage, world_id, backup_root):
    """全量存档：四表 + RAG + trace 全部落盘，meta 统计正确。"""
    mem = FakeMemory(storage=storage)
    await _seed_world(storage, world_id, mem)
    _seed_trace(world_id)

    meta = ws.save_world(storage, mem, world_id, "s1")

    assert meta["save_name"] == "s1"
    assert meta["world_id"] == world_id
    assert meta["counts"] == {"entities": 1, "turns": 1, "history": 1, "rag": 2}
    dest = backup_root / world_id / "s1"
    assert (dest / "meta.json").exists()
    assert (dest / "world.json").exists()
    assert (dest / "rag.json").exists()
    assert (dest / "trace" / "turn-000001.jsonl").exists()
    # 四表内容与源一致
    wd = json.loads((dest / "world.json").read_text(encoding="utf-8"))
    assert wd["world"]["world_id"] == world_id
    assert wd["entities"][0]["name"] == "费莉西蒂"
    assert wd["turns"][0]["turn_num"] == 1
    assert wd["history"][0]["content"] == "调查员行动"


async def test_save_world_duplicate_and_invalid_name(storage, world_id, backup_root):
    """存档名重复 / 非法：抛 ValueError，不产生文件。"""
    mem = FakeMemory(storage=storage)
    await _seed_world(storage, world_id, mem)
    ws.save_world(storage, mem, world_id, "s1")
    with pytest.raises(ValueError):
        ws.save_world(storage, mem, world_id, "s1")
    with pytest.raises(ValueError):
        ws.save_world(storage, mem, world_id, "bad/name")
    with pytest.raises(ValueError):
        ws.save_world(storage, mem, world_id, "")


async def test_list_saves(storage, world_id, backup_root):
    """存档列表：跨世界扫描，按创建时间倒序。"""
    mem = FakeMemory(storage=storage)
    await _seed_world(storage, world_id, mem)
    ws.save_world(storage, mem, world_id, "s1")
    ws.save_world(storage, mem, world_id, "s2")
    # 另一个世界
    w2 = "world_save_other"
    storage.ensure_world(w2, module_name="test_module.docx")
    await _seed_world(storage, w2, mem)
    ws.save_world(storage, mem, w2, "alpha")

    saves = ws.list_saves()
    assert len(saves) == 3
    names = {(s["world_id"], s["save_name"]) for s in saves}
    assert names == {(world_id, "s1"), (world_id, "s2"), (w2, "alpha")}


# ====================================================================
# 恢复
# ====================================================================


async def test_restore_world_roundtrip_renamed(storage, world_id, backup_root):
    """存档 -> 删除原世界 -> 恢复到新世界名：四表/RAG/trace 全部还原。"""
    mem = FakeMemory(storage=storage)
    await _seed_world(storage, world_id, mem)
    _seed_trace(world_id)
    ws.save_world(storage, mem, world_id, "s1")
    # 删除源世界（SQLite/RAG/trace 全清）
    from src.memory.worker import delete_world

    await delete_world(storage, mem, world_id)

    restored = "world_restored_x"
    meta = await ws.restore_world(storage, mem, None, restored, "s1")

    assert storage.get_world(restored) is not None
    assert storage.get_entity(restored, "pc_01")["name"] == "费莉西蒂"
    assert storage.get_turn(restored, 1)["context_data"]["kp"] == "守秘人回应"
    assert len(storage.query_history(restored)) == 1
    # RAG 记忆还原（隔离键改写为新世界）
    hits = await mem.search("托马斯", restored)
    assert any("托马斯" in h.text for h in hits)
    # trace 还原
    from src.webui.trace_store import get_trace_store

    assert get_trace_store().count_turns(restored) == 1
    assert meta["rag_restored"] == 2


async def test_restore_world_overwrites_existing(storage, world_id, backup_root):
    """目标世界已存在：自动覆盖为存档内容（多余数据被清除）。"""
    mem = FakeMemory(storage=storage)
    await _seed_world(storage, world_id, mem)
    ws.save_world(storage, mem, world_id, "s1")
    # 目标世界追加数据，制造"脏状态"
    storage.create_entity(world_id, "npc_99", "NPC", "多余NPC")
    storage.append_turn(world_id, turn_num=99, context_data={"user": "x"}, state_diff={})
    await mem.seed_events(world_id, ["后加的残留记忆。"], turn_num=5)

    await ws.restore_world(storage, mem, None, world_id, "s1")

    assert storage.get_entity(world_id, "npc_99") is None          # 多余实体已清
    assert storage.get_turn(world_id, 99) is None                   # 多余轮次已清
    assert storage.get_turn(world_id, 1) is not None                # 存档轮次在
    # 覆盖后 RAG 只含存档记忆（残留记忆已清）
    assert mem.backend.count(world_id) == 2


async def test_restore_world_missing_save(storage, world_id, backup_root):
    with pytest.raises(FileNotFoundError):
        await ws.restore_world(storage, FakeMemory(storage=storage), None, "w", "no_such")


# ====================================================================
# 适配器命令路由
# ====================================================================


async def test_adapter_save_requires_game(storage, world_id, fake_llm, backup_root):
    """不在游戏中（未读世界）：/world save 提示不在游戏中。"""
    adapter = WebAdapter(storage=storage, memory=FakeMemory(storage=storage), llm=fake_llm.call)
    msg = await adapter.handle(InboundMessage.system_cmd("/world save s1", session_id="sid"))
    assert "不在游戏中" in msg.text


async def test_adapter_save_list_and_load_save(storage, world_id, fake_llm, backup_root):
    """命令链路：存档 / 存档列表 / 读取存档恢复并切换。"""
    mem = FakeMemory(storage=storage)
    await _seed_world(storage, world_id, mem)
    adapter = WebAdapter(storage=storage, memory=mem, llm=fake_llm.call)
    adapter._world_id = world_id

    out = await adapter.handle(InboundMessage.system_cmd("/world save 阶段一", session_id="sid"))
    assert "世界已存档" in out.text and "阶段一" in out.text

    out = await adapter.handle(InboundMessage.system_cmd("/world save list", session_id="sid"))
    assert "存档列表" in out.text and "阶段一" in out.text

    # 恢复到另一个世界名（改名恢复）
    restored = "world_restored_cmd"
    out = await adapter.handle(InboundMessage.system_cmd(
        f"/world load {restored} -save 阶段一", session_id="sid",
    ))
    assert "存档已读取" in out.text and restored in out.text
    assert adapter._world_id == restored
    assert storage.get_world(restored) is not None
