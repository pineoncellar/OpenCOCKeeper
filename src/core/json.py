# -*- coding: utf-8 -*-
"""
@File     :   json.py
@Desc     :   统一 JSON 序列化，供所有 JSON 列（tags/inventory/state_diff 等）复用
@Note     :   约定 ensure_ascii=False 保证中文落盘可读；sort_keys 保证输出稳定可比对
"""

import json
from datetime import date, datetime
from typing import Any, Optional


def _json_default(obj: Any) -> Any:
    # 兜底：datetime/date 转 ISO 字符串，避免 JSON 列写入时出现不可序列化对象
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# ============================================
# 通用 JSON 序列化
# ============================================


def dumps(obj: Any) -> str:
    """对象转紧凑 JSON 字符串：中文不转义、键稳定排序、去除多余空格省体积。

    键稳定排序对 state_diff 这类需要比对/取反的结构尤其重要，
    保证同一对象序列化结果始终一致。
    """
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def loads(text: Optional[str]) -> Any:
    """解析 JSON 字符串；空串或 None 返回 None，格式损坏直接抛错暴露问题。"""
    if text is None or text == "":
        return None
    return json.loads(text)


def loads_or(text: Optional[str], default: Any = None) -> Any:
    """宽容版解析：解析失败返回 default，供冷备/审计等"坏数据不致命"场景使用。

    正常业务写入路径请用严格版 loads，避免静默吞掉脏数据。
    """
    try:
        return loads(text)
    except (json.JSONDecodeError, TypeError):
        return default
