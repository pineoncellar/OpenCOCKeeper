# -*- coding: utf-8 -*-
"""
@File     :   worker.py
@Desc     :   后台固化 Worker — 定时轮询 + 事件触发双通道，把未固化轮次批量交给 Memory.consolidate
@Note     :   生命周期骨架移植自 glyphkeeper/src/workers/memorizer_worker.py；固化逻辑复用本仓库
             Memory.consolidate（无降级、副作用后置、可整批重试）；阈值用「未固化轮数 + 距上次
             固化时间」双通道判定，force 参数为场景切换等强信号预留；同一世界内串行、跨世界并发
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..core.config import get_settings
from ..core.exceptions import WorldNotFoundError
from ..core.log import get_logger
from ..storage.storage import Storage

logger = get_logger(__name__)


# ============================================
# 后台固化调度壳
# ============================================


class ConsolidationWorker:
    """管理一批世界的增量固化调度。

    生命周期:
      start()        启动后台轮询循环（由 asyncio.create_task 挂接）
      stop()         优雅停止，等待后台任务退出
      trigger_now()  唤醒一次全量扫描（手动 / 定时兜底之外的信号通道）
      trigger_world() 事件驱动单世界固化（响应落库后由 pipeline 钩子调用）

    阈值判定:
      未固化轮数达到 min_turns，或距上次固化超过 min_interval，任一满足即固化；
      force=True 无视阈值强制固化，供场景切换等强信号预留。

    并发与失败:
      同一世界内固化严格串行（per-world 锁），不同世界之间并发；
      consolidate 失败不标记进度，副作用后置保证下个周期整批重试，绝不丢轮。
    """

    def __init__(
        self,
        memory,
        storage: Optional[Storage] = None,
        *,
        poll_interval: Optional[float] = None,
        min_turns: Optional[int] = None,
        min_interval: Optional[float] = None,
        batch_limit: Optional[int] = None,
        worlds: Optional[List[str]] = None,
    ) -> None:
        """memory 为 Memory 门面（真实或 FakeMemory）；worlds=None 时扫描全部活跃世界。"""
        from ..core.db import get_db

        self._memory = memory
        self._storage = storage or Storage(db=get_db())
        settings = get_settings()
        self.poll_interval = float(
            poll_interval
            if poll_interval is not None
            else settings.get("memory.solidify_poll_interval", 60)
        )
        self.min_turns = int(
            min_turns
            if min_turns is not None
            else settings.get("memory.solidify_min_turns", 5)
        )
        self.min_interval = float(
            min_interval
            if min_interval is not None
            else settings.get("memory.solidify_min_interval", 300)
        )
        self.batch_limit = int(
            batch_limit
            if batch_limit is not None
            else settings.get("memory.solidify_batch_limit", 50)
        )
        self._worlds: Optional[List[str]] = worlds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._trigger: asyncio.Event = asyncio.Event()
        self._locks: Dict[str, asyncio.Lock] = {}
        # 每个世界距上次固化的墙钟时间，供时间兜底阈值使用
        self._last_consolidated: Dict[str, float] = {}
        self._consolidated_count = 0
        self._last_consolidated_at: Optional[str] = None
        # 最近一次各世界固化结果，供 stats 与日志查看
        self._last_results: Dict[str, Any] = {}

    # ============================================
    # 生命周期
    # ============================================

    async def start(self) -> None:
        """启动后台轮询循环；已在运行则直接返回。"""
        if self._running:
            logger.warning("ConsolidationWorker 已在运行")
            return
        self._running = True
        logger.info(
            f"ConsolidationWorker 启动: poll={self.poll_interval}s, "
            f"min_turns={self.min_turns}, min_interval={self.min_interval}s"
        )
        self._task = asyncio.current_task()
        try:
            while self._running:
                # 状态：等待定时或 trigger 唤醒（先等待，避免启动后立即执行一轮）
                try:
                    await asyncio.wait_for(
                        self._trigger.wait(), timeout=self.poll_interval
                    )
                    self._trigger.clear()
                except asyncio.TimeoutError:
                    pass  # 状态：超时正常，继续下一轮
                try:
                    await self._process_pending()
                except Exception as e:  # noqa: BLE001
                    logger.error(f"ConsolidationWorker 轮询异常: {e}", exc_info=True)
        finally:
            self._running = False
            logger.info("ConsolidationWorker 已停止")

    async def stop(self) -> None:
        """停止 Worker：置停止标志并唤醒等待，取消后台任务。"""
        self._running = False
        self._trigger.set()  # 状态：唤醒等待，让循环走到 finally 退出
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def trigger_now(self) -> None:
        """手动唤醒一次全量扫描（非阻塞，仅置信号）。"""
        logger.info("ConsolidationWorker: 收到手动触发信号")
        self._trigger.set()

    def trigger_world(self, world_id: str, *, force: bool = False) -> None:
        """事件驱动：响应落库后调用，安排该世界一次固化，立即返回不阻塞。

        未启动时忽略（定时轮询会兜底）；force=True 无视阈值强制固化，
        供场景切换等强信号使用；真正固化在后台任务执行，失败不抛给调用方。
        """
        if not self._running:
            logger.debug(f"ConsolidationWorker 未运行，忽略 trigger_world({world_id})")
            return
        # 状态：立即创建后台任务执行固化，避免阻塞响应管线
        task = asyncio.create_task(self._maybe_consolidate(world_id, force=force))
        task.add_done_callback(_log_task_error)

    # ============================================
    # 状态查询
    # ============================================

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict:
        """运行状态与固化统计，供上层观测 / 日志。"""
        return {
            "running": self._running,
            "poll_interval": self.poll_interval,
            "min_turns": self.min_turns,
            "min_interval": self.min_interval,
            "batch_limit": self.batch_limit,
            "consolidated_count": self._consolidated_count,
            "last_consolidated_at": self._last_consolidated_at,
            "last_results": dict(self._last_results),
        }

    def get_world_lock(self, world_id: str) -> asyncio.Lock:
        """返回该世界的异步锁（与后台固化共享），供回档等外部动作互斥使用。

        同一世界内固化与回档必须用同一把锁，否则会出现「固化写入中回档删除」
        或「回档后固化又把旧轮次写回」的竞态；本方法保证外部拿到同一注册表锁。
        """
        return self._locks.setdefault(world_id, asyncio.Lock())

    # ============================================
    # 核心处理
    # ============================================

    async def _process_pending(self) -> None:
        """扫描目标世界，对每个世界按阈值决定是否固化。

        缺省只扫描 status=ACTIVE 的世界——归档（已结团）世界静默跳过，
        不再触发普通轮询固化，避免对只读世界做无效空扫。
        """
        if self._worlds is not None:
            world_ids = list(self._worlds)
        else:
            world_ids = [
                w["world_id"]
                for w in self._storage.list_worlds(status="ACTIVE")
            ]
        if not world_ids:
            return  # 状态：无活跃世界，跳过本轮
        # 状态：不同世界并发固化，同一世界内部由 per-world 锁串行
        await asyncio.gather(
            *(self._maybe_consolidate(wid) for wid in world_ids),
            return_exceptions=True,
        )

    async def _maybe_consolidate(
        self, world_id: str, *, force: bool = False
    ) -> None:
        """对单个世界做阈值判定并固化；per-world 锁保证并发安全。"""
        lock = self._locks.setdefault(world_id, asyncio.Lock())
        async with lock:  # 状态：同一世界固化互斥，防事件与轮询并发重复执行
            try:
                if not self._should_consolidate(world_id, force=force):
                    return
                result = await self._memory.consolidate(world_id)
                now = time.time()
                self._last_consolidated[world_id] = now
                self._last_consolidated_at = _fmt_ts(now)
                self._last_results[world_id] = result.brief()
                if result.ok:
                    self._consolidated_count += 1
                    logger.info(
                        f"固化完成 world={world_id} "
                        f"turns={result.turns_solidified} "
                        f"events={result.events_written} recap={result.recap_updated}"
                    )
            except Exception as e:  # noqa: BLE001
                # 状态：失败不标记进度——consolidate 副作用后置，下个周期整批重试
                logger.error(f"固化失败 world={world_id}: {e}", exc_info=True)

    def _should_consolidate(
        self, world_id: str, *, force: bool = False
    ) -> bool:
        """阈值判定：force 直接通过；否则未固化轮数达标或距上次固化超时，任一即固化。

        首次（从未固化过）不因时间触发，避免刚启动空转，等轮数自然累积即可。
        """
        if force:
            return True
        pending = self._storage.get_unsolidified_turns(
            world_id, up_to_turn=self.batch_limit
        )
        if not pending:
            return False  # 状态：无增量，天然幂等，重复触发直接跳过
        if len(pending) >= self.min_turns:
            return True  # 状态：轮数阈值达标
        last = self._last_consolidated.get(world_id)
        if last is not None and (time.time() - last) >= self.min_interval:
            return True  # 状态：时间兜底——轮数不足但久未固化
        return False


# ============================================
# 辅助
# ============================================


def _fmt_ts(ts: float) -> str:
    """时间戳格式化为本地 ISO 字符串，供 stats 展示。"""
    return datetime.fromtimestamp(ts).isoformat()


def _log_task_error(task: asyncio.Task) -> None:
    """后台固化任务完成回调：记录未捕获异常，避免任务静默失败。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(f"ConsolidationWorker 后台固化任务异常: {exc}", exc_info=exc)


