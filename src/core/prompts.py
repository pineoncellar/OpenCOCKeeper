# -*- coding: utf-8 -*-
"""
@File     :   prompts.py
@Desc     :   LLM 提示词加载器——全部提示词外置到 prompts.yaml（可热重载）
@Note     :   与 config.py 对称：懒加载单例 + 点号路径读取 + 单一事实源；
             get_prompt(key, **kwargs) 支持 str.format 模板填充（如 memory.recap 的 {max}/{old}/{new}）；
             提示词唯一真源为根目录 prompts.yaml——文件缺失或某 key 缺失时抛 PromptError
             （除非调用方显式传入 default）；
             reload_prompts() 强制重读，提示词迭代无需重启
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .config import PROJECT_ROOT
from .exceptions import OpenCOCKeeperError

PROMPTS_FILE: Path = PROJECT_ROOT / "prompts.yaml"


class PromptError(OpenCOCKeeperError):
    """提示词缺失（prompts.yaml 无该 key 且未传显式 default 时抛出）。"""


def _warn(msg: str) -> None:
    """日志系统初始化前的早期告警，直接输出到 stderr（对齐 config._warn）。"""
    sys.stderr.write(f"[Prompts] {msg}\n")



# ====================================================================
# 文件加载与缓存
# ====================================================================

def _load_prompts_yaml() -> Dict[str, Any]:
    """读取 prompts.yaml；文件缺失或解析失败返回空 dict（后续 get_prompt 抛 PromptError）。"""
    if not PROMPTS_FILE.exists():
        _warn(f"未找到 {PROMPTS_FILE}，get_prompt 将抛 PromptError")
        return {}
    try:
        with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        _warn(f"无法读取 prompts.yaml: {e}，get_prompt 将抛 PromptError")
        return {}


def _get_file_value(key: str, data: Dict[str, Any]) -> Optional[str]:
    """按点号路径从文件数据中取值；路径中断或值非 str 返回 None。"""
    node: Any = data
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node if isinstance(node, str) else None


# ── 全局单例（懒加载缓存整份文件，避免逐 key 读盘） ──
_cache: Optional[Dict[str, Any]] = None


def _file_data() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        _cache = _load_prompts_yaml()
    return _cache


# ====================================================================
# 公共 API
# ====================================================================

def get_prompt(key: str, default: Optional[str] = None, **kwargs: Any) -> str:
    """按点号路径读取提示词，支持 str.format 模板填充。

    解析优先级：prompts.yaml 文件值 → 显式 default；
    两层都缺失抛 PromptError（调用方若不希望抛错，请传入 default）。
    仅当传入 kwargs 时才执行 .format；格式化失败（如字面花括号）回退返回原文。
    """
    text: Optional[str] = _get_file_value(key, _file_data())
    if text is None:
        text = default
    if text is None:
        raise PromptError(f"未知提示词 key: {key}")
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError) as e:
        _warn(f"提示词 {key} 模板格式化失败: {e}，返回未格式化原文")
        return text


def get_tool_desc(name: str) -> str:
    """Function Calling 工具描述快捷入口（等价 get_prompt(f\"tools.{name}\")）。"""
    return get_prompt(f"tools.{name}")


def reload_prompts() -> None:
    """强制重新加载 prompts.yaml（修改文件后调用，无需重启）。"""
    global _cache
    _cache = _load_prompts_yaml()


# 提供与 config 一致的入口别名，便于统一调用
load_prompts = _load_prompts_yaml
