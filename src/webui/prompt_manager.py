# -*- coding: utf-8 -*-
"""
@File     :   prompt_manager.py
@Desc     :   提示词管理 API 处理器 — prompts.yaml 读取 / YAML 校验 / 原子写入 + 热重载
@Note     :   与 config_manager 对称：prompts.yaml 不含敏感信息（api_key 在 providers.ini），
             可安全全文暴露；写入走临时文件 + rename 原子替换（复用 config_manager 辅助），
             写入成功后立即 reload_prompts() 热重载——提示词与业务配置不同，各使用点每次
             动态 get_prompt，允许运行时热生效，无需重启；providers.ini 永不在此层读写。
"""

from __future__ import annotations

from typing import Any, Dict, List

import yaml
from aiohttp import web

from src.core.log import get_logger
from src.core.prompts import (
    PROMPTS_FILE,
    _file_data,
    reload_prompts,
)
from src.webui.config_manager import _atomic_write, _err, _read_json

logger = get_logger(__name__)


# ====================================================================
# 读取
# ====================================================================


async def api_get_prompts(request: web.Request) -> web.Response:
    """返回 prompts.yaml 的结构化 dict + 全部可用提示词 key 清单。

    prompts 为文件嵌套 dict；keys 只来自文件实际存在的提示词（点号路径，保序），
    供前端表单逐条渲染——缺失即视为未配置，运行时 get_prompt 会抛 PromptError。
    """
    file_data = _file_data()
    keys: List[str] = _collect_file_keys(file_data)
    return web.json_response({"prompts": file_data, "keys": keys})


async def api_get_prompts_raw(request: web.Request) -> web.Response:
    """返回 prompts.yaml 原始文本，供前端 YAML 编辑器直接展示。"""
    try:
        text = PROMPTS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _err(404, "PromptsNotFound", "prompts.yaml 不存在，无法提供原始 YAML")
    return web.json_response({"yaml": text})


# ====================================================================
# 校验与保存
# ====================================================================


async def api_validate_prompts(request: web.Request) -> web.Response:
    """校验前端提交的 YAML 文本：格式合法性（能 safe_load 且为映射），不写入磁盘。"""
    body = await _read_json(request)
    yaml_text = (body or {}).get("yaml") if isinstance(body, dict) else None
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return _err(400, "BadRequest", "请求体需含 yaml 文本字段")
    ok, errors = _validate_yaml(yaml_text)
    if not ok:
        return web.json_response({"ok": False, "errors": errors}, status=400)
    return web.json_response({"ok": True, "errors": []})


async def api_save_prompts(request: web.Request) -> web.Response:
    """保存前端逐条表单提交的提示词：扁平 dict → 嵌套 YAML → 原子写入 + 热重载。

    body 形如 {"prompts": {"director.system": "...", ...}}；
    值为空串的 key 视为删除（不写入文件），运行时该 key 缺失将抛 PromptError。
    """
    body = await _read_json(request)
    flat = (body or {}).get("prompts") if isinstance(body, dict) else None
    if not isinstance(flat, dict):
        return _err(400, "BadRequest", "请求体需含 prompts 对象字段")
    nested = _unflatten({k: v for k, v in flat.items() if str(v).strip()})
    yaml_text = yaml.safe_dump(
        nested, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    try:
        _atomic_write(PROMPTS_FILE, yaml_text)
        reload_prompts()  # 状态：写入后立即热重载，运行时提示词即刻生效
    except OSError as e:
        logger.error("prompts.yaml 写入失败: %s", e)
        return _err(500, "WriteError", f"写入失败: {e}")
    logger.info("prompts.yaml 已保存并热重载")
    return web.json_response({"ok": True, "message": "提示词已保存并热重载，立即生效"})


async def api_save_prompts_raw(request: web.Request) -> web.Response:
    """保存前端 YAML 全文：先校验再原子写入 + 热重载（YAML 模式用）。"""
    body = await _read_json(request)
    yaml_text = (body or {}).get("yaml") if isinstance(body, dict) else None
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return _err(400, "BadRequest", "请求体需含 yaml 文本字段")
    ok, errors = _validate_yaml(yaml_text)
    if not ok:
        return web.json_response({"ok": False, "errors": errors}, status=400)
    try:
        _atomic_write(PROMPTS_FILE, yaml_text)
        reload_prompts()  # 状态：写入后立即热重载
    except OSError as e:
        logger.error("prompts.yaml 写入失败: %s", e)
        return _err(500, "WriteError", f"写入失败: {e}")
    logger.info("prompts.yaml 已保存并热重载")
    return web.json_response({"ok": True, "message": "提示词已保存并热重载，立即生效"})


async def api_reload_prompts(request: web.Request) -> web.Response:
    """仅重载 prompts.yaml（丢弃缓存，重读磁盘），不写文件。"""
    reload_prompts()
    logger.info("prompts.yaml 已重新加载")
    return web.json_response({"ok": True, "message": "已重新加载 prompts.yaml"})


# ====================================================================
# 内部：校验 / 扁平化辅助
# ====================================================================


def _validate_yaml(yaml_text: str) -> tuple:
    """校验 YAML 文本，返回 (ok, errors)。

    从简务实：能 safe_load 且顶层为映射即可（提示词 key 不设白名单，允许用户自定义）。
    """
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return False, [f"YAML 语法错误: {e}"]
    if not isinstance(parsed, dict):
        return False, ["YAML 顶层必须是映射（对象）"]
    return True, []


def _collect_file_keys(data: Any, prefix: str = "") -> List[str]:
    """递归收集嵌套 dict 的全部叶节点点号路径（文件实际存在的提示词 key）。"""
    keys: List[str] = []
    if not isinstance(data, dict):
        return keys
    for k, v in data.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            keys.extend(_collect_file_keys(v, path))
        else:
            keys.append(path)
    return keys


def _unflatten(flat: Dict[str, Any]) -> Dict[str, Any]:
    """扁平点号路径 dict → 嵌套 dict（如 {"a.b.c": 1} → {"a": {"b": {"c": 1}}}）。"""
    root: Dict[str, Any] = {}
    for path, value in flat.items():
        node = root
        parts = str(path).split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return root
