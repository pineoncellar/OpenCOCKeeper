# -*- coding: utf-8 -*-
"""轻量级日志模块。

提供分级 Logger 工厂 ``get_logger()``：

- 控制台输出（stdout）；
- 按日滚动的文件输出（``logs/<YYYY-MM-DD>.log``，默认 10MB、保留 7 份轮转）；
- ``config.yaml -> project.debug = true`` 时自动使用 DEBUG 级别；
- WARNING 及以上级别自动附带 ``module:lineno``，便于定位问题。

相比旧架构 ``glyphkeeper/src/tools/config.py`` 的日志部分：

- 改为在根 logger 上挂载 handler，子 logger 通过 ``propagate`` 复用，
  避免每个 logger 各自打开一份文件句柄；
- 用两个预构建 Formatter 替代运行期修改 ``_fmt`` 的写法。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .config import PROJECT_ROOT, get_settings

# 默认日志目录（如需自定义，可在初始化时调整 LOG_DIR）
LOG_DIR: Path = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_MAX_BYTES: int = 10 * 1024 * 1024  # 10MB
DEFAULT_BACKUP_COUNT: int = 7

_FORMAT_INFO = "[%(asctime)s] [%(levelname)s] [%(name)s] - %(message)s"
_FORMAT_WARN = "[%(asctime)s] [%(levelname)s] [%(name)s] [%(module)s:%(lineno)d] - %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _ConditionalFormatter(logging.Formatter):
    """INFO 及以下使用简洁格式；WARNING 及以上附带文件名与行号。"""

    def __init__(self) -> None:
        super().__init__(datefmt=_DATEFMT)
        self._info_fmt = logging.Formatter(_FORMAT_INFO, datefmt=_DATEFMT)
        self._warn_fmt = logging.Formatter(_FORMAT_WARN, datefmt=_DATEFMT)

    def format(self, record: logging.LogRecord) -> str:
        fmt = self._warn_fmt if record.levelno >= logging.WARNING else self._info_fmt
        return fmt.format(record)


_root_configured = False


def _resolve_level() -> int:
    """从配置解析日志级别：debug=true -> DEBUG，否则 INFO。"""
    try:
        debug = get_settings().get("project.debug", False)
        return logging.DEBUG if debug else logging.INFO
    except Exception:  # noqa: BLE001  配置尚未就绪时回退 INFO
        return logging.INFO


def _ensure_root_handlers(level: int) -> None:
    """在根 logger 上挂载控制台 + 文件 handler（幂等，仅执行一次）。"""
    global _root_configured
    if _root_configured:
        return

    root = logging.getLogger()
    root.setLevel(level)
    formatter = _ConditionalFormatter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    today = datetime.now().strftime("%Y-%m-%d")
    file_handler = RotatingFileHandler(
        LOG_DIR / f"{today}.log",
        maxBytes=DEFAULT_MAX_BYTES,
        backupCount=DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _root_configured = True


_loggers: dict[str, logging.Logger] = {}


def get_logger(name: str = "opencockeeper", level: Optional[int] = None) -> logging.Logger:
    """获取（并缓存）分级 Logger。

    Args:
        name: Logger 名称，建议使用模块路径（如 ``opencockeeper.core``）。
        level: 显式指定级别；缺省时按 ``project.debug`` 自动选择。
    """
    effective = level or _resolve_level()
    _ensure_root_handlers(effective)
    if name not in _loggers:
        logger = logging.getLogger(name)
        logger.setLevel(effective)
        logger.propagate = True
        _loggers[name] = logger
    return _loggers[name]


_llm_trace_logger: Optional[logging.Logger] = None
_llm_trace_world_loggers: dict[str, logging.Logger] = {}


def get_llm_trace_logger(world_id: Optional[str] = None) -> logging.Logger:
    """获取独立的 LLM 交互 trace logger（仅写文件，不污染根控制台）。

    每次 LLM 请求/响应、工具调用请求/结果写入 ``logs/llm-<date>.log``
    （DEBUG 级、UTF-8、按日滚动），供提示词与 Function Calling 调试；
    与主日志分离，避免完整 prompt/响应刷屏终端。

    当提供 world_id 时，返回 per-world 专属 logger，写入
    ``logs/llm-<world_id>-<date>.log``，方便按世界隔离审计日志。
    per-world logger 与通用 logger 互不干扰，均为独立文件独立 handler。
    """
    if world_id:
        return _get_world_llm_trace_logger(world_id)
    return _get_generic_llm_trace_logger()


def _get_generic_llm_trace_logger() -> logging.Logger:
    """通用 trace logger：logs/llm-<date>.log（无 world_id 限定）。"""
    global _llm_trace_logger
    if _llm_trace_logger is not None:
        return _llm_trace_logger
    lgr = logging.getLogger("opencockeeper.llm_trace")
    lgr.setLevel(logging.DEBUG)
    lgr.propagate = False
    today = datetime.now().strftime("%Y-%m-%d")
    handler = RotatingFileHandler(
        LOG_DIR / f"llm-{today}.log",
        maxBytes=DEFAULT_MAX_BYTES,
        backupCount=DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(_ConditionalFormatter())
    lgr.addHandler(handler)
    _llm_trace_logger = lgr
    return lgr


def _get_world_llm_trace_logger(world_id: str) -> logging.Logger:
    """per-world trace logger：logs/llm-<world_id>-<date>.log。

    按 world_id 缓存，同一世界复用同一 logger 实例；
    文件 handler 独立，不与通用 trace 混写。
    """
    if world_id in _llm_trace_world_loggers:
        return _llm_trace_world_loggers[world_id]
    name = f"opencockeeper.llm_trace.world.{world_id}"
    lgr = logging.getLogger(name)
    lgr.setLevel(logging.DEBUG)
    lgr.propagate = False
    today = datetime.now().strftime("%Y-%m-%d")
    safe_id = world_id.replace("/", "_").replace("\\", "_").replace(":", "_")
    handler = RotatingFileHandler(
        LOG_DIR / f"llm-{safe_id}-{today}.log",
        maxBytes=DEFAULT_MAX_BYTES,
        backupCount=DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(_ConditionalFormatter())
    lgr.addHandler(handler)
    _llm_trace_world_loggers[world_id] = lgr
    return lgr


def setup_logging(level: Optional[int] = None) -> None:
    """显式初始化根日志（幂等）。通常在应用启动时调用一次。"""
    _ensure_root_handlers(level or _resolve_level())
