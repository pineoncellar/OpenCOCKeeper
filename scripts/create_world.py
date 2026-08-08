# -*- coding: utf-8 -*-
"""
@File     :   create_world.py
@Desc     :   世界初始化 CLI：绑定 data/modules 下的模组文件创建新世界
@Note     :   --module 必填（强制绑定模组，文件须已放入 data/modules）；
             --list 仅展示可用模组不创建；世界 id 缺省按时间戳自动生成
"""

from __future__ import annotations

import argparse
import time

from _common import fail, ok, section

from src.core.db import get_db
from src.core.exceptions import OpenCOCKeeperError
from src.core.ids import make_world_id
from src.module import ensure_modules_dir, list_modules
from src.storage.storage import Storage


# ============================================
# 主流程
# ============================================


def main() -> int:
    parser = argparse.ArgumentParser(description="创建绑定模组的跑团世界")
    parser.add_argument("--module", default=None,
                        help="模组文件名（必填，须已放入 data/modules）")
    parser.add_argument("--list", action="store_true",
                        help="仅列出可用模组，不创建世界")
    parser.add_argument("--world-id", default=None,
                        help="固定世界 id（缺省按时间戳自动生成）")
    parser.add_argument("--player", action="append", default=None,
                        help="调查员 id（可多次指定）")
    args = parser.parse_args()

    ensure_modules_dir()
    modules = list_modules()

    if args.list:
        if not modules:
            fail("data/modules 下暂无可用模组，请放入 PDF / Word 模组文件后重试")
            return 0
        section(f"可用模组（{len(modules)} 个）")
        for m in modules:
            ok(f"{m.module_name}  [{m.format}]  {m.size} 字节")
        return 0

    if not args.module:
        fail("必须指定 --module 绑定模组文件；可用模组请先执行 --list")
        return 1

    try:
        storage = Storage(db=get_db())
        wid = args.world_id or make_world_id(int(time.time()) % 1000, _slug(args.module))
        world = storage.ensure_world(
            wid, module_name=args.module, player_ids=args.player or [],
        )
        section("世界已创建")
        ok(f"world_id: {wid}")
        ok(f"绑定模组: {world['module_name']}")
        return 0
    except OpenCOCKeeperError as e:
        fail(f"创建失败: {e}")
        return 1


def _slug(module_name: str) -> str:
    """模组文件名去扩展名并下划线化，作为世界 id 的 slug 段。"""
    return module_name.rsplit(".", 1)[0].lower().replace(" ", "_").replace("-", "_")


if __name__ == "__main__":
    raise SystemExit(main())
