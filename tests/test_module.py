# -*- coding: utf-8 -*-
"""
@File     :   test_module.py
@Desc     :   模组文件层测试：目录/列表/安全解析/原文读取 + 世界初始化强制绑定
@Note     :   依赖 conftest 的 _tmp_modules autouse 夹具（临时模组目录 + test_module.docx）；
             所有用例走 tmp 路径，绝不触碰真实 data/modules
"""

from __future__ import annotations

import pytest

from src.core.exceptions import ModuleFileMissingError, UnsupportedFormatError
from src.core.ids import make_world_id
from src.module import ensure_modules_dir, list_modules, read_module, resolve

# conftest 注入的测试模组文件名（临时目录内真实存在）
TEST_MODULE = "test_module.docx"


# ====================================================================
# 文件层：目录 / 列表 / 解析
# ====================================================================


def test_ensure_modules_dir_creates(tmp_path, monkeypatch):
    from src.module import loader as module_loader
    d = tmp_path / "custom_modules"
    monkeypatch.setattr(module_loader, "MODULES_DIR", d)
    assert ensure_modules_dir() == d
    assert d.is_dir()


def test_list_modules_filters_hidden_and_unsupported(tmp_path, monkeypatch):
    from src.module import loader as module_loader
    d = tmp_path / "mods"
    d.mkdir()
    (d / "a.docx").write_bytes(b"a")
    (d / "b.txt").write_text("b", encoding="utf-8")
    (d / ".hidden.docx").write_bytes(b"h")
    monkeypatch.setattr(module_loader, "MODULES_DIR", d)
    names = [m.module_name for m in list_modules()]
    assert names == ["a.docx"]


def test_resolve_rejects_traversal(tmp_path, monkeypatch):
    from src.module import loader as module_loader
    d = tmp_path / "mods"
    d.mkdir()
    monkeypatch.setattr(module_loader, "MODULES_DIR", d)
    with pytest.raises(ModuleFileMissingError):
        resolve("../outside.docx")


def test_resolve_rejects_hidden():
    with pytest.raises(ModuleFileMissingError):
        resolve(".secret.docx")


def test_resolve_rejects_empty():
    with pytest.raises(ModuleFileMissingError):
        resolve("")


def test_resolve_rejects_unsupported_ext():
    with pytest.raises(UnsupportedFormatError):
        resolve("foo.epub")


def test_resolve_ok(_tmp_modules):
    p = resolve(TEST_MODULE)
    assert p.name == TEST_MODULE
    assert p.is_file()


def test_read_module_returns_original_text(_tmp_modules):
    text = read_module(TEST_MODULE)
    assert "测试模组" in text


def test_read_module_missing():
    with pytest.raises(ModuleFileMissingError):
        read_module("nope.docx")


# ====================================================================
# 世界初始化：强制绑定模组文件
# ====================================================================


def test_ensure_world_requires_module(storage):
    with pytest.raises(ModuleFileMissingError):
        storage.ensure_world(make_world_id(900, "nomod"))


def test_ensure_world_binds_module(storage):
    wid = make_world_id(900, "bound")
    world = storage.ensure_world(wid, module_name=TEST_MODULE)
    assert world["module_name"] == TEST_MODULE
    assert storage.get_world(wid)["module_name"] == TEST_MODULE


def test_ensure_world_rejects_missing_file(storage):
    wid = make_world_id(900, "missing")
    with pytest.raises(ModuleFileMissingError):
        storage.ensure_world(wid, module_name="nope.docx")
    assert storage.get_world(wid) is None  # 校验失败，世界未创建（原子性）


def test_update_world_change_module(storage, world_id):
    storage.update_world(world_id, module_name=TEST_MODULE)
    assert storage.get_world(world_id)["module_name"] == TEST_MODULE
    storage.update_world(world_id, module_name="")  # 传空串解绑
    assert storage.get_world(world_id)["module_name"] == ""


def test_update_world_rejects_missing_file(storage, world_id):
    with pytest.raises(ModuleFileMissingError):
        storage.update_world(world_id, module_name="nope.docx")


def test_multiple_worlds_share_module(storage):
    a = storage.ensure_world(make_world_id(900, "share_a"), module_name=TEST_MODULE)
    b = storage.ensure_world(make_world_id(900, "share_b"), module_name=TEST_MODULE)
    assert a["module_name"] == b["module_name"] == TEST_MODULE
