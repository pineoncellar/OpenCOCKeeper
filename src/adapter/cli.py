# -*- coding: utf-8 -*-
"""
@File     :   cli.py
@Desc     :   CLI 适配器 — 终端交互式 REPL：世界选择 / 回合管线 / 世界管理命令
@Note     :   继承 AbstractAdapter，通过 stdin/stdout 与玩家交互；窗口 id 仅此层存在，
             经 parse 映射为 world_id 后交 handle 处理；后台固化 Worker 由 runtime 挂接
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Optional

from src.core.log import get_logger

from .base import AbstractAdapter
from .protocol import InboundMessage, MessageType, OutboundMessage

logger = get_logger(__name__)


# ============================================
# ANSI 颜色（仅终端 isatty 时启用）
# ============================================

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_RED = "\033[31m"


def _color(text: str, code: str) -> str:
    """终端有 tty 才包 ANSI 码，否则返回纯文本（管道/测试环境不污染输出）。"""
    return f"{code}{text}{_RESET}" if sys.stdout.isatty() else text


_BANNER = (
    "\n"
    + _color("=" * 56, _CYAN)
    + "\n"
    + _color("  OpenCOCKeeper — CoC 7th AI 守秘人系统", _BOLD)
    + "\n"
    + _color("  CLI Adapter - 输入 /help 查看命令", _DIM)
    + "\n"
    + _color("=" * 56, _CYAN)
    + "\n"
)


# ============================================
# CLI 适配器
# ============================================


class CliAdapter(AbstractAdapter):
    """终端交互式跑团界面：主循环读取 stdin，解析为 InboundMessage 交 handle，再渲染输出。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    # ---- AbstractAdapter 接口实现 ----

    async def parse(self, raw_input: Any) -> InboundMessage:
        """将终端原始输入解析为 InboundMessage：/ 开头为系统命令，其余为玩家输入。"""
        text = str(raw_input).strip() if raw_input else ""
        routing = dict(
            platform="cli",
            channel_id="",
            user_id="local",
            world_id=self._world_id or "",
        )
        if not text:
            return InboundMessage(type="", text="", session_id=self.session_id, **routing)
        if text.startswith("/"):
            return InboundMessage.system_cmd(text, self.session_id, **routing)
        return InboundMessage.player_input(text, self.session_id, **routing)

    async def send(self, message: OutboundMessage) -> None:
        """将 OutboundMessage 渲染到终端。"""
        if message.type == MessageType.NARRATIVE:
            print()
            print(_color("-" * 50, _DIM))
            print(_color(message.text, _CYAN))
            print(_color("-" * 50, _DIM))

        elif message.type == MessageType.SYSTEM_MSG:
            level = message.data.get("level", "info")
            if level == "error":
                print(_color(f"[错误] {message.text}", _RED))
            elif level == "warn":
                print(_color(f"[提示] {message.text}", _YELLOW))
            else:
                print(_color(f"[信息] {message.text}", _DIM))

        elif message.type == MessageType.SESSION_INFO:
            d = message.data
            print()
            print(_color("-- 会话状态 ----------------", _BOLD))
            for k, v in d.items():
                if k == "text":
                    continue
                print(f"  {k}: {v}")
            print(_color("---------------------------", _DIM))

    # ---- 主循环 ----

    async def run_impl(self) -> None:
        """CLI 交互主循环：读入 -> parse -> handle -> send，/quit 退出。"""
        print(_BANNER)
        print(_color("  输入 /world start <模组名> 开始游戏，或 /world load <世界ID> 载入", _DIM))
        print(_color("  输入 /help 查看所有命令", _DIM))
        print()

        self._running = True
        while self._running:
            try:
                raw = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: input(f"{_color('输入', _BOLD)} > "),
                )
            except (KeyboardInterrupt, EOFError):
                print()
                break

            msg = await self.parse(raw)
            if not msg.type:
                continue  # 状态：空输入直接跳过

            if msg.type == MessageType.SYSTEM_CMD and msg.text.strip().lower() in ("/quit", "/q"):
                break  # 状态：退出主循环，run() finally 统一清理

            out = await self.handle(msg)
            await self.send(out)
            print()

        print(_color("\n感谢使用 OpenCOCKeeper！", _CYAN))
