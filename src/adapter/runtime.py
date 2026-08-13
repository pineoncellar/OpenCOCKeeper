# -*- coding: utf-8 -*-
"""
@File     :   runtime.py
@Desc     :   App 运行时编排 — 前置自检 / 后台固化 Worker 生命周期 / WebUI 调试窗口
@Note     :   run_app() 为进程入口：preflight FAIL 即中止（可配置跳过），随后构建
             Storage + Memory + ConsolidationWorker 并挂后台，再启动 WebUI 服务
             （唯一交互界面，Web 适配器由 /ws/game 按连接构造）；终端不接收任何
             输入，仅由 logging 输出日志
"""

from __future__ import annotations

import asyncio
import signal
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
    """按适配器类型分派实例；active 缺省取 config.adapter.active（当前仅 web）。

    Web 适配器由 WebUI /ws/game 按连接构造（连接级实例），此工厂供测试与
    未来独立后台任务（如 OneBot）复用；新增类型在此注册映射即可。
    """
    active = (active or str(get_settings().get("adapter.active", "web"))).lower()
    if active == "web":
        from .web.adapter import WebAdapter

        session_id = str(
            get_settings().get("adapter.web.session_id", "web-default")
        )
        return WebAdapter(
            storage=storage, memory=memory, worker=worker,
            llm=llm, session_id=session_id,
        )
    raise ValueError(f"未知适配器类型 '{active}'，当前支持: web")


# ============================================
# 运行时编排
# ============================================


async def run_app(*, storage: Optional[Storage] = None, memory=None, worker=None) -> int:
    """进程主入口：preflight -> 构建运行时 -> 后台 Worker + WebUI 常驻 -> 退出清理。

    终端不再接收任何输入（CLI REPL 已移除）：交互一律走 WebUI 调试窗口，
    终端仅由 logging 输出日志；退出靠 Ctrl-C / Ctrl-Break 置退出事件，
    随后优雅停止 WebUI / Worker / Memory，返回进程退出码。
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

    # 状态：后台固化 Worker 常驻（记忆固化不阻塞玩家响应）
    worker_task = asyncio.create_task(worker.start(), name="consolidation-worker")
    # 状态：WebUI 调试窗口 — 唯一交互界面，共享 TraceBus 与存储门面
    webui_started = False
    if get_settings().get("webui.enabled", True):
        from src.webui.server import start_background as _start_webui
        webui_started = await _start_webui(
            storage=storage, memory=memory, worker=worker,
        )

    # 状态：终端不接收输入——注册退出信号常驻等待。
    # Windows 上 add_signal_handler 不可用（抛 NotImplementedError），Ctrl-C 会走
    # asyncio.run 的取消路径，由下方 except CancelledError 兜底统一清理；
    # Linux 上 SIGINT/SIGTERM 信号处理器触发 stop.set 优雅停机。
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # 状态：平台不支持该信号处理器时跳过，取消路径/KeyboardInterrupt 兜底
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        # 状态：Ctrl-C / 外部取消——统一进入清理；CancelledError 已被消费，
        # 按 Python 3.11 取消计数语义，finally 内的 await 可正常执行
        pass
    finally:
        # 状态：先停 WebUI，再停 worker，最后释放 Memory 后端
        if webui_started:
            from src.webui.server import stop_background as _stop_webui
            try:
                await _stop_webui()
            except asyncio.CancelledError:
                pass  # 状态：取消传播中，跳过（进程将退出）
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
        except BaseException as e:  # noqa: BLE001  关闭失败/取消均不影响退出码
            logger.debug(f"Memory 后端关闭失败: {e}")
    return 0


# ============================================
# 自检输出
# ============================================


def _print_preflight(report: PreflightReport) -> None:
    """将自检报告打印到终端（属日志输出）：逐项状态 + 失败项详情。

    状态标记与 preflight 的 STATUS_OK/STATUS_FAIL/STATUS_SKIP 大写常量对齐，
    否则通过项会被误显示为 FAIL（曾经的大小写不匹配显示 bug）。
    """
    print()
    print("== RAG 前置自检 ==")
    for c in report.checks:
        mark = {"OK": "OK ", "FAIL": "FAIL", "SKIP": "SKIP"}.get(c.status, "?   ")
        print(f"  [{mark}] {c.name}: {c.detail}")
    if report.failed:
        print(f"  共 {len(report.failed)} 项未通过")
    print()


# ============================================
# 独立入口
# ============================================


def main() -> None:
    """进程入口：包装 asyncio.run，Ctrl-C 兜底（信号处理器注册失败时的最后防线）。"""
    try:
        raise SystemExit(asyncio.run(run_app()))
    except KeyboardInterrupt:
        print("\n再见！")


if __name__ == "__main__":
    main()
