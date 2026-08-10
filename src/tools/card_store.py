# -*- coding: utf-8 -*-
"""
@File     :   card_store.py
@Desc     :   角色卡种子库：导入后先存种子（JSON 文件，不绑定任何世界），
             创建世界 / 加入世界时选择并拷贝一份到目标世界
@Note     :   种子 = 候选草稿，world_id 为空（不绑定世界），与 entities 表
             （外键约束 + 世界隔离）解耦，走独立 JSON 文件存储；
             拷贝 = 读种子 entity -> storage.create_entity 到世界 + 绑定 player_ids，
             种子文件保持不动（"拷贝一份"语义）
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.config import PROJECT_ROOT
from ..core.log import get_logger

logger = get_logger(__name__)

# 种子库存放目录：data/cards/imported
SEED_DIR = PROJECT_ROOT / "data" / "cards" / "imported"

_SEED_SUFFIX = ".json"


def _seed_dir() -> Path:
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    return SEED_DIR


def _slug(name: str) -> str:
    """从角色名生成 slug（字母/数字/中文保留，其余归一为下划线）供 seed_id 使用。"""
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", (name or "").strip(), flags=re.IGNORECASE).strip("_")
    return s[:24] or "pc"


def make_seed_id(name: str) -> str:
    """生成种子 id：card_<时间戳>_<名字slug>。"""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"card_{ts}_{_slug(name)}"


def save_seed(entity: Dict[str, Any], meta: Dict[str, Any], source: str) -> str:
    """把解析好的角色卡落为种子（JSON 文件），返回 seed_id。

    entity 为 parse_investigator_xlsx 产出的可直接入库字段；
    meta 为原始元数据（gender/age/birthplace/occupation/luck），供展示与选择。
    """
    seed_id = make_seed_id(meta.get("name") or "")
    payload = {
        "seed_id": seed_id,
        "meta": meta,
        "entity": entity,
        "source": source,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
    }
    path = _seed_dir() / f"{seed_id}{_SEED_SUFFIX}"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("种子角色已入库: %s (%s)", meta.get("name"), seed_id)
    return seed_id


def load_seed(seed_id: str) -> Dict[str, Any]:
    """读取种子（含 meta/entity）；不存在抛 FileNotFoundError。"""
    path = _seed_dir() / f"{seed_id}{_SEED_SUFFIX}"
    if not path.exists():
        raise FileNotFoundError(f"种子角色不存在: {seed_id}（/card list 查看）")
    return json.loads(path.read_text(encoding="utf-8"))


def list_seed_cards() -> List[Dict[str, Any]]:
    """列出全部种子角色（meta 摘要 + 来源 + 导入时间，按导入时间倒序）。"""
    if not SEED_DIR.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for f in sorted(SEED_DIR.glob(f"*{_SEED_SUFFIX}"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        meta = data.get("meta") or {}
        rows.append(
            {
                "seed_id": data.get("seed_id", f.stem),
                "name": meta.get("name", ""),
                "gender": meta.get("gender", ""),
                "age": meta.get("age", 0),
                "birthplace": meta.get("birthplace", ""),
                "occupation": meta.get("occupation", ""),
                "luck": meta.get("luck", 0),
                "source": data.get("source", ""),
                "imported_at": data.get("imported_at", ""),
            }
        )
    return rows


def copy_seed_to_world(
    storage,
    seed_id: str,
    world_id: str,
    *,
    entity_id: Optional[str] = None,
) -> str:
    """把种子角色拷贝一份到世界：create_entity + 绑定 player_ids。

    返回新实体 id；世界必须已存在；种子文件保持不动。
    """
    from ..core.ids import make_entity_id

    data = load_seed(seed_id)
    entity = data["entity"]
    if entity_id is None:
        # 世界内同类型实体序号递增分配，保证复合主键 (world_id, id) 唯一  # 状态：自动分配
        count = len(storage.get_entities(world_id, entity_type="PC"))
        entity_id = make_entity_id("PC", count + 1)

    storage.create_entity(
        world_id,
        entity_id,
        entity.get("entity_type", "PC"),
        entity.get("name", ""),
        occupation=entity.get("occupation", ""),
        hp=int(entity.get("hp", 0)),
        hp_max=int(entity.get("hp_max", 0)),
        mp=int(entity.get("mp", 0)),
        mp_max=int(entity.get("mp_max", 0)),
        san=int(entity.get("san", 0)),
        san_max=int(entity.get("san_max", 0)),
        attributes_and_skills=entity.get("attributes_and_skills"),
        inventory=entity.get("inventory"),
        tags=entity.get("tags"),
        background=entity.get("background"),
    )
    # 绑定 player_ids（去重追加）
    world = storage.get_world(world_id)
    pids = list(world.get("player_ids") or [])
    if entity_id not in pids:
        pids.append(entity_id)
        storage.update_world(world_id, player_ids=pids)
    logger.info("种子角色已拷贝到世界: %s -> %s/%s", seed_id, world_id, entity_id)
    return entity_id
