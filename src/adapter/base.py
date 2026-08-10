# -*- coding: utf-8 -*-
"""
@File     :   base.py
@Desc     :   抽象适配器基类 — 所有 adapter（CLI / OneBot / Web）继承的统一接入层
@Note     :   模板方法 run() 驱动 run_impl() 主循环；handle() 统一路由 SYSTEM_CMD / PLAYER_INPUT；
             本层持有 storage / memory / worker / llm，是窗口 id 到 world_id 映射的唯一边界，
             下游（run_narrated_turn / 存储 / 工具）只认 world_id，绝不接收窗口 id
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Optional

from src.core.ids import make_world_id
from src.core.log import get_logger

from .protocol import InboundMessage, MessageType, OutboundMessage

logger = get_logger(__name__)


# ============================================
# 抽象适配器基类
# ============================================


class AbstractAdapter(ABC):
    """统一接入层基类。

    子类需实现 parse() / send() / run_impl()；可直接使用 handle() 完成
    InboundMessage -> OutboundMessage 的统一路由（含系统命令分派）。
    命令分派内的世界管理 / 状态查询 / 回档 / 记忆调试均为多适配器共用逻辑，
    本类实现一次即可，CLI 与 OneBot 直接复用。
    """

    def __init__(
        self,
        storage: Any = None,
        memory: Any = None,
        worker: Any = None,
        *,
        llm: Any = None,
        session_id: str = "default",
    ) -> None:
        self.storage = storage
        self.memory = memory
        self.worker = worker
        self.llm = llm
        self.session_id = session_id
        # 当前会话映射的世界 id（窗口 -> 世界映射的落点，仅本层持有）
        self._world_id: Optional[str] = None
        self._running = False

    # ---- 子类需实现的接口 ----

    @abstractmethod
    async def parse(self, raw_input: Any) -> InboundMessage:
        """原始外部输入 -> 统一 InboundMessage，此处完成窗口/会话到 world_id 的映射。"""
        ...

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None:
        """统一 OutboundMessage -> 外部输出（终端 / 群消息 / HTTP 响应）。"""
        ...

    @abstractmethod
    async def run_impl(self) -> None:
        """启动适配器主循环。"""
        ...

    # ---- 生命周期 ----

    async def run(self) -> None:
        """启动适配器（模板方法）：进入主循环，退出后统一清理。"""
        try:
            await self.run_impl()
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        """默认清理钩子：停止后台固化 Worker，子类可覆盖扩展。"""
        if self.worker is not None:
            try:
                await self.worker.stop()  # 状态：优雅停止，等待后台任务退出
            except Exception as e:  # noqa: BLE001
                logger.debug(f"worker 停止异常: {e}")

    @property
    def is_running(self) -> bool:
        return self._running

    # ---- 统一消息路由 ----

    async def handle(self, msg: InboundMessage) -> OutboundMessage:
        """统一消息处理入口：InboundMessage -> OutboundMessage。

        路由规则：SYSTEM_CMD -> 系统命令处理；PLAYER_INPUT -> 回合管线执行。
        """
        if msg.type == MessageType.SYSTEM_CMD:
            return await self._handle_system_cmd(msg.text, msg)
        return await self._handle_player_input(msg.text, msg)

    # ---- 内部路由：玩家输入 -> 回合管线 ----

    async def _handle_player_input(self, text: str, msg: InboundMessage) -> OutboundMessage:
        """玩家输入 -> 串行管线（裁决 -> 演播 -> 落库），返回玩家视角叙事。"""
        if not text.strip():
            return OutboundMessage.system_msg("输入不能为空", level="warn", session_id=msg.session_id)
        world_id = msg.world_id or self._world_id  # 状态：消息已带世界优先，否则取会话当前世界
        if not world_id:
            return OutboundMessage.system_msg(
                "当前未选择世界。请先用 /world start <模组名> 开始游戏，或 /world use <世界ID> 切换。",
                level="warn", session_id=msg.session_id,
            )
        try:
            from src.agent.pipeline import run_narrated_turn

            # 状态：裁决档位/温度由 Director 自读 config.context.director，适配器不感知
            turn = await run_narrated_turn(
                self.storage,
                world_id,
                text.strip(),
                llm=self.llm,
                on_turn_committed=self._on_turn_committed,
            )
            return OutboundMessage.narrative(
                turn.narration, session_id=msg.session_id, world_id=world_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"回合执行异常 world={world_id}: {e}", exc_info=True)
            return OutboundMessage.system_msg(
                f"回合执行失败: {type(e).__name__}: {e}",
                level="error", session_id=msg.session_id,
            )

    async def _on_turn_committed(self, world_id: str, turn_num: int) -> None:
        """落库钩子：通知后台固化 Worker 安排该世界固化，失败不抛给管线。"""
        if self.worker is not None:
            self.worker.trigger_world(world_id)  # 状态：事件触发双通道之一，立即返回不阻塞

    # ---- 内部路由：系统命令分派 ----

    async def _handle_system_cmd(self, cmd: str, msg: InboundMessage) -> OutboundMessage:
        """系统命令分派入口；子类可在其上追加专属命令。"""
        low = cmd.strip().lower()
        sid = msg.session_id

        if low in ("/help", "/h"):
            return OutboundMessage.system_msg(_HELP_TEXT, session_id=sid)
        if low.startswith("/world"):
            return await self._handle_world_cmd(cmd.strip(), sid)
        if low in ("/status", "/st"):
            return await self._handle_status_cmd(sid)
        if low.startswith("/rollback"):
            return await self._handle_rollback_cmd(cmd.strip(), sid)
        if low.startswith("/memory"):
            return await self._handle_memory_cmd(cmd.strip(), sid)
        if low.startswith("/card"):
            return await self._handle_card_cmd(cmd.strip(), sid)
        if low in ("/module", "/module list", "/module ls"):
            return await self._handle_module_cmd(sid)
        return OutboundMessage.system_msg(f"未知命令: {cmd}（输入 /help 查看用法）", level="warn", session_id=sid)

    # ============================================
    # 世界管理命令
    # ============================================

    async def _handle_world_cmd(self, cmd: str, sid: str) -> OutboundMessage:
        """处理 /world 系列命令：list / start / use。

        /world start <模组名> 创建新世界并绑定 data/modules 下的模组文件（强制校验），
        同时把会话切换到新世界；重复创建同名模组世界时序号自增，不覆盖已有世界。
        """
        parts = cmd.split(maxsplit=2)
        sub = parts[1].strip().lower() if len(parts) > 1 else "list"

        if sub == "list":
            worlds = self.storage.list_worlds()
            if not worlds:
                return OutboundMessage.system_msg(
                    "当前无任何世界。使用 /world start <模组名> 创建。", session_id=sid,
                )
            lines = [f"世界列表 ({len(worlds)} 个):"]
            for w in worlds:
                mark = "  <- 当前" if w["world_id"] == self._world_id else ""
                mod = w.get("module_name") or "-"
                lines.append(f"  {w['world_id']}  [{mod}]{mark}")
            lines.append("使用 /world use <世界ID> 切换。")
            return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

        if sub == "start":
            rest = parts[2].strip() if len(parts) > 2 else ""
            if not rest:
                return OutboundMessage.system_msg(
                    "用法: /world start <模组名> [角色名...]\n"
                    "使用 /module list 查看可绑定的模组文件；"
                    "角色名可选（匹配种子库中的角色，先 /card import）。",
                    level="warn", session_id=sid,
                )
            tokens = rest.split()
            module_name = tokens[0]
            card_names = tokens[1:]  # 状态：可选种子角色名（创建世界时选择并拷贝）
            try:
                world_id = self._create_world_for_module(module_name)
            except Exception as e:  # noqa: BLE001
                logger.error(f"创建世界失败: {e}")
                return OutboundMessage.system_msg(
                    f"创建世界失败: {type(e).__name__}: {e}",
                    level="error", session_id=sid,
                )
            bound = []
            if card_names:
                from src.tools.card_store import copy_seed_to_world, list_seed_cards

                seeds = list_seed_cards()
                for cn in card_names:
                    hit = next((s for s in seeds if cn in s["name"]), None)
                    if hit:
                        eid = copy_seed_to_world(self.storage, hit["seed_id"], world_id)
                        bound.append(f"{hit['name']}({eid})")
                    else:
                        bound.append(f"{cn}(未找到种子角色)")
            self._world_id = world_id  # 状态：创建即切换
            lines = [f"世界已创建并切换: {world_id}（模组: {module_name}）"]
            if bound:
                lines.append("已绑定 PC: " + "、".join(bound))
            lines.append("直接输入行动文本开始探索。")
            return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

        if sub == "use":
            world_id = parts[2].strip() if len(parts) > 2 else ""
            if not world_id or self.storage.get_world(world_id) is None:
                return OutboundMessage.system_msg(
                    f"世界不存在: {world_id}。使用 /world list 查看已有世界。",
                    level="warn", session_id=sid,
                )
            self._world_id = world_id  # 状态：会话切换目标世界
            return OutboundMessage.system_msg(f"已切换到世界: {world_id}", session_id=sid)

        return OutboundMessage.system_msg(
            "用法:\n"
            "  /world list               - 列出所有世界\n"
            "  /world start <模组名>     - 创建新世界并绑定模组\n"
            "  /world use <世界ID>       - 切换当前世界",
            level="warn", session_id=sid,
        )

    def _create_world_for_module(self, module_name: str) -> str:
        """按模组名创建世界：序号取已有世界数 + 1，slug 由模组文件名清洗生成。"""
        from src.module.loader import resolve as resolve_module

        resolve_module(module_name)  # 状态：绑定前强制校验模组文件存在（文件层唯一出口）
        slug = _slugify(module_name)
        seq = len(self.storage.list_worlds()) + 1
        world_id = make_world_id(seq, slug)
        self.storage.ensure_world(world_id, module_name=module_name)
        return world_id

    # ============================================
    # 状态查询命令
    # ============================================

    async def _handle_status_cmd(self, sid: str) -> OutboundMessage:
        """显示当前世界状态：模组 / 轮次 / PC 实体快照。"""
        world_id = self._world_id
        if not world_id:
            return OutboundMessage.system_msg(
                "当前未选择世界。使用 /world start <模组名> 或 /world use <世界ID>。",
                level="warn", session_id=sid,
            )
        world = self.storage.get_world(world_id)
        if world is None:
            return OutboundMessage.system_msg(
                f"当前世界不存在（可能已被删除）: {world_id}", level="warn", session_id=sid,
            )
        pcs = self.storage.get_entities(world_id, entity_type="PC")
        turn = self.storage.next_turn_num(world_id) - 1  # 状态：下一轮号 - 1 即最新轮次
        lines = [
            f"世界:      {world_id}",
            f"模组:      {world.get('module_name') or '-'}",
            f"最新轮次:  {max(turn, 0)}",
        ]
        if pcs:
            lines.append(f"PC ({len(pcs)}):")
            for pc in pcs:
                lines.append(
                    f"  {pc['name']}  HP {pc['hp']}/{pc['hp_max']}  "
                    f"SAN {pc['san']}/{pc['san_max']}  {pc.get('tags') or ''}"
                )
        else:
            lines.append("PC: 无（世界暂无调查员实体）")
        return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

    # ============================================
    # 回档命令
    # ============================================

    async def _handle_rollback_cmd(self, cmd: str, sid: str) -> OutboundMessage:
        """处理 /rollback 命令 — 回档到指定轮次之前（物理 + 语义双侧一致）。

        /rollback         列出最近轮次供选择
        /rollback <N>     回档到第 N 轮之前（撤销 >= N 的轮次）
        回档复用 src.memory.worker.rollback_world 编排，与后台固化共享该世界锁互斥。
        """
        world_id = self._world_id
        if not world_id:
            return OutboundMessage.system_msg(
                "当前未选择世界。先 /world start 或 /world use。", level="warn", session_id=sid,
            )
        if self.memory is None:
            return OutboundMessage.system_msg(
                "记忆后端未配置，无法执行语义侧回档。", level="warn", session_id=sid,
            )
        parts = cmd.split(maxsplit=1)
        target = int(parts[1].strip()) if len(parts) > 1 else None

        latest = self.storage.next_turn_num(world_id) - 1  # 状态：最新轮次号
        if latest < 1:
            return OutboundMessage.system_msg("该世界尚无轮次可回档。", level="warn", session_id=sid)

        if target is None:
            turns = self.storage.get_recent_turns(world_id, limit=10)
            lines = [f"最新轮次 {latest}，最近 {len(turns)} 轮:"]
            for t in turns:
                snippet = (t["context_data"].get("user") or "")[:40]
                lines.append(f"  #{t['turn_num']}  {snippet}")
            lines.append("使用 /rollback <N> 回档到第 N 轮之前。")
            return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

        if target < 1 or target > latest + 1:
            return OutboundMessage.system_msg(
                f"轮次 {target} 越界（当前范围 1-{latest + 1}）", level="error", session_id=sid,
            )
        try:
            from src.memory.worker import rollback_world

            deleted = await rollback_world(
                self.storage, self.memory, world_id, target, worker=self.worker,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"回档失败 world={world_id} target={target}: {e}")
            return OutboundMessage.system_msg(
                f"回档失败: {type(e).__name__}: {e}", level="error", session_id=sid,
            )
        return OutboundMessage.system_msg(
            f"已回档到第 {target} 轮之前（撤销 >= {target} 的轮次，RAG 清理 {deleted} 条）",
            session_id=sid,
        )

    # ============================================
    # 记忆调试命令
    # ============================================

    async def _handle_memory_cmd(self, cmd: str, sid: str) -> OutboundMessage:
        """处理 /memory <查询> — 语义召回调试，直接走 Memory.search 带 world_id 过滤。"""
        world_id = self._world_id
        if not world_id:
            return OutboundMessage.system_msg(
                "当前未选择世界。先 /world start 或 /world use。", level="warn", session_id=sid,
            )
        if self.memory is None:
            return OutboundMessage.system_msg(
                "记忆后端未配置，无法检索。", level="warn", session_id=sid,
            )
        parts = cmd.split(maxsplit=1)
        query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            return OutboundMessage.system_msg(
                "用法: /memory <查询内容>\n按语义召回当前世界的历史记忆，供调试召回质量。",
                level="warn", session_id=sid,
            )
        try:
            hits = await self.memory.search(query, world_id, top_k=5)
        except Exception as e:  # noqa: BLE001
            logger.error(f"记忆检索失败 world={world_id}: {e}")
            return OutboundMessage.system_msg(
                f"记忆检索失败: {type(e).__name__}: {e}", level="error", session_id=sid,
            )
        if not hits:
            return OutboundMessage.system_msg(
                f"未命中相关记忆（查询: {query[:60]}）", session_id=sid,
            )
        lines = [f"命中 {len(hits)} 条（查询: {query[:60]}）:"]
        for h in hits:
            turn = f"t{h.turn_num}" if h.turn_num else "?"
            lines.append(f"  [{turn} | {h.score:.3f}] {h.text[:80]}")
        return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

    # ============================================
    # 角色卡命令
    # ============================================

    async def _handle_card_cmd(self, cmd: str, sid: str) -> OutboundMessage:
        """处理 /card 系列命令：import / list / use。

        /card import <来源>  解析 xlsx 角色卡并存入种子库（world_id 空，不绑定任何世界）
        /card list           列出种子库中的候选角色（供 /world start 或 /card use 选择）
        /card use <种子id>   把种子角色拷贝一份到当前世界并绑定为 PC
        """
        parts = cmd.split(maxsplit=2)
        sub = parts[1].strip().lower() if len(parts) > 1 else "list"

        if sub == "import":
            source = parts[2].strip() if len(parts) > 2 else ""
            try:
                from src.tools.card_importer import parse_investigator_xlsx, resolve_card_source
                from src.tools.card_store import save_seed

                path = resolve_card_source(source)
                parsed = parse_investigator_xlsx(path)
                meta, entity = parsed["meta"], parsed["entity"]
                seed_id = save_seed(entity, meta, source=str(path))
            except Exception as e:  # noqa: BLE001
                logger.error(f"角色卡导入失败: {e}")
                return OutboundMessage.system_msg(
                    f"角色卡导入失败: {type(e).__name__}: {e}",
                    level="error", session_id=sid,
                )
            lines = [
                f"角色卡已导入种子库: {seed_id}",
                f"  姓名: {meta.get('name') or '-'}  职业: {meta.get('occupation') or '-'}"
                f"  幸运: {meta.get('luck', 0)}",
            ]
            if meta.get("gender") or meta.get("age") or meta.get("birthplace"):
                lines.append(
                    f"  性别: {meta.get('gender') or '-'}  年龄: {meta.get('age', 0)}"
                    f"  住地: {meta.get('birthplace') or '-'}"
                )
            lines.append("用 /world start <模组名> <角色名> 创建世界并绑定，或 /card use 加入当前世界。")
            return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

        if sub == "list":
            from src.tools.card_store import list_seed_cards

            seeds = list_seed_cards()
            if not seeds:
                return OutboundMessage.system_msg(
                    "种子库为空。使用 /card import <xlsx路径或 data/cards 内文件名> 导入。",
                    session_id=sid,
                )
            lines = [f"种子角色 ({len(seeds)} 个，/world start 或 /card use 使用):"]
            for s in seeds:
                lines.append(
                    f"  {s['seed_id']}  {s['name']}  [{s['occupation'] or '-'}]"
                    f"  幸运{s['luck']}"
                )
            lines.append("用法: /world start <模组名> <角色名>，或 /card use <种子id>。")
            return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

        if sub == "use":
            seed_id = parts[2].strip() if len(parts) > 2 else ""
            world_id = self._world_id
            if not seed_id:
                return OutboundMessage.system_msg(
                    "用法: /card use <种子id>\n使用 /card list 查看可用的种子角色。",
                    level="warn", session_id=sid,
                )
            if not world_id:
                return OutboundMessage.system_msg(
                    "当前未选择世界。先 /world start <模组名> 创建，或 /world use <世界ID> 切换。",
                    level="warn", session_id=sid,
                )
            try:
                from src.tools.card_store import copy_seed_to_world

                entity_id = copy_seed_to_world(self.storage, seed_id, world_id)
            except Exception as e:  # noqa: BLE001
                logger.error(f"种子角色加入世界失败: {e}")
                return OutboundMessage.system_msg(
                    f"加入世界失败: {type(e).__name__}: {e}",
                    level="error", session_id=sid,
                )
            return OutboundMessage.system_msg(
                f"种子角色已拷贝到当前世界 {world_id}，实体 ID: {entity_id}。",
                session_id=sid,
            )

        return OutboundMessage.system_msg(
            "用法:\n"
            "  /card import <来源>    - 导入 xlsx 角色卡到种子库（路径或 data/cards 内文件名）\n"
            "  /card list             - 列出种子库中的候选角色\n"
            "  /card use <种子id>     - 把种子角色拷贝到当前世界",
            level="warn", session_id=sid,
        )

    # ============================================
    # 模组列表命令
    # ============================================

    async def _handle_module_cmd(self, sid: str) -> OutboundMessage:
        """列出 data/modules 下可绑定的模组文件，供 /world start 选择。"""
        from src.module.loader import list_modules

        mods = list_modules()
        if not mods:
            return OutboundMessage.system_msg(
                "data/modules 目录为空。请先把模组原文（pdf/docx/md）放入该目录。",
                level="warn", session_id=sid,
            )
        lines = [f"可用模组 ({len(mods)} 个):"]
        for m in mods:
            lines.append(f"  {m.module_name}  ({m.size} 字节)")
        lines.append("使用 /world start <模组名> 创建世界。")
        return OutboundMessage.system_msg("\n".join(lines), session_id=sid)


# ============================================
# 辅助函数与帮助文本
# ============================================


def _slugify(module_name: str) -> str:
    """从模组文件名清洗出世界 slug：去扩展名、转小写、非法字符归一为下划线并截断。"""
    stem = module_name.rsplit(".", 1)[0] if "." in module_name else module_name
    slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    return slug[:32] or "module"


_HELP_TEXT = (
    "可用命令:\n"
    "======= 世界 =======\n"
    "  /world list                - 列出所有世界\n"
    "  /world start <模组名> [角色名...] - 创建世界并绑定模组，可选绑定种子角色\n"
    "  /world use <世界ID>        - 切换当前世界\n"
    "  /module list               - 列出可绑定的模组文件\n"
    "======= 角色卡 =======\n"
    "  /card import <来源>        - 导入 xlsx 角色卡到种子库（路径或 data/cards 内文件名）\n"
    "  /card list                 - 列出种子库中的候选角色\n"
    "  /card use <种子id>         - 把种子角色拷贝到当前世界\n"
    "======= 游戏 =======\n"
    "  /status                    - 查看当前世界与 PC 状态\n"
    "  /rollback                  - 列出最近轮次\n"
    "  /rollback <N>              - 回档到第 N 轮之前（物理 + 语义双侧）\n"
    "  /memory <查询>             - 语义召回当前世界的历史记忆\n"
    "======= 其他 =======\n"
    "  /help /h                   - 显示此帮助\n"
    "  /quit /q                   - 退出\n"
    "直接输入行动文本开始探索。"
)
