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


class EmptyUpdateError(OpenCOCKeeperError):
    """状态更新输入三段（检定/数值/背包）全空，无任何可执行操作。"""


class SkillNotFoundError(OpenCOCKeeperError):
    """检定项（技能/属性/理智）无法从实体属性表解析。"""


class InvalidDiceExpressionError(OpenCOCKeeperError):
    """骰子表达式非法：SC 表达式或伤害/治疗表达式无法解析。"""


class ItemNotFoundError(OpenCOCKeeperError):
    """背包移除目标不存在：items_to_remove 中的名称不在实体背包内。"""


class ConflictingInputError(OpenCOCKeeperError):
    """输入互斥冲突：如 san_change 与 san_sc_expression 同时传入。"""


class AgentLoopError(OpenCOCKeeperError):
    """主 Agent 决策闭环失败：LLM 请求失败、未产出决策文本等，回合无法继续。"""


class NarratorError(OpenCOCKeeperError):
    """润色 Agent（Narrator）演播失败：LLM 请求失败或未产出叙事文本。

    轮次物理状态已在 Director.run_turn 落库，Narrator 失败不影响回档；
    上层管线可捕获后以导演手记降级对外输出，保证玩家至少拿到裁决内容。
    """


class OpeningError(OpenCOCKeeperError):
    """开场初始化失败：前置条件缺失（未绑模组 / 无有效 PC）、Opening Agent 决策失败
    或 Turn 0 演播/落库失败。

    无静默降级——报错即停，修复后重试；副作用后置保证 LLM 失败时零残留、可干净重试，
    不遗留半开场状态（Turn 0 半写 / 记忆重复植入）。
    """


class EndingError(OpenCOCKeeperError):
    """终局收尾失败：终局演播 / 终局叙事落库 / 全盘固化 / 终局快照 / 世界归档任一步失败。

    无静默降级——报错即中断停运，且世界状态保持 ACTIVE 不归档，保证状态绝对一致，
    用户修复后重试；终局轮次本身已随 Director 落库，重试走 /world archive 复用同一轮
    不重复落轮。
    """
