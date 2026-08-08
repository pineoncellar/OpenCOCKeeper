# -*- coding: utf-8 -*-
"""
@File     :   module/__init__.py
@Desc     :   模组文件层对外入口：目录、列表、安全解析、原文读取
@Note     :   与记忆 RAG / 存储状态分离——此层只负责"静态模组原文"的存取，
             不进入 world_id 隔离维度（模组文件为多世界共享的只读资产）
"""

from .loader import MODULES_DIR, ensure_modules_dir, list_modules, resolve
from .models import ModuleInfo
from .parsers import SUPPORTED_EXTS
from .reader import read_module

__all__ = [
    "MODULES_DIR",
    "ensure_modules_dir",
    "list_modules",
    "resolve",
    "read_module",
    "ModuleInfo",
    "SUPPORTED_EXTS",
]
