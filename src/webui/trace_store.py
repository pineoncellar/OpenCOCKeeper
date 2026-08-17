#- encoding: utf-8 -#
#
# @File     :   trace_store.py
# @Desc     :   持久化 Trace 存储 — 每世界一个目录，每轮一个 JSONL 文件
# @Note     :   与内存 TraceBus 分工：TraceBus 负责实时增量广播，本模块负责
#              历史持久化与按轮读取；写路径统一在 TraceBus.publish 内触发，
#              生产端无感知。目录 logs/traces/<world_id>/turn-000000.jsonl，
#              文件名即轮次索引，重启后扫目录即可恢复全部历史（不按日期分割）。
#              写失败只记日志不抛出——trace 属辅助调试，绝不阻断游戏主流程；
#              TRACE_DIR 为模块级变量，测试可替换为 tmp 目录
#

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.core.config import PROJECT_ROOT
from src.core.log import get_logger
from src.webui.trace_engine import TraceEvent

logger = get_logger(__name__)

# trace 根目录（模块启动时创建；测试可 monkeypatch 到临时目录）
TRACE_DIR: Path = PROJECT_ROOT / "logs" / "traces"
TRACE_DIR.mkdir(parents=True, exist_ok=True)

# 轮次文件名：turn-000001.jsonl（六位定宽，字典序即轮次序）
_TURN_RE = re.compile(r"^turn-(\d+)\.jsonl$")


def _safe_world_id(world_id: str) -> str:
    """清洗 world_id 为安全目录名，杜绝路径穿越；空串归 _unknown 占位防散落根目录。"""
    raw = str(world_id).strip()
    if not raw:
        return "_unknown"
    return (
        raw
        .replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
        .replace("..", "_")
    )


def _turn_path(root: Path, world_id: str, turn_num: int) -> Path:
    return root / _safe_world_id(world_id) / f"turn-{int(turn_num):06d}.jsonl"


def _count_lines(path: Path) -> int:
    """统计文件有效行数（跳过空行）；文件异常返回 0。"""
    try:
        return sum(1 for line in path.open(encoding="utf-8") if line.strip())
    except Exception:  # noqa: BLE001  读坏文件不阻断列表
        return 0


# ====================================================================
# TurnMeta — 轮次元信息（列表接口用，避免读全量事件）
# ====================================================================


@dataclass
class TurnMeta:
    """一轮 trace 的元信息：轮号 / 事件数 / 最后更新时间。"""

    turn_num: int
    event_count: int
    updated_at: str       # ISO 时间串（文件 mtime）


# ====================================================================
# TraceStore — 持久化存储门面
# ====================================================================


