# -*- coding: utf-8 -*-
"""
@File     :   runtime.py
@Desc     :   App 运行时编排 — 前置自检 / 后台固化 Worker 生命周期 / CLI 适配器挂接
@Note     :   run_cli() 为 CLI 入口：preflight FAIL 即中止（可配置跳过），随后构建
             Storage + Memory + ConsolidationWorker 并挂后台，再跑 CliAdapter 主循环；
             退出时先由 adapter 清理 stop worker，此处兜底 cancel 后台任务
"""

from __future__ import annotations

import asyncio
from typing import Optional

from src.core.config import get_settings
from src.core.log import get_logger
from src.memory.interface import Memory
from src.memory.preflight import PreflightReport, preflight
from src.memory.worker import ConsolidationWorker
from src.storage.storage import Storage

from .cli import CliAdapter

logger = get_logger(__name__)


# ============================================
# 运行时编排
# ============================================


async def run_cli(*, storage: Optional[Storage] = None, memory=None, worker=None) -> int:
    """CLI 主入口编排：preflight -> 构建运行时 -> 跑 REPL -> 清理，返回进程退出码。

    storage / memory / worker 可注入（测试可传 fake），缺省自动构建真实运行时；
    preflight 失败返回 1 中止，除非 config.adapter.preflight 关闭。
    """
    settings = get_settings()

    if settings.get("adapter.preflight", True):
        report = await preflight(
            check_embedding=bool(settings.get("adapter.check_embedding", True))
        )
        _print_preflight(report)
        if not report.all_ok:
            logger.error("前置自检未通过，中止启动")
            return 1  # 状态：FAIL 即中止，避免带病运行

    storage = storage or Storage()
    memory = memory or Memory()
    worker = worker or ConsolidationWorker(memory=memory, storage=storage)

    worker_task = asyncio.create_task(worker.start(), name="consolidation-worker")
    adapter = CliAdapter(storage=storage, memory=memory, worker=worker)
    try:
        await adapter.run()  # 状态：adapter._cleanup 已优雅 stop worker
    finally:
        if not worker_task.done():  # 状态：兜底取消，防异常路径遗留后台任务
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
    return 0


# ============================================
# 自检输出
# ============================================


def _print_preflight(report: PreflightReport) -> None:
    """将自检报告打印到终端：逐项状态 + 失败项详情。"""
    print()
    print("== RAG 前置自检 ==")
    for c in report.checks:
        mark = "OK " if c.status == "ok" else "FAIL"
        print(f"  [{mark}] {c.name}: {c.detail}")
    if report.failed:
        print(f"  共 {len(report.failed)} 项未通过")
    print()


# ============================================
# 独立入口
# ============================================


def main() -> None:
    """进程入口：包装 asyncio.run，捕获 Ctrl-C。"""
    try:
        raise SystemExit(asyncio.run(run_cli()))
    except KeyboardInterrupt:
        print("\n再见！")


if __name__ == "__main__":
    main()
