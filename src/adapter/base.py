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
from typing import Any, Dict, Optional

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
        # 状态：进行中的世界创建交互流程（/world start <世界id> 后两步收集：模组名 -> 角色卡名）
        self._pending_world_flow: Dict[str, dict] = {}
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
        """玩家输入 -> 串行管线（裁决 -> 演播 -> 落库），返回玩家视角叙事。

        归档（已结团）世界为只读——拒绝继续游玩，返回提示不触发管线；
        管线判定终局（is_ending=True）时返回终局结算卡片并重置会话世界指针。
        """
        if not text.strip():
            return OutboundMessage.system_msg("输入不能为空", level="warn", session_id=msg.session_id)
        # 状态：世界创建交互流程（/world start 后两步收集：模组名 -> 角色卡名）
        pending = self._pending_world_flow.get(msg.session_id)
        if pending is not None:
            return await self._step_world_flow(pending, text.strip(), msg)
        world_id = msg.world_id or self._world_id  # 状态：消息已带世界优先，否则取会话当前世界
        if not world_id:
            return OutboundMessage.system_msg(
                "当前未选择世界。请先用 /world start <世界ID> 开始游戏，或 /world load <世界ID> 载入。",
                level="warn", session_id=msg.session_id,
            )
        world = self.storage.get_world(world_id)
        if world is None:
            return OutboundMessage.system_msg(
                f"当前世界不存在（可能已被删除）: {world_id}",
                level="warn", session_id=msg.session_id,
            )
        if world.get("status") == "ARCHIVED":  # 状态：归档世界只读拦截
            return OutboundMessage.system_msg(
                f"世界已归档（结团），只读不可继续游玩。"
                f"载入其他世界或 /world start 开启新游戏。",
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
                memory=self.memory,
                worker=self.worker,
                on_turn_committed=self._on_turn_committed,
            )
            if turn.ended:  # 状态：终局轮——渲染结算卡片并退回主菜单
                self._world_id = None
                return self._render_ending_card(world_id, turn, msg.session_id)
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
        """处理 /world 系列命令：list / start / load / delete。

        /world start <世界ID> 登记交互式创建流程——先收集模组名（校验存在），
        再收集角色卡名，随后创建世界并绑定 data/modules 下的模组文件（强制校验）；
        世界 id 不允许重复；创建后会话切换到新世界；
        /world load <世界ID> 载入已有世界（会话切换，等同读档续玩）；
        /world delete <世界ID> 永久删除世界（SQLite 级联实体/轮次/历史 + RAG 语义记忆）。
        """
        parts = cmd.split(maxsplit=2)
        sub = parts[1].strip().lower() if len(parts) > 1 else "list"

        if sub == "list":
            worlds = self.storage.list_worlds()
            if not worlds:
                return OutboundMessage.system_msg(
                    "当前无任何世界。使用 /world start <世界ID> 创建。", session_id=sid,
                )
            lines = [f"世界列表 ({len(worlds)} 个):"]
            for w in worlds:
                mark = "  <- 当前" if w["world_id"] == self._world_id else ""
                mod = w.get("module_name") or "-"
                archived = "  [已归档]" if w.get("status") == "ARCHIVED" else ""
                lines.append(f"  {w['world_id']}  [{mod}]{archived}{mark}")
            lines.append("使用 /world load <世界ID> 载入。")
            return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

        if sub == "start":
            world_id = parts[2].strip() if len(parts) > 2 else ""
            err = self._validate_new_world_id(world_id)
            if err:
                return OutboundMessage.system_msg(
                    f"用法: /world start <世界ID>（如 /world start test1）\n{err}",
                    level="warn", session_id=sid,
                )
            # 状态：登记交互式创建流程——先收模组名，再收角色卡名，两步校验后创建
            self._pending_world_flow[sid] = {
                "world_id": world_id, "step": "module", "module_name": None,
            }
            return OutboundMessage.system_msg(
                f"世界 id 已登记: {world_id}。\n"
                f"请输入要绑定的模组名（/module list 查看可选模组）：",
                session_id=sid,
            )



        if sub == "load":
            world_id = parts[2].strip() if len(parts) > 2 else ""
            if not world_id or self.storage.get_world(world_id) is None:
                return OutboundMessage.system_msg(
                    f"世界不存在: {world_id}。使用 /world list 查看已有世界。",
                    level="warn", session_id=sid,
                )
            self._world_id = world_id  # 状态：会话载入目标世界
            # 状态：载入后附上回忆块（最新程序回复 + 最近记忆），帮玩家接续存档
            return await self._build_world_recall(world_id, sid)

        if sub == "archive":
            return await self._handle_archive_cmd(sid)

        if sub == "delete":
            world_id = parts[2].strip() if len(parts) > 2 else ""
            if not world_id or self.storage.get_world(world_id) is None:
                return OutboundMessage.system_msg(
                    f"世界不存在: {world_id}。使用 /world list 查看已有世界。",
                    level="warn", session_id=sid,
                )
            try:
                from src.memory.worker import delete_world

                # 状态：先清 RAG 语义记忆（需世界仍存在），再删 SQLite（级联实体/轮次/历史）
                rag_deleted = await delete_world(
                    self.storage, self.memory, world_id, worker=self.worker,
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"删除世界失败 world={world_id}: {e}")
                return OutboundMessage.system_msg(
                    f"删除世界失败: {type(e).__name__}: {e}",
                    level="error", session_id=sid,
                )
            if self._world_id == world_id:
                self._world_id = None  # 状态：删除当前选中世界后取消会话指向
            return OutboundMessage.system_msg(
                f"世界已删除: {world_id}（RAG 清理 {rag_deleted} 条）", session_id=sid,
            )

        return OutboundMessage.system_msg(
            "用法:\n"
            "  /world list               - 列出所有世界\n"
            "  /world start <世界ID>     - 创建新世界（交互式输入模组名/角色卡名）\n"
            "  /world load <世界ID>      - 载入已有世界\n"
            "  /world archive            - 主动结团（BD 软结局）并归档当前世界\n"
            "  /world delete <世界ID>    - 删除世界（级联 + RAG 清理）",
            level="warn", session_id=sid,
        )

    def _validate_new_world_id(self, value: str) -> Optional[str]:
        """校验用户自定义世界 id：非空 + 安全字符 + 不重复；合法返回 None，否则返回错误信息。"""
        v = (value or "").strip()
        if not v:
            return "世界 id 不能为空。"
        if not _WORLD_ID_RE.match(v):
            return "世界 id 只能包含字母/数字/下划线/连字符（不能以连字符开头），长度不超过 64。"
        if self.storage.get_world(v) is not None:
            return f"世界 id 已存在: {v}，请换一个（/world list 查看已有世界）。"
        return None

    async def _step_world_flow(self, pending: dict, text: str, msg: InboundMessage) -> OutboundMessage:
        """推进世界创建交互流程（/world start <世界id> 后）：模组名 -> 角色卡名 -> 创建。"""
        sid = msg.session_id
        if pending["step"] == "module":
            # 状态：第一步——校验模组存在（resolve_fuzzy 支持自动补后缀）
            from src.module.loader import resolve_fuzzy as resolve_module

            try:
                path = resolve_module(text)
            except Exception as e:  # noqa: BLE001
                return OutboundMessage.system_msg(
                    f"模组不存在: {text}（{type(e).__name__}）。"
                    f"使用 /module list 查看可选模组，请重新输入：",
                    level="warn", session_id=sid,
                )
            pending["module_name"] = path.name
            pending["step"] = "card"
            return OutboundMessage.system_msg(
                f"模组已确认: {path.name}\n"
                f"请输入使用的角色卡名（/card list 查看可用角色；输入“跳过”可不绑定角色）：",
                session_id=sid,
            )
        # 状态：第二步——收集角色卡名并创建世界
        self._pending_world_flow.pop(sid, None)  # 先清流程，防创建过程嵌套触发
        card_name = text.strip()
        if not card_name or card_name in ("跳过", "跳过角色", "无", "-"):
            card_names: list = []
        else:
            card_names = [card_name]
        return await self._finalize_world_creation(
            pending["world_id"], pending["module_name"], card_names, sid,
        )

    async def _finalize_world_creation(
        self, world_id: str, module_name: str, card_names: list, sid: str,
    ) -> OutboundMessage:
        """创建世界（绑定模组）-> 绑定种子角色 -> 切换会话 -> 有 PC 时自动开场演播。"""
        try:
            from src.module.loader import resolve_fuzzy as resolve_module

            path = resolve_module(module_name)  # 状态：绑定前强制校验模组存在（含自动补后缀）
            full_name = path.name
            self.storage.ensure_world(world_id, module_name=full_name)
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
        lines = [f"世界已创建并切换: {world_id}（模组: {full_name}）"]
        if bound:
            lines.append("已绑定 PC: " + "、".join(bound))
            # 状态：模组 + PC 双要素齐备，自动顺承 Turn 0 开场演播
            # 无静默降级——开场失败（前置缺失/LLM 异常）显式拦截提示，可修复后重试
            try:
                from src.agent.opening import run_opening_narration
                from src.core.exceptions import OpeningError

                opened = await run_opening_narration(
                    self.storage, world_id, memory=self.memory, llm=self.llm,
                )
                return OutboundMessage.narrative(
                    "\n".join(lines + ["", opened.narration]),
                    session_id=sid, world_id=world_id,
                )
            except OpeningError as e:
                lines.append(f"（开场初始化被拦截: {e}）")
                lines.append("请修复后重试 /world start，或直接输入行动文本继续。")
                return OutboundMessage.system_msg("\n".join(lines), session_id=sid)
        lines.append("直接输入行动文本开始探索。")
        return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

    async def _build_world_recall(self, world_id: str, sid: str) -> OutboundMessage:
        """载入世界时的回忆块：最新程序回复 + 最近记忆，帮玩家接续上次存档。

        输出两部分（有则显示，无则跳过）：
        ① 最新程序回复——recent_turns 最近一轮的玩家视角叙事（assistant，缺省用手记）；
        ② 最近记忆——memory.search 语义召回 top 3（无记忆后端则跳过）。
        回忆是附加信息，任一步失败都不影响载入本身。
        """
        lines = [f"已载入世界: {world_id}"]
        has_content = False
        # ① 最新程序回复
        recent = self.storage.get_recent_turns(world_id, limit=1)
        if recent:
            cd = recent[-1].get("context_data") or {}
            narration = (cd.get("assistant") or cd.get("directive") or "").strip()
            if narration:
                has_content = True
                lines += ["", "【上次进展】", narration]
        # ② 最近记忆（语义召回；无记忆后端跳过）
        if self.memory is not None:
            try:
                hits = await self.memory.search(
                    "最近发生了什么事件、当前处境、已掌握的线索与下一步目标",
                    world_id,
                    top_k=3,
                )
            except Exception as e:  # noqa: BLE001  回忆失败不影响载入
                logger.debug(f"载入世界回忆召回失败 world={world_id}: {e}")
                hits = []
            if hits:
                has_content = True
                lines.append("")
                lines.append("【近期记忆】")
                # 状态：记忆条目用 · 项目符号排版，不带 [tN] 轮次前缀——
                # 括号标记对玩家接续存档是噪音，轮次信息可在 Worlds 面板记忆卡片回查
                for h in hits:
                    lines.append(f"  · {h.text}")
        if not has_content:
            lines.append("（该世界暂无历史记录，直接输入行动文本开始探索）")
        return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

    # ============================================
    # 结团归档命令
    # ============================================

    async def _handle_archive_cmd(self, sid: str) -> OutboundMessage:
        """处理 /world archive — 主动结团（BD 软结局）并归档当前世界。

        构造 is_ending=True / ending_type=BD 的终局契约进入收尾管线：
        终局演播 -> 全盘固化 -> __ENDING__ 快照 -> status=ARCHIVED；
        无静默降级——任一步失败抛 EndingError，世界保持活跃可修复后重试；
        成功后重置会话世界指针并渲染终局结算卡片。
        """
        world_id = self._world_id
        if not world_id:
            return OutboundMessage.system_msg(
                "当前未选择世界。先 /world start 或 /world load。", level="warn", session_id=sid,
            )
        world = self.storage.get_world(world_id)
        if world is None:
            return OutboundMessage.system_msg(
                f"当前世界不存在（可能已被删除）: {world_id}", level="warn", session_id=sid,
            )
        if world.get("status") == "ARCHIVED":
            return OutboundMessage.system_msg(
                f"世界已归档（结团）: {world_id}，只读不可再结团。", level="warn", session_id=sid,
            )
        if self.memory is None:
            return OutboundMessage.system_msg(
                "记忆后端未配置，无法执行终局固化。", level="warn", session_id=sid,
            )
        try:
            from src.agent.pipeline import prepare_manual_ending, run_ending_wrapup

            directive = prepare_manual_ending(
                self.storage, world_id, ending_type="BD",
            )
            ended = await run_ending_wrapup(
                self.storage, world_id, directive,
                memory=self.memory, llm=self.llm, worker=self.worker,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"主动结团失败 world={world_id}: {e}", exc_info=True)
            return OutboundMessage.system_msg(
                f"结团失败: {type(e).__name__}: {e}\n"
                f"世界保持活跃，可修复后重试 /world archive。",
                level="error", session_id=sid,
            )
        self._world_id = None  # 状态：结团归档后会话退回主菜单
        return self._render_ending_card(world_id, ended, sid)

    def _render_ending_card(self, world_id: str, ended, sid: str) -> OutboundMessage:
        """终局结算卡片：结局标签 + 终局演播文本 + 归档提示（CLI/Web 通用渲染）。"""
        from src.agent.narrator import ending_label

        header = (
            "========== 结 团 战 报 ==========\n"
            f"世界: {world_id}\n"
            f"结局: {ended.ending_type}（{ending_label(ended.ending_type)}）\n"
            "================================"
        )
        footer = "本次冒险已归档，返回主菜单。输入 /world start <世界ID> 开启新旅程。"
        return OutboundMessage.narrative(
            f"{header}\n\n{ended.narration}\n\n{footer}",
            session_id=sid, world_id=world_id,
        )

    # ============================================
    # 状态查询命令
    # ============================================

    async def _handle_status_cmd(self, sid: str) -> OutboundMessage:
        """显示当前世界状态：模组 / 轮次 / PC 实体快照。"""
        world_id = self._world_id
        if not world_id:
            return OutboundMessage.system_msg(
                "当前未选择世界。使用 /world start <世界ID> 或 /world load <世界ID>。",
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
        回档复用 src.memory.worker.rollback_world 编排，与后台固化共享该世界锁互斥；
        归档（已结团）世界同样允许回滚——终局轮是最新轮，撤销 >= N 即回到结团之前，
        回滚成功后自动把 status 从 ARCHIVED 恢复为 ACTIVE（结团撤销，可继续游玩）。
        """
        world_id = self._world_id
        if not world_id:
            return OutboundMessage.system_msg(
                "当前未选择世界。先 /world start 或 /world load。", level="warn", session_id=sid,
            )
        world = self.storage.get_world(world_id)
        # 状态：记录归档标记——回滚成功后若撤销了终局轮，则解除归档恢复活跃
        was_archived = world is not None and world.get("status") == "ARCHIVED"
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
            if was_archived:
                lines.append("（本世界已结团归档，回滚到终局轮之前会自动解除归档）")
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
        lines = [
            f"已回档到第 {target} 轮之前（撤销 >= {target} 的轮次，RAG 清理 {deleted} 条）",
        ]
        if was_archived:
            # 状态：仅当终局轮确被撤销才解除结团——归档世界只读无新轮，最新轮即终局轮；
            # /rollback latest+1 这类无操作回滚不解除归档，避免"没回滚却撤销结团"
            if self.storage.get_turn(world_id, latest) is None:
                self.storage.update_world(world_id, status="ACTIVE")
                lines.append("结团已撤销：世界恢复为活跃状态（ARCHIVED -> ACTIVE），可继续游玩")
            else:
                lines.append("未撤销终局轮，世界保持归档（结团）状态")
        return OutboundMessage.system_msg("\n".join(lines), session_id=sid)

    # ============================================
    # 记忆调试命令
    # ============================================

    async def _handle_memory_cmd(self, cmd: str, sid: str) -> OutboundMessage:
        """处理 /memory <查询> — 语义召回调试，直接走 Memory.search 带 world_id 过滤。"""
        world_id = self._world_id
        if not world_id:
            return OutboundMessage.system_msg(
                "当前未选择世界。先 /world start 或 /world load。", level="warn", session_id=sid,
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
        """处理 /card 系列命令：import / list / use / delete。

        /card import <来源>  解析 xlsx 角色卡并存入种子库（world_id 空，不绑定任何世界）
        /card list           列出种子库中的候选角色（供 /world start 或 /card use 选择）
        /card use <种子id>   把种子角色拷贝一份到当前世界并绑定为 PC
        /card delete <种子id> 删除种子库中的角色卡（不影响已拷贝进世界的 PC 副本）
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
            lines.append("用 /world start <世界ID> 创建世界后按提示绑定，或 /card use 加入当前世界。")
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
            lines.append("用法: /world start <世界ID> 后按提示输入角色卡名，或 /card use <种子id>。")
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
                    "当前未选择世界。先 /world start <世界ID> 创建，或 /world load <世界ID> 载入。",
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

        if sub == "delete":
            seed_id = parts[2].strip() if len(parts) > 2 else ""
            if not seed_id:
                return OutboundMessage.system_msg(
                    "用法: /card delete <种子id>\n使用 /card list 查看可用的种子角色。",
                    level="warn", session_id=sid,
                )
            try:
                from src.tools.card_store import delete_seed

                delete_seed(seed_id)
            except Exception as e:  # noqa: BLE001
                logger.error(f"种子卡删除失败: {e}")
                return OutboundMessage.system_msg(
                    f"删除种子卡失败: {type(e).__name__}: {e}",
                    level="error", session_id=sid,
                )
            return OutboundMessage.system_msg(
                f"种子卡已删除: {seed_id}", session_id=sid,
            )

        return OutboundMessage.system_msg(
            "用法:\n"
            "  /card import <来源>    - 导入 xlsx 角色卡到种子库（路径或 data/cards 内文件名）\n"
            "  /card list             - 列出种子库中的候选角色\n"
            "  /card use <种子id>     - 把种子角色拷贝到当前世界\n"
            "  /card delete <种子id>  - 删除种子库中的角色卡",
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
        lines.append("使用 /world start <世界ID> 创建世界。")
        return OutboundMessage.system_msg("\n".join(lines), session_id=sid)


# ============================================
# 辅助函数与帮助文本
# ============================================


# 用户自定义世界 id：仅字母/数字/下划线/连字符（不以连字符开头），长度 1~64
_WORLD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


_HELP_TEXT = (
    "可用命令:\n"
    "======= 世界 =======\n"
    "  /world list                - 列出所有世界\n"
    "  /world start <世界ID>      - 创建世界（交互输入模组名与角色卡名）\n"
    "  /world load <世界ID>       - 载入已有世界\n"
    "  /world archive             - 主动结团（BD 软结局）并归档当前世界\n"
    "  /world delete <世界ID>     - 删除世界（级联 + RAG 清理）\n"
    "  /module list               - 列出可绑定的模组文件\n"
    "======= 角色卡 =======\n"
    "  /card import <来源>        - 导入 xlsx 角色卡到种子库（路径或 data/cards 内文件名）\n"
    "  /card list                 - 列出种子库中的候选角色\n"
    "  /card use <种子id>         - 把种子角色拷贝到当前世界\n"
    "  /card delete <种子id>      - 删除种子库中的角色卡\n"
    "======= 游戏 =======\n"
    "  /status                    - 查看当前世界与 PC 状态\n"
    "  /rollback                  - 列出最近轮次\n"
    "  /rollback <N>              - 回档到第 N 轮之前（物理 + 语义双侧；归档世界回滚即解除结团）\n"
    "  /memory <查询>             - 语义召回当前世界的历史记忆\n"
    "======= 其他 =======\n"
    "  /help /h                   - 显示此帮助\n"
    "  /quit /q                   - 退出\n"
    "直接输入行动文本开始探索。"
)