# ============================================
# 回档编排
# ============================================


async def rollback_world(
    storage: Storage,
    memory,
    world_id: str,
    turn_num: int,
    *,
    worker: Optional[ConsolidationWorker] = None,
) -> int:
    """回档到第 turn_num 轮之前：先撤销 SQLite 物理状态，再擦除 RAG 语义记忆。

    顺序约定（先物理后语义）：SQLite 撤销失败抛异常中断，避免物理状态错乱；
    Qdrant 删除幂等可重试。传入 worker 时与其共享该世界异步锁，防止后台固化
    与回档并发；不传则免锁（单测直连）。目标轮次已被裁剪时跳过 SQLite 段、
    仅补 RAG 清理，保证半完成态重试安全。返回 RAG 侧删除的记忆条数。
    """
    if worker is not None:
        async with worker.get_world_lock(world_id):  # 状态：与后台固化互斥
            return await _rollback_unlocked(storage, memory, world_id, turn_num)
    return await _rollback_unlocked(storage, memory, world_id, turn_num)


async def _rollback_unlocked(storage: Storage, memory, world_id: str, turn_num: int) -> int:
    """无锁回档主体：SQLite 目标轮次存在才撤销物理状态，随后必清 RAG。"""
    # 状态：目标轮次已删/已裁剪则视为已撤销，跳过物理段、仅补 RAG（幂等重试安全）
    if storage.get_turn(world_id, turn_num) is not None:
        undone = storage.undo_from(world_id, turn_num)
        logger.info(
            "SQLite 回档 world=%s 撤销轮次=%s",
            world_id, [t["turn_num"] for t in undone],
        )
    else:
        logger.info(
            "SQLite 目标轮次已不存在，跳过物理撤销（仅清理 RAG）world=%s turn=%s",
            world_id, turn_num,
        )
    deleted = await memory.undo(world_id, turn_num)
    logger.info("RAG 回档 world=%s turn>=%s 删除=%s", world_id, turn_num, deleted)
    return deleted


