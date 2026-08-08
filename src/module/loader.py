# -*- coding: utf-8 -*-
"""
@File     :   loader.py
@Desc     :   模组文件层核心：目录确保、可用模组扫描、文件名安全解析（唯一路径出口）
@Note     :   MODULES_DIR 为模块级变量，测试可 monkeypatch 指向临时目录整体替换；
             resolve 是本层所有读取/列表的必经校验点，任何穿越/非法名在此被拒
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ..core.config import PROJECT_ROOT, get_settings
from ..core.exceptions import ModuleFileMissingError, UnsupportedFormatError
from .models import ModuleInfo
from .parsers import SUPPORTED_EXTS, normalize_format

# 模组原文目录（config.storage.modules_dir，相对项目根则拼到项目根下）
MODULES_DIR: Path = Path(str(get_settings().get("storage.modules_dir", "data/modules")))
if not MODULES_DIR.is_absolute():
    MODULES_DIR = PROJECT_ROOT / MODULES_DIR

# 禁止的字符/前缀，从源头防路径穿越与隐藏文件  # 状态：命名约束
_FORBIDDEN_CHARS = ("/", "\\")
_FORBIDDEN_PREFIX = (".")


# ============================================
# 目录与扫描
# ============================================


def ensure_modules_dir() -> Path:
    """确保模组目录存在并返回其绝对路径（不存在则自动创建）。"""
    MODULES_DIR.mkdir(parents=True, exist_ok=True)
    return MODULES_DIR


def list_modules() -> List[ModuleInfo]:
    """扫描模组目录下全部受支持文件，按文件名排序返回。

    仅收录非隐藏且扩展名在白名单内的普通文件；解析能力由 parsers 注册表决定。
    """
    d = ensure_modules_dir()
    infos: List[ModuleInfo] = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.name.startswith(_FORBIDDEN_PREFIX):
            continue
        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTS:
            continue
        stat = p.stat()
        infos.append(
            ModuleInfo(
                module_name=p.name,
                path=p.resolve(),
                format=normalize_format(ext),
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
        )
    return infos


# ============================================
# 文件名校验与路径解析
# ============================================


def validate_name(module_name: str) -> str:
    """纯文本层面的文件名校验，非法即抛 ModuleFileMissingError。

    检查项：非空、不含路径分隔符、不以点开头、扩展名在白名单内。
    这里只校验"名字"本身，文件是否真实存在由 resolve 兜底。
    """
    name = str(module_name).strip()
    if not name:
        raise ModuleFileMissingError("模组名为空，世界初始化必须绑定模组文件")
    if any(c in name for c in _FORBIDDEN_CHARS):
        raise ModuleFileMissingError(f"模组名不能含路径分隔符: {name!r}")
    if name.startswith(_FORBIDDEN_PREFIX):
        raise ModuleFileMissingError(f"模组名不能以点开头: {name!r}")
    if Path(name).suffix.lower() not in SUPPORTED_EXTS:
        raise UnsupportedFormatError(
            f"不支持的模组格式: {name!r}，当前支持 {sorted(SUPPORTED_EXTS)}"
        )
    return name


def resolve(module_name: str) -> Path:
    """校验并返回模组文件的绝对路径；非法名或文件不存在一律抛异常。

    这是文件层唯一的路径出口：先过 validate_name，再做 resolve + is_relative_to
    双保险防穿越，最后确认文件真实存在。读取与绑定都必须经此函数。
    """
    name = validate_name(module_name)
    base = ensure_modules_dir().resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base):
        raise ModuleFileMissingError(f"模组路径越界，已拒绝: {name!r}")
    if not path.is_file():
        raise ModuleFileMissingError(
            f"模组文件不存在: {name}（请先放入 {MODULES_DIR} 目录）"
        )
    return path
