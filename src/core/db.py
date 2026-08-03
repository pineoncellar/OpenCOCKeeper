# -*- coding: utf-8 -*-
"""
@File     :   db.py
@Desc     :   SQLite 连接与生命周期管理：连接工厂、事务上下文、轻量迁移
@Note     :   单库多世界：全局 data/app.db，业务层通过 world_id 隔离；
             每次操作新建连接（WAL 并发安全），写事务用 BEGIN IMMEDIATE 降低写锁冲突
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence, Union

from .config import PROJECT_ROOT, get_settings
from .exceptions import StorageError

# 默认数据库路径（config.storage.db_path 优先，缺省走这里）
DEFAULT_DB_PATH = "data/app.db"

# 一条迁移：要么是 callable(conn)，要么是一组单条 SQL 语句（避免 executescript 隐式提交的坑）
Migration = Union[Callable[[sqlite3.Connection], None], Sequence[str]]


# ============================================
# Database 类：连接与生命周期
# ============================================


class Database:
    """SQLite 数据库封装：负责建目录、开连接、事务提交与版本迁移。

    约定：所有存储操作显式传 world_id，本类不感知业务表结构，
    表结构由存储层通过 migrate() 注册。
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        *,
        timeout: float = 5.0,
        check_same_thread: bool = False,
    ) -> None:
        # 先确保父目录存在，避免首次建库报"无法打开数据库文件"  # 状态：初始化
        self._path = Path(db_path) if db_path else self._default_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._timeout = timeout
        # 允许连接跨线程使用，兼容异步适配层在多线程池中各自建立连接
        self._check_same_thread = check_same_thread

    @staticmethod
    def _default_path() -> Path:
        try:
            configured = get_settings().get("storage.db_path", DEFAULT_DB_PATH)
        except Exception:  # noqa: BLE001  配置未就绪时回退默认路径
            configured = DEFAULT_DB_PATH
        return PROJECT_ROOT / str(configured)

    @property
    def path(self) -> Path:
        """数据库文件绝对路径。"""
        return self._path

    def connect(self) -> sqlite3.Connection:
        """打开一个配置好的连接：Row 工厂、WAL、外键、忙等待超时。"""
        try:
            conn = sqlite3.connect(
                self._path,
                timeout=self._timeout,
                check_same_thread=self._check_same_thread,
            )
        except sqlite3.Error as e:
            raise StorageError(f"无法打开数据库 {self._path}: {e}") from e
        conn.row_factory = sqlite3.Row
        # 必须消费返回行，否则部分驱动会残留读锁  # 状态：连接配置
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
        return conn

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """原子事务上下文：成功 commit，异常 rollback，无论成败都关闭连接。

        写操作默认 BEGIN IMMEDIATE 提前拿写锁，
        这是为了减少多会话并发写时的 "database is locked" 冲突。
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """只读连接上下文：纯 SELECT 用，不开启写事务。"""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    # ============================================
    # 轻量 schema 迁移
    # ============================================

    def user_version(self) -> int:
        """当前 schema 版本（PRAGMA user_version）。"""
        conn = self.connect()
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def migrate(self, migrations: Sequence[Migration]) -> int:
        """按序执行未应用过的迁移，返回最终版本号。

        每条迁移要么是 callable(conn)，要么是一组单条 SQL 语句；
        每成功一条即推进 user_version，已应用的自动跳过，可重复调用。
        """
        current = self.user_version()
        if current > len(migrations):
            # 数据库版本高于迁移总数，说明迁移被删过或降级，拒绝继续以免破坏数据
            raise StorageError(
                f"schema 版本 {current} 超过迁移总数 {len(migrations)}，疑似迁移缺失或降级"
            )
        for index in range(current, len(migrations)):
            entry = migrations[index]
            with self.transaction() as conn:
                if callable(entry):
                    entry(conn)
                else:
                    for statement in entry:
                        conn.execute(statement)
                # user_version 不接受参数绑定，直接拼整数（无注入风险）
                conn.execute(f"PRAGMA user_version = {index + 1}")
        return len(migrations)


# ============================================
# 全局单例
# ============================================

_db: Optional[Database] = None


def get_db() -> Database:
    """获取全局数据库实例（懒加载单例，路径取 config.storage.db_path）。"""
    global _db
    if _db is None:
        _db = Database()
    return _db