class TraceStore:
    """按 (world_id, turn_num) 路由追加写入，扫目录恢复历史。

    append 单行追加（JSONL），原子按行写，崩溃最多丢半行（读取时跳过坏行）；
    同一世界同一轮由 asyncio 单线程串行追加，无跨进程并发竞争。
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = Path(root) if root is not None else TRACE_DIR
        self._root.mkdir(parents=True, exist_ok=True)

    # ---- 写入 ----

    def append(self, event: TraceEvent) -> None:
        """把事件追加到对应世界对应轮次文件；失败仅记日志不抛出。"""
        try:
            path = _turn_path(self._root, event.world_id, event.turn_num)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(event), ensure_ascii=False)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # noqa: BLE001  写失败不阻断游戏主流程
            logger.warning(
                "TraceStore 写入失败 world=%s turn=%s: %s",
                event.world_id, event.turn_num, e,
            )

    # ---- 读取 ----

    def list_world_ids(self) -> List[str]:
        """扫描根目录，返回含轮次文件的世界 id 列表（字典序）。"""
        if not self._root.exists():
            return []
        worlds = []
        for child in sorted(self._root.iterdir()):
            if not child.is_dir():
                continue
            if any(p.is_file() and _TURN_RE.match(p.name) for p in child.iterdir()):
                worlds.append(child.name)
        return worlds

    def world_summaries(self) -> List[dict]:
        """每个世界一行摘要：世界 id / 轮数 / 最新轮号 / 最新更新时间。

        供 Trace 面板左侧世界列表一次性填充。
        """
        result = []
        for world_id in self.list_world_ids():
            turns = self.list_turns(world_id, limit=1 << 20)  # 全量元信息
            if not turns:
                continue
            result.append(
                {
                    "world_id": world_id,
                    "turn_count": len(turns),
                    "latest_turn": turns[0].turn_num,
                    "latest_ts": turns[0].updated_at,
                }
            )
        return result

    def list_turns(
        self, world_id: str, limit: int = 20, offset: int = 0
    ) -> List[TurnMeta]:
        """列出某世界轮次元信息，按轮次倒序（最新在前），支持分页。"""
        d = self._root / _safe_world_id(world_id)
        if not d.exists():
            return []
        metas: List[TurnMeta] = []
        for p in d.iterdir():
            m = _TURN_RE.match(p.name)
            if not m or not p.is_file():
                continue
            metas.append(
                TurnMeta(
                    turn_num=int(m.group(1)),
                    event_count=_count_lines(p),
                    updated_at=_iso_from_mtime(p),
                )
            )
        metas.sort(key=lambda t: t.turn_num, reverse=True)  # 状态：最新在前
        return metas[offset : offset + limit]

    def count_turns(self, world_id: str) -> int:
        """某世界轮次文件总数（分页接口 total 用，避免全量 list 读事件数）。"""
        d = self._root / _safe_world_id(world_id)
        if not d.exists():
            return 0
        return sum(1 for p in d.iterdir() if p.is_file() and _TURN_RE.match(p.name))

    def load_turn(self, world_id: str, turn_num: int) -> List[TraceEvent]:
        """读取某轮全部事件；文件缺失返回空列表，坏行静默跳过。"""
        path = _turn_path(self._root, world_id, turn_num)
        if not path.exists():
            return []
        events: List[TraceEvent] = []
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            return []
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(TraceEvent(**json.loads(line)))
            except Exception:  # noqa: BLE001  坏行（半行写断）跳过
                logger.debug("TraceStore 跳过坏行 world=%s turn=%s", world_id, turn_num)
        return events

    # ---- 删除 ----

    def delete_world(self, world_id: str) -> int:
        """删除某世界的全部 trace（整目录），返回删除的轮次文件数。

        供删除世界编排调用——SQLite/RAG 清理后同步清空对应 trace，
        避免残留孤儿 trace 目录；目录不存在或删除失败返回 0（trace 属辅助，
        失败仅记日志不阻断主流程）。路径经 _safe_world_id 清洗防穿越。
        """
        d = self._root / _safe_world_id(world_id)
        if not d.exists():
            return 0
        try:
            files = [p for p in d.iterdir() if p.is_file() and _TURN_RE.match(p.name)]
            for p in files:
                p.unlink()
            # 状态：清空目录内其余残留文件后移除目录本身
            for p in d.iterdir():
                if p.is_file():
                    p.unlink()
            try:
                d.rmdir()
            except OSError:
                pass  # 目录非空/被占用时保留，不影响删除
            logger.info("TraceStore 删除世界 world=%s 轮次文件=%d", world_id, len(files))
            return len(files)
        except Exception as e:  # noqa: BLE001
            logger.warning("TraceStore 删除世界失败 world=%s: %s", world_id, e)
            return 0


# ====================================================================
# 工具函数与全局单例
# ====================================================================


def _iso_from_mtime(path: Path) -> str:
    """文件 mtime 转 ISO 时间串；取不到时回退空串。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        return ""


_trace_store: Optional[TraceStore] = None


def get_trace_store() -> TraceStore:
    """获取全局 TraceStore 单例（惰性初始化）。"""
    global _trace_store
    if _trace_store is None:
        _trace_store = TraceStore()
    return _trace_store
