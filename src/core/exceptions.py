# -*- coding: utf-8 -*-
"""
@File     :   exceptions.py
@Desc     :   统一异常体系，供配置、数据库与存储层精确捕获，避免上层裸接 sqlite3.Error
@Note     :   OpenCOCKeeperError 为全库基类，新增业务异常应继承其下再细化
"""


class OpenCOCKeeperError(Exception):
    """全库异常基类，所有底层/业务异常都继承它。"""


class StorageError(OpenCOCKeeperError):
    """存储层通用错误：连接失败、SQL 执行失败、迁移异常等。"""


class WorldNotFoundError(StorageError):
    """指定的 world_id 不存在。"""


class EntityNotFoundError(StorageError):
    """指定的实体（PC/NPC/SCENE/ITEM）不存在。"""


class TurnNotFoundError(StorageError):
    """指定的轮次记录不存在，回档时常用。"""


class UndoError(StorageError):
    """回档失败：state_diff 缺失、或已回滚到最旧轮次无法继续。"""


class MemoryOperationError(OpenCOCKeeperError):
    """记忆固化/检索流程失败：LLM 提炼失败、返回内容不可解析、后端写入异常等。

    约定：固化接口不做任何降级兜底，失败即抛本异常并保持"未固化"进度，
    上层事件处理流可捕获后整批重试（名称避开 Python 内置 MemoryError）。
    """


class ModuleFileMissingError(OpenCOCKeeperError):
    """模组文件缺失或文件名非法：世界初始化绑定/读取时找不到对应模组文件。

    名称刻意避开 Python 内置 ModuleNotFoundError；调用方捕获后应
    向用户提示"data/modules 下缺少该模组文件"，而非视为 import 错误。
    """


class UnsupportedFormatError(OpenCOCKeeperError):
    """模组文件格式不受支持：扩展名不在解析器白名单内（如 v1 未接 pdf/docx）。"""
