from __future__ import annotations
"""
数据库层 — SQLite (开发) / PostgreSQL (生产)
使用 Python 标准库 sqlite3，保持零依赖
"""
import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path

from backend.core.config import config

DB_PATH = str(config.DB_PATH)

# ─────────────────────────────────────────────
# Schema — 所有表定义
# ─────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ① 用户表
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,          -- UUID
    phone       TEXT UNIQUE,
    email       TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role        TEXT DEFAULT 'creator',    -- creator | receiver | both
    created_at  TEXT DEFAULT (datetime('now'))
);

-- ② 替身表
CREATE TABLE IF NOT EXISTS avatars (
    id              TEXT PRIMARY KEY,      -- UUID
    creator_id      TEXT NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,         -- 替身显示名称，如"小王（儿子）"
    relationship    TEXT NOT NULL,         -- son | boyfriend | friend | custom
    persona_prompt  TEXT DEFAULT '',       -- 生成的人格 System Prompt
    voice_model_id  TEXT DEFAULT '',       -- 声音克隆模型 ID（v0.2）
    status          TEXT DEFAULT 'active', -- active | paused | deleted
    share_code      TEXT UNIQUE,           -- 共享码，接收者用此绑定
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- ③ 替身-接收者绑定表
CREATE TABLE IF NOT EXISTS avatar_receivers (
    id          TEXT PRIMARY KEY,
    avatar_id   TEXT NOT NULL REFERENCES avatars(id),
    receiver_id TEXT NOT NULL REFERENCES users(id),
    bound_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(avatar_id, receiver_id)
);

-- ④ 记忆表（存储结构化记忆条目）
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    avatar_id   TEXT NOT NULL REFERENCES avatars(id),
    content     TEXT NOT NULL,             -- 记忆原文
    mem_type    TEXT DEFAULT 'opinion',    -- episode | opinion | style | wechat
    topic_tags  TEXT DEFAULT '[]',         -- JSON 数组，如 ["新闻", "科技"]
    source      TEXT DEFAULT 'manual',     -- manual | wechat_import | voice
    -- 简化向量：用 JSON 存储关键词权重，生产换向量数据库
    keywords    TEXT DEFAULT '{}',         -- JSON {词: 权重}
    created_at  TEXT DEFAULT (datetime('now'))
);

-- ⑤ 敏感话题预设表
--   avatar_id = NULL  → 全局话题，适用于所有替身（反诈骗/数据保护）
--   avatar_id = xxx   → 替身专属话题（已废弃，保留字段兼容旧数据）
CREATE TABLE IF NOT EXISTS sensitive_topics (
    id               TEXT PRIMARY KEY,
    avatar_id        TEXT REFERENCES avatars(id),   -- NULL = 全局
    trigger_keywords TEXT NOT NULL,                 -- JSON 数组（保留兼容）
    topic_description TEXT DEFAULT '',              -- LLM 生成的意图描述
    strategy         TEXT NOT NULL,                 -- preset_answer | deflect | admit_unknown
    preset_content   TEXT DEFAULT '',               -- strategy=preset_answer 时的回答内容
    created_at       TEXT DEFAULT (datetime('now'))
);

-- ⑥ 对话历史表
CREATE TABLE IF NOT EXISTS chat_messages (
    id          TEXT PRIMARY KEY,
    avatar_id   TEXT NOT NULL REFERENCES avatars(id),
    receiver_id TEXT NOT NULL REFERENCES users(id),
    role        TEXT NOT NULL,             -- user | assistant
    content     TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- ⑦ 数据导入任务表（微信记录解析进度）
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id          TEXT PRIMARY KEY,
    avatar_id   TEXT NOT NULL REFERENCES avatars(id),
    filename    TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',    -- pending | processing | done | failed
    total_msgs  INTEGER DEFAULT 0,
    imported    INTEGER DEFAULT 0,
    error_msg   TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    finished_at TEXT
);

-- ⑧ 查询扩展缓存表（持久化 LLM 同义词扩展结果）
CREATE TABLE IF NOT EXISTS query_expansions (
    query_hash  TEXT PRIMARY KEY,          -- 查询文本的 hash（前50字）
    query_text  TEXT NOT NULL,             -- 原始查询（便于调试）
    expanded    TEXT NOT NULL,             -- JSON 数组，扩展词列表
    hit_count   INTEGER DEFAULT 1,         -- 命中次数（越高越可信）
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_memories_avatar  ON memories(avatar_id);
CREATE INDEX IF NOT EXISTS idx_chat_avatar      ON chat_messages(avatar_id, receiver_id);
CREATE INDEX IF NOT EXISTS idx_topics_avatar    ON sensitive_topics(avatar_id);
"""


def get_connection() -> sqlite3.Connection:
    """获取数据库连接（每次调用返回新连接）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row       # 让查询结果支持列名访问
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    """上下文管理器 — 自动提交/回滚"""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """初始化数据库，创建所有表并执行增量迁移"""
    with get_db() as conn:
        conn.executescript(SCHEMA)
        _run_migrations(conn)
    print(f"✅ 数据库初始化完成: {DB_PATH}")


def _run_migrations(conn: sqlite3.Connection):
    """增量迁移 — 对已存在的数据库补充新字段，幂等安全"""
    migrations = [
        # v0.2: 敏感话题增加语义描述字段
        "ALTER TABLE sensitive_topics ADD COLUMN topic_description TEXT DEFAULT ''",
        # v0.2: 记忆表增加 bigram 索引字段（存储双字组合，加速检索）
        "ALTER TABLE memories ADD COLUMN bigrams TEXT DEFAULT '{}'",
        # v0.2: 查询扩展缓存表（新建，executescript 里已有 CREATE IF NOT EXISTS，这里仅占位）
        # v0.4: 替身增加 identity_desc（用户填写的人设描述，与 LLM 生成的 persona_prompt 分离）
        "ALTER TABLE avatars ADD COLUMN identity_desc TEXT DEFAULT ''",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass  # 字段已存在时 SQLite 会报错，直接忽略


# ─────────────────────────────────────────────
# 通用 CRUD 辅助函数
# ─────────────────────────────────────────────

def row_to_dict(row) -> dict:
    """sqlite3.Row → dict"""
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows) -> list:
    return [dict(r) for r in rows]
