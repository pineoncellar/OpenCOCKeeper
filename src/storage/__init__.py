# -*- coding: utf-8 -*-
"""
@File     :   storage/__init__.py
@Desc     :   存储层对外入口：Storage 门面 + diff 工具 + 建表迁移
@Note     :   红线：存储层只认 world_id，窗口/会话 id 仅限适配器层
"""

from .diff import (
    empty_diff,
    is_empty,
    merge_diff,
    negate_diff,
    record_inventory_change,
    record_numeric_change,
    record_tag_change,
)
from .schema import MIGRATIONS
from .storage import Storage, get_storage

__all__ = [
    "Storage",
    "get_storage",
    "MIGRATIONS",
    "empty_diff",
    "is_empty",
    "merge_diff",
    "negate_diff",
    "record_numeric_change",
    "record_tag_change",
    "record_inventory_change",
]
