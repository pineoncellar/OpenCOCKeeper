#- encoding: utf-8 -#
#
# @File     :   adapter.py
# @Desc     :   Web 适配器 — 浏览器 WebSocket 与核心管线的双向桥接（继承 AbstractAdapter）
# @Note     :   复用 base.py 全部命令路由（/world /status /rollback /card 等），
#              窗口/会话 id 仅本层存在，映射为 world_id 下传；两种驱动方式——
#              aiohttp /ws/game 处理器逐消息调 handle_inbound 直驱，
#              或 submit 入队后跑 run() 走模板方法（测试/独立模式）。
#              落库钩子 _on_turn_committed 重写：触发 worker 固化 + 广播 state_diff
#              给前端刷新角色面板（血条/背包/检定卡片）
#

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from src.core.log import get_logger

from ..base import AbstractAdapter
from ..protocol import InboundMessage, MessageType, OutboundMessage
from .protocol import (
    build_narrative_frame,
    build_session_frame,
    build_state_diff_frame,
    build_system_frame,
)

logger = get_logger(__name__)


class WebAdapter(AbstractAdapter):
    """WebSocket 适配器：浏览器 JSON 帧 <-> 统一消息协议。

    每个连接对应一个 session_id（窗口 id），持有唯一 world_id 指针；
    _world_id 由 base 层命令（/world start|load|archive 等）维护。
    """

    def __init__(
        self,
        storage: Any = None,
        memory: Any = None,
        worker: Any = None,
        *,
        llm: Any = None,
        session_id: str = "web-default",
        ws: Any = None,
    ) -> None:
        super().__init__(
            storage=storage, memory=memory, worker=worker,
            llm=llm, session_id=session_id,
        )
        # 状态：绑定的 WebSocket 连接（aiohttp WebSocketResponse，None 时 send 丢弃）
        self._ws = ws
        # 状态：服务端直驱模式的入站队列（run() 模板方法读取）
        self._inbound_queue: asyncio.Queue = asyncio.Queue()
        self._quit = False

    # ====================================================================
    # AbstractAdapter 接口实现
    # ====================================================================

    async def parse(self, raw_input: Any) -> InboundMessage:
        """JSON 帧 -> InboundMessage：type 决定 player_input / system_cmd。

        入站帧形如 {"type": "player_input"|"system_cmd", "text": "..."}；
        world_id 以会话当前指针 _world_id 填充（与 CLI 一致）。
        """
        if isinstance(raw_input, str):
            try:
                frame = json.loads(raw_input)
            except json.JSONDecodeError:
                frame = {"type": "player_input", "text": raw_input}
        else:
            frame = raw_input or {}
        text = str(frame.get("text") or "").strip()
        ftype = str(frame.get("type") or "").strip()
        routing = dict(
            platform="web",
            channel_id="",
            user_id=frame.get("user_id", "web-user"),
            world_id=self._world_id or "",
        )
        if ftype == MessageType.SYSTEM_CMD or text.startswith("/"):
            return InboundMessage.system_cmd(text, self.session_id, **routing)
        return InboundMessage.player_input(text, self.session_id, **routing)

    async def send(self, message: OutboundMessage) -> None:
        """OutboundMessage -> JSON 帧推送到 WebSocket；未绑定连接时丢弃。"""
        if self._ws is None or self._ws.closed:
            return
        if message.type == MessageType.NARRATIVE:
            frame = build_narrative_frame(
                message.text, world_id=message.world_id, session_id=self.session_id,
            )
        elif message.type == MessageType.SESSION_INFO:
            frame = build_session_frame(message.data, session_id=self.session_id)
        else:
            frame = build_system_frame(
                message.text,
                level=str(message.data.get("level", "info")),
                world_id=message.world_id, session_id=self.session_id,
            )
        await self._ws.send_json(frame)

    async def run_impl(self) -> None:
        """主循环（服务端直驱时通常不调用）：读取入站队列，逐个处理并回发。"""
        self._running = True
        while self._running and not self._quit:
            try:
                raw = await asyncio.wait_for(self._inbound_queue.get(), timeout=30)
            except asyncio.TimeoutError:
                continue  # 状态：空闲超时继续轮询，便于退出检测
            if raw is None:  # 状态：None 为停止信号
                break
            try:
                await self.handle_inbound(raw)
            except Exception as e:  # noqa: BLE001  单条消息失败不中断主循环
                logger.error(f"Web 消息处理异常: {e}", exc_info=True)

    # ====================================================================
    # 服务端直驱接口
    # ====================================================================

    async def handle_inbound(self, raw: Any) -> Optional[OutboundMessage]:
        """处理单条入站帧（aiohttp WS 处理器每收到一条消息调用一次）。

        空文本/空类型返回 None 不发送；正常经 handle() 路由并回发响应。
        系统命令执行后若已选中世界，额外推送 session_info 刷新帧——
        使前端角色面板在 /world load|start|rollback 后即时同步实体快照。
        """
        msg = await self.parse(raw)
        if not msg.type or not msg.text:
            return None
        # 状态：/quit 仅对 Web 连接关闭连接（不退出进程）
        if msg.type == MessageType.SYSTEM_CMD and msg.text.strip().lower() in ("/quit", "/q"):
            await self._send_system("连接关闭", level="info")
            self._quit = True
            return None
        out = await self.handle(msg)
        await self.send(out)
        # 状态：系统命令后推送会话刷新帧，保证角色面板同步
        if msg.type == MessageType.SYSTEM_CMD and self._world_id:
            await self._send_session_refresh()
        return out

    def submit(self, raw: Any) -> None:
        """把入站消息推入队列（配合 run() 模板方法使用）。"""
        self._inbound_queue.put_nowait(raw)

    def bind(self, ws: Any) -> None:
        """绑定 WebSocket 连接（连接建立后调用，替代构造时注入）。"""
        self._ws = ws

    def close_connection(self) -> None:
        """标记连接关闭，_quit 置位。"""
        self._quit = True

    # ====================================================================
    # 落库钩子重写：触发固化 + 广播状态增量
    # ====================================================================

    async def _on_turn_committed(self, world_id: str, turn_num: int) -> None:
        """落库后：先触发后台固化 Worker，再向前端广播 state_diff 刷新角色面板。"""
        # 状态：复用基类逻辑触发固化（失败不抛给管线）
        await super()._on_turn_committed(world_id, turn_num)
        try:
            await self._broadcast_state_diff(world_id, turn_num)
        except Exception as e:  # noqa: BLE001  推送失败不影响本轮交付
            logger.warning(f"state_diff 广播失败 world={world_id} turn={turn_num}: {e}")

    async def _broadcast_state_diff(self, world_id: str, turn_num: int) -> None:
        """推送本轮落库后的状态增量：叙事/检定权威副本 + 变更后实体快照。

        前端据此渲染检定卡片、刷新血条/理智/背包，无需再发 GET。
        """
        if self._ws is None or self._ws.closed:
            return
        # 状态：读本轮 context_data（叙事 + 检定权威区）
        turn = self.storage.get_turn(world_id, turn_num)
        ctx = (turn or {}).get("context_data") or {}
        narration = ctx.get("assistant") or ""
        checks = ctx.get("checks") or []
        # 状态：取变更后实体快照（PC/NPC 物理状态，供角色面板刷新）
        entities = self.storage.get_entities(world_id, entity_type="PC")
        frame = build_state_diff_frame(
            world_id=world_id,
            turn_num=turn_num,
            state_diff=(turn or {}).get("state_diff") or {},
            narration=narration,
            checks=checks,
            entities=[_entity_brief(e) for e in entities],
        )
        await self._ws.send_json(frame)

    # ====================================================================
    # 会话状态查询（供前端加载角色面板）
    # ====================================================================

    def world_status(self) -> dict:
        """当前世界 + 全部角色实体的轻量快照（前端 /status 用）。"""
        if not self._world_id:
            return {"world_id": None, "entities": []}
        world = self.storage.get_world(self._world_id) or {}
        entities = self.storage.get_entities(self._world_id, entity_type="PC")
        return {
            "world_id": self._world_id,
            "status": world.get("status", "ACTIVE"),
            "module_name": world.get("module_name"),
            "entities": [_entity_brief(e) for e in entities],
        }

    async def _send_system(self, text: str, *, level: str = "info") -> None:
        """发系统消息帧（内部工具，避免绕道 handle）。"""
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_json(build_system_frame(text, level=level))

    async def _send_session_refresh(self) -> None:
        """推送会话刷新帧：当前世界 + 角色快照，供前端面板即时同步。"""
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_json(
                build_session_frame(self.world_status(), session_id=self.session_id)
            )


# ====================================================================
# 内部工具
# ====================================================================


def _entity_brief(entity: dict) -> dict:
    """实体轻量摘要：供前端血条/背包渲染，剔除大字段（技能/背景）。"""
    return {
        "id": entity.get("id"),
        "name": entity.get("name"),
        "type": entity.get("type"),
        "hp": entity.get("hp"),
        "hp_max": entity.get("hp_max"),
        "san": entity.get("san"),
        "san_max": entity.get("san_max"),
        "mp": entity.get("mp"),
        "mp_max": entity.get("mp_max"),
        "tags": entity.get("tags") or [],
        "inventory": entity.get("inventory") or [],
    }
