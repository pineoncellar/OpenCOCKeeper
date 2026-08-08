# -*- coding: utf-8 -*-
"""
@File     :   _common.py
@Desc     :   scripts 共享设施（非测试模块）：路径引导、异步入口、分级输出、world_id 生成
@Note     :   与 pytest 单测不同，scripts/* 全部走真实链路：
             真实 LLM 接口 / 真实 Mem0(qdrant) / 真实 data/app.db。
             运行方式：在项目根目录执行  .\.venv\Scripts\python.exe scripts\<脚本名>.py
             导入约定：本模块被 scripts 下脚本以 `from _common import ...` 引用，
             PROJECT_ROOT 在此插入 sys.path，保证任意 cwd 启动都能 import src.*
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# 项目根 = scripts/ 的上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def make_world_id(prefix: str) -> str:
    """生成一个带时间戳的独立 world_id，避免污染真实数据。"""
    return f"{prefix}_{int(time.time())}"


# ---- 输出工具 ----


def section(title: str) -> None:
    print(f"\n===== {title} =====")


def step(text: str) -> None:
    print(f"  · {text}")


def ok(text: str) -> None:
    print(f"  [OK ] {text}")


def warn(text: str) -> None:
    print(f"  [WARN] {text}")


def fail(text: str) -> None:
    print(f"  [FAIL] {text}")


def run_async(coro):
    """包一层 asyncio.run：Ctrl-C 与未捕获异常都收敛为友好退出码。"""
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        print("\n[已中断 Ctrl-C]")
        return 130
    except Exception as e:  # noqa: BLE001
        fail(f"链路异常终止: {type(e).__name__}: {e}")
        return 1


def add_common_args(parser) -> None:
    """给各脚本注入公共参数：--tier / --world-id / --module。"""
    parser.add_argument(
        "--tier", default="standard", help="模型档位: fast / standard / smart"
    )
    parser.add_argument(
        "--world-id", default=None, help="固定 world_id（缺省自动生成，带时间戳）"
    )
    parser.add_argument(
        "--module", default="test_module.docx",
        help="世界绑定的模组文件名（须已放入 data/modules，默认 test_module.docx）",
    )
