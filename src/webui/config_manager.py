#- encoding: utf-8 -#
#
# @File     :   config_manager.py
# @Desc     :   配置管理 API 处理器 — config.yaml 读取 / YAML 校验 / 原子写入
# @Note     :   config.yaml 不含敏感信息（api_key 在 providers.ini，已 gitignore），
#              可安全全文暴露；写入走临时文件 + rename 原子替换，写入后不热重载——
#              运行时热重载可能导致正在运行的 Director/Narrator 读到不一致配置，
#              故约定"保存后重启生效"。providers.ini 永不在此层读写。
#

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from aiohttp import web

from src.core.config import CONFIG_FILE
from src.core.log import get_logger

logger = get_logger(__name__)

# 配置顶层必填段：缺失即校验失败
_REQUIRED_TOP_LEVEL = ("project", "model_tiers", "storage", "adapter")

# 允许保存的顶层段白名单（防止误删项目无关键；未列出的段原样保留）
# 说明：白名单内字段可被表单编辑，未列出的键不做删除/改动，仅透传


# ====================================================================
# 读取
# ====================================================================


async def api_get_config(request: web.Request) -> web.Response:
    """返回当前 config.yaml 的结构化 dict（脱敏：不含 providers.ini 任何内容）。"""
    data = _read_config_yaml()
    return web.json_response({"config": data})


async def api_get_config_raw(request: web.Request) -> web.Response:
    """返回 config.yaml 原始文本，供前端 YAML 编辑器直接展示。"""
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _err(404, "ConfigNotFound", "config.yaml 不存在")
    return web.json_response({"yaml": text})


# ====================================================================
# 校验与保存
# ====================================================================


async def api_validate_config(request: web.Request) -> web.Response:
    """校验前端提交的 YAML 文本：格式合法性 + 顶层必填段，不写入磁盘。"""
    body = await _read_json(request)
    yaml_text = (body or {}).get("yaml") if isinstance(body, dict) else None
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return _err(400, "BadRequest", "请求体需含 yaml 文本字段")
    ok, errors = _validate_yaml(yaml_text)
    if not ok:
        return web.json_response({"ok": False, "errors": errors}, status=400)
    return web.json_response({"ok": True, "errors": []})


async def api_save_config(request: web.Request) -> web.Response:
    """保存前端提交的 YAML：先校验再原子写入 config.yaml，约定重启后生效。"""
    body = await _read_json(request)
    yaml_text = (body or {}).get("yaml") if isinstance(body, dict) else None
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return _err(400, "BadRequest", "请求体需含 yaml 文本字段")
    ok, errors = _validate_yaml(yaml_text)
    if not ok:
        return web.json_response({"ok": False, "errors": errors}, status=400)
    try:
        _atomic_write(CONFIG_FILE, yaml_text)
    except OSError as e:
        logger.error("config.yaml 写入失败: %s", e)
        return _err(500, "WriteError", f"写入失败: {e}")
    logger.info("config.yaml 已保存（重启后生效）")
    return web.json_response(
        {"ok": True, "message": "配置已保存，重启后生效"}
    )


# ====================================================================
# 内部：读取 / 校验 / 原子写入
# ====================================================================


def _read_config_yaml() -> Dict[str, Any]:
    """读取 config.yaml 为 dict；不存在或解析失败返回空 dict。"""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.error("config.yaml 解析失败: %s", e)
        return {}


def _validate_yaml(yaml_text: str) -> tuple:
    """校验 YAML 文本，返回 (ok, errors)。

    errors 为错误描述列表，可能含多处（语法错 + 缺段）；空列表表示通过。
    校验规则从简务实：能 safe_load、顶层为 dict、必填段存在即可。
    """
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return False, [f"YAML 语法错误: {e}"]
    if not isinstance(parsed, dict):
        return False, ["YAML 顶层必须是映射（对象）"]
    errors: List[str] = []
    for key in _REQUIRED_TOP_LEVEL:
        if key not in parsed:
            errors.append(f"缺少必填段: {key}")
    return (not errors), errors


def _atomic_write(path: Path, content: str) -> None:
    """原子写入：写临时文件再 rename 替换，防写入中途崩溃损坏 config.yaml。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".config-", suffix=".yaml.tmp"
    )
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)
        Path(tmp_name).replace(path)
    except Exception:
        # 状态：失败时清理临时文件，避免残留
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ====================================================================
# 内部工具
# ====================================================================


def _err(status: int, code: str, message: str) -> web.Response:
    """统一错误响应：{error: {code, message}}。"""
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


async def _read_json(request: web.Request) -> Any:
    """读取请求体 JSON，失败返回 None（调用方按空 body 处理）。"""
    try:
        return await request.json()
    except Exception:  # noqa: BLE001  非 JSON body 按空处理
        return None
