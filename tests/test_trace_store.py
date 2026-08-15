# -*- coding: utf-8 -*-
"""
@File     :   test_trace_store.py
@Desc     :   TraceStore 持久化测试 — 每世界每轮 JSONL 写读回 / 世界隔离 / 分页 / 坏行容错
@Note     :   autouse 夹具把 TRACE_DIR 重定向到 tmp_path 并重置单例，杜绝污染真实 logs/traces；
             同时覆盖 TraceBus.publish 自动落盘（生产端经 publish 即持久化）
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from src.webui.trace_engine import get_trace_bus, make_llm_request_event
from src.webui.trace_store import TraceStore, get_trace_store



@pytest.fixture
def store(tmp_path):
    """指向独立临时目录的 TraceStore（不依赖全局单例）。"""
    return TraceStore(root=tmp_path / "traces")


# ====================================================================
# 写读回与轮次文件命名
# ====================================================================


async def test_append_load_roundtrip(store):
    store.append(make_llm_request_event("smart", [{"role": "user", "content": "hi"}], None,
                                        world_id="w1", turn_num=3))
    events = store.load_turn("w1", 3)
    assert len(events) == 1
    assert events[0].world_id == "w1"
    assert events[0].turn_num == 3
    assert events[0].data["messages"][0]["content"] == "hi"


def test_turn_path_uses_six_digit_name(store):
    path = store._root / "w1" / "turn-000001.jsonl"
    store.append(make_llm_request_event("smart", [], None, world_id="w1", turn_num=1))
    assert path.exists()
    assert not (store._root / "w1" / "turn-1.jsonl").exists()


async def test_append_order_preserved(store):
    # 状态：同一轮多次追加，读回保持追加顺序
    for i in range(3):
        store.append(make_llm_request_event("smart", [{"role": "user", "content": str(i)}], None,
                                            world_id="w1", turn_num=0))
    events = store.load_turn("w1", 0)
    assert [e.data["messages"][0]["content"] for e in events] == ["0", "1", "2"]


# ====================================================================
# 世界隔离与列表
# ====================================================================


async def test_world_isolation(store):
    store.append(make_llm_request_event("smart", [], None, world_id="wa", turn_num=1))
    store.append(make_llm_request_event("smart", [], None, world_id="wb", turn_num=1))
    assert sorted(store.list_world_ids()) == ["wa", "wb"]
    assert store.load_turn("wa", 1) and not store.load_turn("wa", 2)
    assert store.load_turn("wb", 1) and store.load_turn("wa", 1) != store.load_turn("wb", 1)


async def test_list_turns_desc_order_and_paging(store):
    for t in range(1, 6):
        store.append(make_llm_request_event("smart", [], None, world_id="w1", turn_num=t))
    assert store.count_turns("w1") == 5
    # 状态：倒序（最新在前）
    page1 = store.list_turns("w1", limit=2, offset=0)
    assert [m.turn_num for m in page1] == [5, 4]
    page2 = store.list_turns("w1", limit=2, offset=2)
    assert [m.turn_num for m in page2] == [3, 2]
    assert store.list_turns("w1", limit=2, offset=4)[0].turn_num == 1
    # 状态：事件数元信息
    assert page1[0].event_count == 1


async def test_world_summaries(store):
    store.append(make_llm_request_event("smart", [], None, world_id="w1", turn_num=2))
    store.append(make_llm_request_event("smart", [], None, world_id="w1", turn_num=5))
    store.append(make_llm_request_event("smart", [], None, world_id="w2", turn_num=1))
    summaries = {s["world_id"]: s for s in store.world_summaries()}
    assert summaries["w1"]["turn_count"] == 2
    assert summaries["w1"]["latest_turn"] == 5
    assert summaries["w2"]["turn_count"] == 1


# ====================================================================
# 容错
# ====================================================================


async def test_bad_line_skipped(store):
    store.append(make_llm_request_event("smart", [], None, world_id="w1", turn_num=1))
    path = store._root / "w1" / "turn-000001.jsonl"
    # 状态：手动追加一行半截 JSON（模拟写断），读取应跳过不崩溃
    with path.open("a", encoding="utf-8") as f:
        f.write('{"timestamp": "broken", "event_ty\n')
    events = store.load_turn("w1", 1)
    assert len(events) == 1
    assert events[0].event_type == "llm_request"


async def test_append_with_path_traversal_world(store):
    # 状态：world_id 含路径分隔符被清洗为安全目录名，不越界写
    evil = "../escape"
    store.append(make_llm_request_event("smart", [], None, world_id=evil, turn_num=1))
    assert store.load_turn(evil, 1)
    assert not (store._root.parent / "escape" / "turn-000001.jsonl").exists()


async def test_empty_world_id_goes_to_unknown_dir(store):
    # 状态：空 world_id（如历史 bug 的开场叙事）归 _unknown 占位目录，
    # 绝不散落 traces 根目录（根目录文件前端扫不到会丢）
    store.append(make_llm_request_event("smart", [], None, world_id="", turn_num=0))
    assert (store._root / "_unknown" / "turn-000000.jsonl").exists()
    assert not (store._root / "turn-000000.jsonl").exists()
    assert "_unknown" in store.list_world_ids()
    assert store.load_turn("", 0)


def test_load_missing_turn_returns_empty(store):
    assert store.load_turn("nope", 99) == []
    assert store.list_turns("nope") == []
    assert store.count_turns("nope") == 0


# ====================================================================
# TraceBus.publish 自动落盘（生产端经 publish 即持久化）
# ====================================================================


async def test_publish_persists_to_store(tmp_path):
    from src.webui import trace_store as trace_store_module
    from src.webui import trace_engine as trace_engine_module

    old_dir = trace_store_module.TRACE_DIR
    trace_store_module.TRACE_DIR = tmp_path / "traces"
    trace_store_module._trace_store = None
    try:
        bus = get_trace_bus()
        await bus.publish(make_llm_request_event(
            "smart", [{"role": "user", "content": "p"}], None,
            world_id="pw", turn_num=7,
        ))
        stored = get_trace_store().load_turn("pw", 7)
        assert len(stored) == 1
        assert stored[0].data["messages"][0]["content"] == "p"
    finally:
        trace_store_module.TRACE_DIR = old_dir
        trace_store_module._trace_store = None