# ============================================
# 世界删除编排
# ============================================


async def delete_world(
    storage: Storage,
    memory,
    world_id: str,
    *,
    worker: Optional[ConsolidationWorker] = None,
) -> int:
    """删除世界：先清空 RAG 语义记忆，再删除 SQLite 世界行（外键级联实体/轮次/历史）。

    顺序约定（先语义后物理）：Memory.undo 前置要求世界仍存在（_require_world 校验），
    故先以 turn>=0 全量清空该世界 RAG 记忆，再删 SQLite；SQLite 删除失败抛异常中断，
    RAG 删除幂等可重试。传入 worker 时与其共享该世界异步锁，防止后台固化并发写入
    孤儿记忆。返回 RAG 侧删除的记忆条数。
    """
    if worker is not None:
        async with worker.get_world_lock(world_id):  # 状态：与后台固化互斥
            return await _delete_world_unlocked(storage, memory, world_id)
    return await _delete_world_unlocked(storage, memory, world_id)


async def _delete_world_unlocked(storage: Storage, memory, world_id: str) -> int:
    """无锁删除主体：RAG 全清 -> SQLite 删除。"""
    deleted = 0
    if memory is not None:
        deleted = await memory.undo(world_id, 0)  # 状态：turn>=0 全量清空该世界语义记忆
        logger.info("RAG 清空 world=%s 删除=%s", world_id, deleted)
    removed = storage.delete_world(world_id)
    if not removed:
        raise WorldNotFoundError(f"世界不存在: {world_id}")
    logger.info("SQLite 删除世界 world=%s", world_id)
    return deleted
