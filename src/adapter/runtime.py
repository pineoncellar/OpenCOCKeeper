# -*- coding: utf-8 -*-
"""
@File     :   runtime.py
@Desc     :   App 运行时编排 — 前置自检 / 后台固化 Worker 生命周期 / 适配器挂接
@Note     :   run_cli() 为进程入口：preflight FAIL 即中止（可配置跳过），随后构建
             Storage + Memory + ConsolidationWorker 并挂后台，再按 config.adapter.active
             分派适配器（create_adapter 工厂）跑主循环；退出时先由 adapter 清理 stop
             worker，此处兜底 cancel 后台任务
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from src.core.config import get_settings
from src.core.log import get_logger
from src.memory.interface import Memory
from src.memory.preflight import PreflightReport, preflight
from src.memory.worker import ConsolidationWorker
from src.storage.storage import Storage

logger = get_logger(__name__)


# ============================================
# 适配器工厂
# ============================================  


def create_adapter(
    storage: Any,
    memory: Any,
    worker: Any,
    *,
    llm: Any = None,
    active: Optional[str] = None,
) -> Any:
    """按适配器类型分派实例；active 缺省取 config.adapter.active（当前仅 cli）。

    未来新增 OneBot / Web 适配器时，在此注册类型名 -> 适配器类的映射即可，
    上层 run_cli 无需感知具体类。
    """
    active = (active or str(get_settings().get("adapter.active", "cli"))).lower()
    if active == "cli":
        from .cli import CliAdapter

        session_id = str(
            get_settings().get("adapter.cli.session_id", "cli-default")
        )
        return CliAdapter(
            storage=storage, memory=memory, worker=worker,
            llm=llm, session_id=session_id,
        )
    raise ValueError(f"未知适配器类型 '{active}'，当前支持: cli")


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
    # 状态：WebUI 后台服务 — 与主适配器同事件循环共存，共享 TraceBus 与存储门面
    webui_started = False
    if get_settings().get("webui.enabled", True):
        from src.webui.server import start_background as _start_webui
        webui_started = await _start_webui(
            storage=storage, memory=memory, worker=worker,
        )
    adapter = create_adapter(storage=storage, memory=memory, worker=worker)
    try:
        await adapter.run()  # 状态：adapter._cleanup 已优雅 stop worker
    finally:
        # 状态：先停 WebUI，再停 worker，最后释放 Memory 后端
        if webui_started:
            from src.webui.server import stop_background as _stop_webui
            await _stop_webui()
        if not worker_task.done():  # 状态：兜底取消，防异常路径遗留后台任务
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        # 状态：显式关闭记忆后端——否则 mem0/qdrant 残留非守护线程，
        # 进程退出挂起等待 + QdrantClient.__del__ 抛 ImportError 噪音
        try:
            await memory.close()
        except Exception as e:  # noqa: BLE001  关闭失败不影响退出码
            logger.debug(f"Memory 后端关闭失败: {e}")
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
