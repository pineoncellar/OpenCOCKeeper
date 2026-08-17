# -*- coding: utf-8 -*-
"""
@File     :   world_save.py
@Desc     :   世界全量存档/恢复——SQLite 四表 + RAG 记忆（含向量）+ trace 目录
@Note     :   存档目录 data/backups/<原world_id>/<存档名>/：
               meta.json（元信息）/ world.json（四表原始行）/ rag.json（记忆点 id+向量+payload）
              / trace/（logs/traces/<world_id>/ 的拷贝）。
              恢复允许改名（世界名任意），目标世界已存在则先删除覆盖（走 worker.delete_world
              编排保证 SQLite/RAG/trace 一并清理）；存档名仅字母/数字/下划线/连字符/中文。
              供适配器 /world save|load -save 命令与 WebUI 复用。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.config import PROJECT_ROOT, get_settings
from src.core.log import get_logger

logger = get_logger(__name__)

# 存档名：字母/数字/下划线/连字符/中文，长度 1~32
_SAVE_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff-]{1,32}$")


# ============================================
# 路径与校验
# ============================================


def _backup_root() -> Path:
    """存档根目录（config.storage.backups_dir，相对项目根则拼接）。"""
    raw = str(get_settings().get("storage.backups_dir", "data/backups"))
    p = Path(raw)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def _sanitize(name: str) -> str:
    """清洗为安全目录名（防路径穿越）。"""
    return (name or "").replace("/", "_").replace("\\", "_").replace("..", "_")


def _save_dir(world_id: str, save_name: str) -> Path:
    return _backup_root() / _sanitize(world_id) / save_name


def _trace_dir(world_id: str) -> Path:
    """该世界 trace 目录（动态读 TRACE_DIR，测试 monkeypatch 生效）。"""
    from src.webui.trace_store import TRACE_DIR, _safe_world_id

    return TRACE_DIR / _safe_world_id(world_id)


def validate_save_name(name: str) -> Optional[str]:
    """校验存档名合法性；合法返回 None，否则返回错误文案。"""
    if not name or not _SAVE_NAME_RE.match(name):
        return "存档名仅允许字母/数字/下划线/连字符/中文，长度 1~32。"
    return None


def _find_save(save_name: str) -> Optional[Path]:
    """按存档名跨世界定位存档目录（含 meta.json），找不到返回 None。"""
    root = _backup_root()
    if not root.exists():
        return None
    for world_dir in root.iterdir():
        if not world_dir.is_dir():
            continue
        p = world_dir / save_name
        if p.is_dir() and (p / "meta.json").exists():
            return p
    return None


# ============================================
# 备份
# ============================================


def save_world(storage, memory, world_id: str, save_name: str) -> Dict[str, Any]:
    """全量备份世界到 data/backups/<world_id>/<save_name>/，返回元信息。

    导出 SQLite 四表（world.json）+ RAG 记忆含向量（rag.json）+ trace 目录
    （trace/）+ 元信息（meta.json）。存档名已存在或非法抛 ValueError。
    """
    err = validate_save_name(save_name)
    if err:
        raise ValueError(err)
    world_data = storage.export_world(world_id)
    if world_data is None:
        raise ValueError(f"世界不存在: {world_id}")
    rag = memory.export_rag(world_id) if memory is not None else []
    dest = _save_dir(world_id, save_name)
    if dest.exists():
        raise ValueError(f"存档已存在: {world_id}/{save_name}（请用不同存档名）")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "world.json").write_text(
        json.dumps(world_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest / "rag.json").write_text(
        json.dumps(rag, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # trace：拷贝 logs/traces/<safe_world_id>/ 目录（不存在则跳过）
    trace_src = _trace_dir(world_id)
    if trace_src.exists():
        shutil.copytree(trace_src, dest / "trace", dirs_exist_ok=True)
    meta = {
        "save_name": save_name,
        "world_id": world_id,
        "module_name": world_data["world"].get("module_name", ""),
        "status": world_data["world"].get("status", "ACTIVE"),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {
            "entities": len(world_data["entities"]),
            "turns": len(world_data["turns"]),
            "history": len(world_data["history"]),
            "rag": len(rag),
        },
    }
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("世界存档 world=%s save=%s counts=%s", world_id, save_name, meta["counts"])
    return meta


# ============================================
# 存档列表
# ============================================


def list_saves() -> List[Dict[str, Any]]:
    """扫描 data/backups/*/ 下全部存档（含 meta），按创建时间倒序返回。"""
    root = _backup_root()
    if not root.exists():
        return []
    saves: List[Dict[str, Any]] = []
    for world_dir in sorted(root.iterdir()):
        if not world_dir.is_dir():
            continue
        for save_dir in sorted(world_dir.iterdir()):
            meta_path = save_dir / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001  坏 meta 跳过
                continue
            meta["save_dir"] = str(save_dir)
            saves.append(meta)
    saves.sort(key=lambda m: str(m.get("created_at", "")), reverse=True)
    return saves


# ============================================
# 恢复
# ============================================


async def restore_world(storage, memory, worker, world_id: str, save_name: str) -> Dict[str, Any]:
    """从存档恢复到目标世界 world_id（允许改名；目标已存在则先删除覆盖）。

    顺序：目标已存在 -> delete_world 编排清 SQLite+RAG+trace；storage.import_world
    重建四表（world_id 改写）；memory.import_rag 重建记忆（隔离键改写）；
    trace 目录拷回。存档不存在抛 FileNotFoundError。
    """
    src = _find_save(save_name)
    if src is None:
        raise FileNotFoundError(f"存档不存在: {save_name}")
    meta = json.loads((src / "meta.json").read_text(encoding="utf-8"))
    world_data = json.loads((src / "world.json").read_text(encoding="utf-8"))
    rag_path = src / "rag.json"
    rag = json.loads(rag_path.read_text(encoding="utf-8")) if rag_path.exists() else []
    # 目标已存在则先删除覆盖（复用世界删除编排，连带清理 SQLite/RAG/trace）
    if storage.get_world(world_id) is not None:
        from src.memory.worker import delete_world

        await delete_world(storage, memory, world_id, worker=worker)
    storage.import_world(world_data, world_id)
    rag_restored = memory.import_rag(rag, world_id) if memory is not None else 0
    # 恢复 trace：拷贝存档 trace/ 回 logs/traces/<safe_world_id>/
    trace_src = src / "trace"
    if trace_src.exists():
        trace_dest = _trace_dir(world_id)
        trace_dest.mkdir(parents=True, exist_ok=True)
        for item in trace_src.iterdir():
            if item.is_file():
                shutil.copy2(item, trace_dest / item.name)
    logger.info("世界恢复 world=%s save=%s rag=%d", world_id, save_name, rag_restored)
    return {**meta, "rag_restored": rag_restored}
