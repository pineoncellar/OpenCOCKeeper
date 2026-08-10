# -*- coding: utf-8 -*-
"""
@File     :   storage.py
@Desc     :   存储层门面：4 张核心表的 CRUD + 轮次回档 + 历史冷备
@Note     :   红线：所有方法强制显式 world_id，绝不接受窗口/会话 id；
             JSON 列统一经 core.json 编解码；undo 依赖 state_diff 取反后原子应用
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from ..core import json as cjson
from ..core.config import get_settings
from ..core.db import Database, get_db
from ..core.exceptions import (
    EntityNotFoundError,
    StorageError,
    TurnNotFoundError,
    WorldNotFoundError,
)
from ..core.ids import make_turn_id
from .diff import negate_diff
from .schema import MIGRATIONS

# 白名单：拒绝拼接任意列名，从根上防 SQL 注入与误改
_NUMERIC_COLUMNS = frozenset({"hp", "hp_max", "mp", "mp_max", "san", "san_max"})
_JSON_COLUMNS = frozenset({"attributes_and_skills", "inventory", "tags", "background"})
_TEXT_COLUMNS = frozenset({"type", "name"})
_WORLD_JSON_FIELDS = frozenset({"player_ids", "global_flags"})
_WORLD_TEXT_FIELDS = frozenset({"game_phase", "global_recap"})


# ============================================
# 行解码
# ============================================


def _decode_world(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["player_ids"] = cjson.loads_or(result.get("player_ids")) or []
    result["global_flags"] = cjson.loads_or(result.get("global_flags")) or {}
    return result


def _decode_entity(row: sqlite3.Row) -> dict:
    result = dict(row)
    for field in _JSON_COLUMNS:
        result[field] = cjson.loads_or(result.get(field))
    return result


def _decode_turn(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["context_data"] = cjson.loads_or(result.get("context_data"))
    result["state_diff"] = cjson.loads_or(result.get("state_diff"))
    return result


# ============================================
# Storage 门面
# ============================================


class Storage:
    """存储层统一入口，所有写操作走事务、所有方法都强制带 world_id。"""

    def __init__(
        self,
        db: Optional[Database] = None,
        *,
        auto_migrate: bool = True,
        turns_window: Optional[int] = None,
    ) -> None:
        self._db = db or get_db()
        # 近程窗口缺省取 config.memory.recent_turns_window
        self._turns_window = int(
            turns_window or get_settings().get("memory.recent_turns_window", 20)
        )
        if auto_migrate:
            self.migrate()

    @property
    def db(self) -> Database:
        """底层 Database 实例（高级用法可绕过门面直连）。"""
        return self._db

    def migrate(self) -> int:
        """应用全部未执行的建表迁移，幂等可重复调用。"""
        return self._db.migrate(MIGRATIONS)

    def _require_world(self, world_id: str) -> None:
        if self.get_world(world_id) is None:
            raise WorldNotFoundError(f"世界不存在: {world_id}，请先 ensure_world 创建")

    def _require_entity(self, world_id: str, entity_id: str) -> None:
        if self.get_entity(world_id, entity_id) is None:
            raise EntityNotFoundError(f"实体不存在: {world_id}/{entity_id}")

    # ============================================
    # 世界全局状态
    # ============================================

    def ensure_world(
        self,
        world_id: str,
        *,
        module_name: str = "",
        player_ids: Optional[List[str]] = None,
        game_phase: str = "EXPLORATION",
        global_flags: Optional[Dict[str, Any]] = None,
        global_recap: str = "",
    ) -> dict:
        """确保世界存在（不存在则创建），返回当前世界状态。

        实体/轮次均外键依赖世界，因此创建任何数据前必须先有世界行。
        强制约束：创建世界必须绑定 data/modules 下已存在的模组文件，
        缺省/空串/文件不存在一律抛 ModuleFileMissingError 且世界不创建
        （文件校验在事务前）；已存在的世界保持 INSERT OR IGNORE 幂等语义，
        换绑走 update_world。
        """
        from ..module.loader import resolve as resolve_module
        resolve_module(module_name)  # 强制必填：非空 + 白名单 + 文件存在  # 状态：绑定校验
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO world_state "
                "(world_id, player_ids, game_phase, global_flags, global_recap, module_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    world_id,
                    cjson.dumps(player_ids or []),
                    game_phase,
                    cjson.dumps(global_flags or {}),
                    global_recap,
                    module_name,
                ),
            )
        world = self.get_world(world_id)
        if world is None:
            # 理论上不会走到，防御未知驱动行为
            raise StorageError(f"创建世界 {world_id} 失败")
        return world

    def get_world(self, world_id: str) -> Optional[dict]:
        """读取世界状态；不存在返回 None。"""
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM world_state WHERE world_id = ?", (world_id,)
            ).fetchone()
        return _decode_world(row) if row else None

    def list_worlds(self) -> List[dict]:
        """列出全部世界（按 world_id 排序）。"""
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM world_state ORDER BY world_id"
            ).fetchall()
        return [_decode_world(r) for r in rows]

    def update_world(
        self,
        world_id: str,
        *,
        module_name: Optional[str] = None,
        player_ids: Optional[List[str]] = None,
        game_phase: Optional[str] = None,
        global_flags: Optional[Dict[str, Any]] = None,
        global_recap: Optional[str] = None,
    ) -> dict:
        """部分更新世界状态；传入 None 的字段保持不变。

        module_name 传值即换绑模组（须是 data/modules 下存在的文件），
        传空串可解绑；global_recap 是宏观记忆固化写回的全局前情提要，
        传空串可主动清空，传 None 表示本次不改动。
        """
        self._require_world(world_id)
        from ..module.loader import resolve as resolve_module
        sets: List[str] = []
        params: List[Any] = []
        if module_name is not None:
            # 非空换绑须为 data/modules 下存在的文件；空串表示解绑（允许清空）  # 状态：换绑/解绑
            if module_name:
                resolve_module(module_name)
            sets.append("module_name = ?")
            params.append(module_name)
        if player_ids is not None:
            sets.append("player_ids = ?")
            params.append(cjson.dumps(player_ids))
        if game_phase is not None:
            sets.append("game_phase = ?")
            params.append(game_phase)
        if global_flags is not None:
            sets.append("global_flags = ?")
            params.append(cjson.dumps(global_flags))
        if global_recap is not None:
            sets.append("global_recap = ?")
            params.append(global_recap)
        if sets:
            params.append(world_id)
            with self._db.transaction() as conn:
                conn.execute(
                    f"UPDATE world_state SET {', '.join(sets)} WHERE world_id = ?",
                    params,
                )
        return self.get_world(world_id)

    def delete_world(self, world_id: str) -> bool:
        """删除世界及其所有实体/轮次/历史（外键级联）；返回是否删除成功。"""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM world_state WHERE world_id = ?", (world_id,)
            )
        return cur.rowcount > 0

    # ============================================
    # 实体与角色
    # ============================================

    def create_entity(
        self,
        world_id: str,
        entity_id: str,
        entity_type: str,
        name: str,
        *,
        hp: int = 0,
        hp_max: int = 0,
        mp: int = 0,
        mp_max: int = 0,
        san: int = 0,
        san_max: int = 0,
        attributes_and_skills: Optional[Dict[str, Any]] = None,
        inventory: Optional[List[dict]] = None,
        tags: Optional[List[str]] = None,
        background: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """新建实体；世界必须已存在，否则抛 WorldNotFoundError。

        background 为调查员入模组前的背景故事 JSON（形象/信念/羁绊/创伤等），
        与 glyphkeeper Character 背景字段键对齐，键见 storage/schema.py 迁移 4 注释。
        """
        self._require_world(world_id)
        with self._db.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO entities (world_id, id, type, name, hp, hp_max, mp, mp_max, "
                    "san, san_max, attributes_and_skills, inventory, tags, background) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        world_id,
                        entity_id,
                        entity_type,
                        name,
                        hp,
                        hp_max,
                        mp,
                        mp_max,
                        san,
                        san_max,
                        cjson.dumps(attributes_and_skills or {}),
                        cjson.dumps(inventory or []),
                        cjson.dumps(tags or []),
                        cjson.dumps(background or {}),
                    ),
                )
            except sqlite3.IntegrityError as e:
                # 外键失败说明世界缺失，主键冲突说明实体重复，分开提示便于定位
                if "FOREIGN KEY" in str(e):
                    raise WorldNotFoundError(f"世界不存在: {world_id}") from e
                raise StorageError(f"创建实体 {entity_id} 失败: {e}") from e
        return self.get_entity(world_id, entity_id)

    def get_entity(self, world_id: str, entity_id: str) -> Optional[dict]:
        """读取单个实体；不存在返回 None。"""
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM entities WHERE world_id = ? AND id = ?",
                (world_id, entity_id),
            ).fetchone()
        return _decode_entity(row) if row else None

    def get_entities(
        self, world_id: str, *, entity_type: Optional[str] = None
    ) -> List[dict]:
        """读取某世界全部实体，可按 type 过滤（PC/NPC/SCENE/ITEM）。"""
        sql = "SELECT * FROM entities WHERE world_id = ?"
        params: List[Any] = [world_id]
        if entity_type is not None:
            sql += " AND type = ?"
            params.append(entity_type)
        sql += " ORDER BY id"
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_decode_entity(r) for r in rows]

    def update_entity(self, world_id: str, entity_id: str, **fields: Any) -> dict:
        """部分更新实体；字段按白名单校验，JSON 字段传 Python 对象即可。"""
        self._require_entity(world_id, entity_id)
        sets: List[str] = []
        params: List[Any] = []
        for key, value in fields.items():
            if key in _NUMERIC_COLUMNS:
                if not isinstance(value, int):
                    raise ValueError(
                        f"字段 {key} 需要整数，收到 {type(value).__name__}"
                    )
                sets.append(f"{key} = ?")
                params.append(value)
            elif key in _JSON_COLUMNS:
                sets.append(f"{key} = ?")
                params.append(cjson.dumps(value))
            elif key in _TEXT_COLUMNS:
                sets.append(f"{key} = ?")
                params.append(value)
            else:
                raise ValueError(f"未知实体字段: {key}")
        if not sets:
            return self.get_entity(world_id, entity_id)
        sets.append("updated_at = datetime('now', 'localtime')")
        params.extend([world_id, entity_id])
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE entities SET {', '.join(sets)} "
                "WHERE world_id = ? AND id = ?",
                params,
            )
        return self.get_entity(world_id, entity_id)

    def delete_entity(self, world_id: str, entity_id: str) -> bool:
        """删除实体；返回是否删除成功。"""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM entities WHERE world_id = ? AND id = ?",
                (world_id, entity_id),
            )
        return cur.rowcount > 0

    def adjust_stat(self, world_id: str, entity_id: str, field: str, delta: int) -> int:
        """原子增减数值字段（hp/san 等），返回调整后的新值。"""
        if field not in _NUMERIC_COLUMNS:
            raise ValueError(f"不可调整的数值字段: {field}")
        self._require_entity(world_id, entity_id)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE entities SET {field} = {field} + ?, "
                "updated_at = datetime('now', 'localtime') "
                "WHERE world_id = ? AND id = ?",
                (delta, world_id, entity_id),
            )
            row = conn.execute(
                f"SELECT {field} FROM entities WHERE world_id = ? AND id = ?",
                (world_id, entity_id),
            ).fetchone()
        return int(row[0])

    def add_tag(self, world_id: str, entity_id: str, tag: str) -> dict:
        """给实体追加一个 Tag（去重），返回更新后的实体。"""
        entity = self.get_entity(world_id, entity_id)
        if entity is None:
            raise EntityNotFoundError(f"实体不存在: {world_id}/{entity_id}")
        tags = entity["tags"]
        if tag not in tags:
            tags.append(tag)
        return self.update_entity(world_id, entity_id, tags=tags)

    def remove_tag(self, world_id: str, entity_id: str, tag: str) -> dict:
        """移除实体上的一个 Tag（不存在则静默），返回更新后的实体。"""
        entity = self.get_entity(world_id, entity_id)
        if entity is None:
            raise EntityNotFoundError(f"实体不存在: {world_id}/{entity_id}")
        tags = entity["tags"]
        if tag in tags:
            tags.remove(tag)
        return self.update_entity(world_id, entity_id, tags=tags)

    # ============================================
    # 最近对话与回档
    # ============================================

    def append_turn(
        self,
        world_id: str,
        *,
        turn_num: int,
        context_data: Optional[Dict[str, Any]] = None,
        state_diff: Optional[dict] = None,
    ) -> dict:
        """写入一轮记录并裁剪近程窗口，返回落库的轮次 dict。"""
        self._require_world(world_id)
        turn_id = make_turn_id(world_id, turn_num)
        with self._db.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO recent_turns (world_id, turn_id, turn_num, "
                    "context_data, state_diff) VALUES (?, ?, ?, ?, ?)",
                    (
                        world_id,
                        turn_id,
                        turn_num,
                        cjson.dumps(context_data or {}),
                        cjson.dumps(state_diff or {}),
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise StorageError(f"写入轮次 {turn_id} 失败（可能重复）: {e}") from e
            self._prune_turns(conn, world_id)
        return self.get_turn(world_id, turn_num)

    def commit_turn(
        self,
        world_id: str,
        turn_num: int,
        *,
        state_diff: Optional[dict] = None,
        context_data: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """单事务内应用 state_diff 并写入轮次记录（合并提交协调器唯一落库入口）。

        与 append_turn 的差异：本方法把「应用 diff」与「写轮次」放同一事务，
        保证一轮变更与回档记录原子一致，崩溃不留半截状态；空 diff 也允许。
        """
        self._require_world(world_id)
        diff = state_diff or {}
        turn_id = make_turn_id(world_id, turn_num)
        with self._db.transaction() as conn:
            if diff:
                self._apply_diff(conn, world_id, diff)  # 状态：应用本轮变更
            try:
                conn.execute(
                    "INSERT INTO recent_turns (world_id, turn_id, turn_num, "
                    "context_data, state_diff) VALUES (?, ?, ?, ?, ?)",
                    (
                        world_id,
                        turn_id,
                        turn_num,
                        cjson.dumps(context_data or {}),
                        cjson.dumps(diff),
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise StorageError(f"写入轮次 {turn_id} 失败（可能重复）: {e}") from e
            self._prune_turns(conn, world_id)
        return self.get_turn(world_id, turn_num)

    def update_turn_context_data(
        self, world_id: str, turn_num: int, **updates: Any
    ) -> dict:
        """合并更新某轮次 context_data 字段，不触碰 state_diff（回档单元不变）。

        供串行管线把 Narrator 玩家视角叙事覆盖 assistant、手记转存 directive 等；
        updates 为要合并写回的键值；轮次不存在抛 TurnNotFoundError。
        """
        turn = self.get_turn(world_id, turn_num)
        if turn is None:
            raise TurnNotFoundError(
                f"轮次不存在: world={world_id} turn={turn_num}"
            )
        merged = dict(turn.get("context_data") or {})
        merged.update(updates)
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE recent_turns SET context_data = ? "
                "WHERE world_id = ? AND turn_num = ?",
                (cjson.dumps(merged), world_id, turn_num),
            )
        return self.get_turn(world_id, turn_num)

    def _prune_turns(self, conn: sqlite3.Connection, world_id: str) -> None:
        # 只保留最近 N 轮；被裁掉的轮次连同其 state_diff 一并删除，超出窗口即不可回档
        conn.execute(
            "DELETE FROM recent_turns WHERE world_id = ? AND turn_num <= "
            "(SELECT turn_num FROM recent_turns WHERE world_id = ? "
            " ORDER BY turn_num DESC LIMIT 1 OFFSET ?)",
            (world_id, world_id, self._turns_window),
        )

    def get_turn(self, world_id: str, turn_num: int) -> Optional[dict]:
        """按轮次号读取单条记录；不存在返回 None。"""
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM recent_turns WHERE world_id = ? AND turn_num = ?",
                (world_id, turn_num),
            ).fetchone()
        return _decode_turn(row) if row else None

    def get_recent_turns(self, world_id: str, *, limit: Optional[int] = None) -> List[dict]:
        """按时间正序返回近程上下文，limit 缺省为窗口大小。"""
        limit = limit or self._turns_window
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM (SELECT * FROM recent_turns WHERE world_id = ? "
                " ORDER BY turn_num DESC LIMIT ?) ORDER BY turn_num ASC",
                (world_id, limit),
            ).fetchall()
        return [_decode_turn(r) for r in rows]

    def next_turn_num(self, world_id: str) -> int:
        """返回下一可用轮次号：当前最大 turn_num + 1，无轮次则从 1 开始。

        主 Agent 编排器据此自增轮次，保证每次落库轮次号单调递增。
        """
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_num), 0) + 1 AS n "
                "FROM recent_turns WHERE world_id = ?",
                (world_id,),
            ).fetchone()
        return int(row["n"])

    def get_unsolidified_turns(
        self, world_id: str, *, up_to_turn: Optional[int] = None
    ) -> List[dict]:
        """读取尚未固化进 RAG 的轮次（solidified=0，按 turn_num 升序）。

        up_to_turn 非空时只返回 <= 该轮次的记录，供固化接口按需截断批次。
        已固化的轮次不会重复出现，这是固化接口"只处理增量"的依据。
        """
        query = "SELECT * FROM recent_turns WHERE world_id = ? AND solidified = 0"
        params: List[Any] = [world_id]
        if up_to_turn is not None:
            query += " AND turn_num <= ?"
            params.append(int(up_to_turn))
        query += " ORDER BY turn_num ASC"
        with self._db.read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_decode_turn(r) for r in rows]

    def mark_turns_solidified(self, world_id: str, turn_nums: List[int]) -> int:
        """把指定轮次标记为已固化（solidified=1），返回实际更新的行数。

        固化接口在"提炼成功并写入 RAG"后调用，保证进度落库、崩溃后可续跑；
        占位符数量由内部 len(turn_nums) 生成，值全部走参数绑定，无注入风险。
        """
        nums = [int(n) for n in turn_nums]
        if not nums:
            return 0
        placeholders = ",".join("?" * len(nums))
        with self._db.transaction() as conn:
            cur = conn.execute(
                f"UPDATE recent_turns SET solidified = 1 WHERE world_id = ? "
                f"AND turn_num IN ({placeholders})",
                [world_id, *nums],
            )
        return cur.rowcount

    def undo_turn(self, world_id: str, turn_num: int) -> dict:
        """回档指定轮次：取反该轮 state_diff 原子应用，再删除该轮记录。

        返回被撤销的轮次信息；该轮已被窗口裁剪时抛 TurnNotFoundError。
        """
        turn = self.get_turn(world_id, turn_num)
        if turn is None:
            raise TurnNotFoundError(
                f"世界 {world_id} 没有第 {turn_num} 轮记录（可能已被窗口裁剪）"
            )
        diff = turn["state_diff"] or {}
        reverse = negate_diff(diff)
        with self._db.transaction() as conn:
            self._apply_diff(conn, world_id, reverse)  # 状态：应用反向 diff
            conn.execute(  # 状态：删除已撤销轮次
                "DELETE FROM recent_turns WHERE world_id = ? AND turn_num = ?",
                (world_id, turn_num),
            )
        return turn

    def undo_from(self, world_id: str, turn_num: int) -> List[dict]:
        """撤销到第 turn_num 轮之前：单事务内倒序反转并删除 >= 该轮的全部记录。

        与 undo_turn 的单轮语义不同，本方法把「撤销 N 及之后所有轮次」作为一个
        原子单元——先校验目标轮次存在（窗口裁剪后缺失抛 TurnNotFoundError），
        再在同一事务内倒序应用各轮反向 diff 并批量删除记录，避免循环撤销中途
        失败留下部分撤销的脏状态；返回被撤销轮次（按 turn_num 升序）。
        """
        if self.get_turn(world_id, turn_num) is None:
            raise TurnNotFoundError(
                f"世界 {world_id} 没有第 {turn_num} 轮记录（可能已被窗口裁剪），无法撤销到此轮之前"
            )
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM recent_turns WHERE world_id = ? AND turn_num >= ? "
                "ORDER BY turn_num ASC",
                (world_id, int(turn_num)),
            ).fetchall()
        turns = [_decode_turn(r) for r in rows]
        if not turns:
            return []  # 状态：目标轮次存在但无 >= 记录（防御），无撤销动作
        # 状态：倒序撤销——从最新轮向目标轮逐轮取反，保证状态依赖顺序正确
        ordered = sorted(turns, key=lambda t: t["turn_num"], reverse=True)
        with self._db.transaction() as conn:
            for t in ordered:
                diff = t["state_diff"] or {}
                self._apply_diff(conn, world_id, negate_diff(diff))  # 状态：倒序反转
            conn.execute(  # 状态：批量删除已撤销轮次
                "DELETE FROM recent_turns WHERE world_id = ? AND turn_num >= ?",
                (world_id, int(turn_num)),
            )
        return turns

    def _apply_diff(self, conn: sqlite3.Connection, world_id: str, diff: dict) -> None:
        # 数值分区：路径形如 "player_01.hp"，拆分实体与字段后增量更新
        for path, delta in diff["numeric_changes"].items():
            entity_id, _, field = path.partition(".")
            if not entity_id or field not in _NUMERIC_COLUMNS:
                raise StorageError(f"非法数值变更路径: {path}")
            cur = conn.execute(
                f"UPDATE entities SET {field} = {field} + ?, "
                "updated_at = datetime('now', 'localtime') "
                "WHERE world_id = ? AND id = ?",
                (delta, world_id, entity_id),
            )
            if cur.rowcount == 0:
                raise EntityNotFoundError(f"回档目标实体不存在: {world_id}/{entity_id}")
        # tags / inventory 分区：按实体分组读改写
        for group_name in ("tags", "inventory"):
            for entity_id, holder in diff.get(group_name, {}).items():
                row = conn.execute(
                    "SELECT * FROM entities WHERE world_id = ? AND id = ?",
                    (world_id, entity_id),
                ).fetchone()
                if row is None:
                    raise EntityNotFoundError(f"回档目标实体不存在: {world_id}/{entity_id}")
                current = cjson.loads_or(row[group_name]) or []
                self._apply_list_diff(current, holder, group_name)
                conn.execute(
                    f"UPDATE entities SET {group_name} = ?, "
                    "updated_at = datetime('now', 'localtime') "
                    "WHERE world_id = ? AND id = ?",
                    (cjson.dumps(current), world_id, entity_id),
                )

    @staticmethod
    def _apply_list_diff(current: list, holder: dict, group_name: str) -> None:
        # added 表示要补回，removed 表示要移除；物品按 name 匹配删除避免字典细节残留
        for item in holder.get("added", []):
            if item not in current:
                current.append(item)
        for item in holder.get("removed", []):
            if group_name == "inventory":
                current[:] = [i for i in current if i.get("name") != item.get("name")]
            else:
                if item in current:
                    current.remove(item)

    # ============================================
    # 全量历史冷备
    # ============================================

    def append_history(self, world_id: str, role: str, content: str) -> None:
        """追加一条原始对话到冷备日志。"""
        self._require_world(world_id)
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO chat_history_all (world_id, role, content) VALUES (?, ?, ?)",
                (world_id, role, content),
            )

    def query_history(
        self,
        world_id: str,
        *,
        limit: Optional[int] = None,
        since_id: Optional[int] = None,
    ) -> List[dict]:
        """按时间正序查询历史，可限制条数或从某条 id 之后开始。"""
        sql = "SELECT * FROM chat_history_all WHERE world_id = ?"
        params: List[Any] = [world_id]
        if since_id is not None:
            sql += " AND id > ?"
            params.append(since_id)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


# ============================================
# 全局单例
# ============================================

_storage: Optional[Storage] = None


def get_storage() -> Storage:
    """获取全局存储实例（懒加载单例，共用全局数据库）。"""
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage
