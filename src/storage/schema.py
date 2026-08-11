# -*- coding: utf-8 -*-
"""
@File     :   schema.py
@Desc     :   存储层建表 DDL 迁移，经 db.migrate() 按 user_version 顺序应用
@Note     :   单库多世界；entities/recent_turns/chat_history_all 均外键关联
             world_state，ON DELETE CASCADE 保证删世界时级联清理，杜绝孤儿数据；
             迁移 1 为 world_state 补充 global_recap 宏观前情提要列
"""

# 迁移脚本：每个元素是一组单条 SQL 语句，列表索引即 schema 版本号
MIGRATIONS: list[list[str]] = [
    # 迁移 0：四张核心表 + 索引
    [
        """
        CREATE TABLE IF NOT EXISTS world_state (
            world_id     TEXT PRIMARY KEY,
            player_ids   TEXT NOT NULL DEFAULT '[]',
            game_phase   TEXT NOT NULL DEFAULT 'EXPLORATION',
            global_flags TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS entities (
            world_id              TEXT NOT NULL,
            id                    TEXT NOT NULL,
            type                  TEXT NOT NULL,
            name                  TEXT NOT NULL,
            hp                    INTEGER NOT NULL DEFAULT 0,
            hp_max                INTEGER NOT NULL DEFAULT 0,
            mp                    INTEGER NOT NULL DEFAULT 0,
            mp_max                INTEGER NOT NULL DEFAULT 0,
            san                   INTEGER NOT NULL DEFAULT 0,
            san_max               INTEGER NOT NULL DEFAULT 0,
            attributes_and_skills TEXT NOT NULL DEFAULT '{}',
            inventory             TEXT NOT NULL DEFAULT '[]',
            tags                  TEXT NOT NULL DEFAULT '[]',
            created_at            TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at            TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (world_id, id),
            FOREIGN KEY (world_id) REFERENCES world_state(world_id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(world_id, type)",
        """
        CREATE TABLE IF NOT EXISTS recent_turns (
            world_id     TEXT NOT NULL,
            turn_id      TEXT NOT NULL,
            turn_num     INTEGER NOT NULL,
            context_data TEXT NOT NULL DEFAULT '{}',
            state_diff   TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (world_id, turn_id),
            UNIQUE (world_id, turn_num),
            FOREIGN KEY (world_id) REFERENCES world_state(world_id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_recent_turns_num ON recent_turns(world_id, turn_num)",
        """
        CREATE TABLE IF NOT EXISTS chat_history_all (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            world_id   TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (world_id) REFERENCES world_state(world_id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_chat_history_world ON chat_history_all(world_id, id)",
    ],
    # 迁移 1：world_state 增加 global_recap 列（宏观记忆固化写回的全局前情提要）
    # 已有库走 ALTER TABLE 补列，全新库按序执行后字段同样齐备；NOT NULL DEFAULT '' 保证存量行不空
    [
        "ALTER TABLE world_state ADD COLUMN global_recap TEXT NOT NULL DEFAULT ''",
    ],
    # 迁移 2：recent_turns 增加 solidified 固化标记列，跟踪"已提炼进 RAG"的轮次
    # 固化接口据此找出尚未固化的轮次，避免重复提炼；默认 0=未固化
    [
        "ALTER TABLE recent_turns ADD COLUMN solidified INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_recent_turns_solidified "
        "ON recent_turns(world_id, solidified, turn_num)",
    ],
    # 迁移 3：world_state 增加 module_name 列（世界绑定的模组文件名）
    # 世界初始化时强制绑定 data/modules 下已存在的模组文件，空串代表未绑定
    # （自由扮演/自定义剧情）；文件存在性校验在存储层 ensure_world/update_world 完成
    [
        "ALTER TABLE world_state ADD COLUMN module_name TEXT NOT NULL DEFAULT ''",
    ],
    # 迁移 4：entities 增加 background 列（调查员入模组前的背景故事 JSON）
    # 键与 glyphkeeper Character 字段对齐：appearance_desc 形象描述 / belief 思想与信念 /
    # significant_person 重要之人 / significant_place 意义非凡之地 / cherished_possession 宝贵之物 /
    # trait 特质 / injury_scar 伤口和疤痕 / phobias_manias 恐惧症和躁狂症 / full_backstory 背景故事；
    # 属"入局前固定资产"随卡持久化，模组内新事件走记忆库不写回本列
    [
        "ALTER TABLE entities ADD COLUMN background TEXT NOT NULL DEFAULT '{}'",
    ],
    # 迁移 5：entities 增加 occupation 职业列（角色卡 xlsx E5 格子，导入/建卡时写入）
    [
        "ALTER TABLE entities ADD COLUMN occupation TEXT NOT NULL DEFAULT ''",
    ],
    # 迁移 6：world_state 增加 status 生命周期状态列（ACTIVE 活跃 / ARCHIVED 已结团归档）
    # 归档世界只读、后台固化 Worker 轮询跳过；与 game_phase（剧情阶段）语义分离，严禁复用
    [
        "ALTER TABLE world_state ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE'",
        "CREATE INDEX IF NOT EXISTS idx_world_status ON world_state(status)",
    ],
]
