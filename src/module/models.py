# -*- coding: utf-8 -*-
"""
@File     :   models.py
@Desc     :   模组文件层数据模型：ModuleInfo 供列表/绑定/读取使用
@Note     :   frozen dataclass 只读，module_name 一律为文件全名（含扩展名）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModuleInfo:
    """可用模组文件的元信息（由 list_modules 扫描得出）。

    format 为归一化格式名（如 "markdown"），由扩展名映射而来；
    module_name 即文件名，世界绑定与读取都直接用它。
    """

    module_name: str      # 文件名全名，如 "lightless-horizon.docx"
    path: Path            # 文件绝对路径
    format: str           # 归一化格式：pdf / docx
    size: int             # 文件字节数
    mtime: float          # 最近修改时间戳
