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

-- ⑨ 原始上传存储表（v0.5）
CREATE TABLE IF NOT EXISTS raw_uploads (
    id              TEXT PRIMARY KEY,
    avatar_id       TEXT NOT NULL REFERENCES avatars(id),
    creator_id      TEXT NOT NULL REFERENCES users(id),
    filename        TEXT DEFAULT '',
    file_type       TEXT DEFAULT 'txt',    -- txt | html | paste
    content         TEXT NOT NULL,         -- 原始全文
    char_count      INTEGER DEFAULT 0,
    content_hash    TEXT NOT NULL,         -- SHA256，防重复存储
    process_version TEXT DEFAULT 'v0.5',   -- 处理时算法版本，便于增量重处理
    processed_at    TEXT,                  -- NULL = 尚未处理 / 待重处理
    created_at      TEXT DEFAULT (datetime('now'))
);

-- ⑩ 平台身份联邦表（微信/WhatsApp/Facebook 等第三方登录）
CREATE TABLE IF NOT EXISTS user_identities (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    platform    TEXT NOT NULL,     -- wechat | whatsapp | facebook | telegram
    external_id TEXT NOT NULL,     -- 各平台原始 ID（openid、手机号、fb_user_id）
    extra       TEXT DEFAULT '{}', -- JSON，可存 session_key 等平台额外信息
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(platform, external_id)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_memories_avatar      ON memories(avatar_id);
CREATE INDEX IF NOT EXISTS idx_chat_avatar          ON chat_messages(avatar_id, receiver_id);
CREATE INDEX IF NOT EXISTS idx_topics_avatar        ON sensitive_topics(avatar_id);
CREATE INDEX IF NOT EXISTS idx_raw_uploads_avatar   ON raw_uploads(avatar_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_uploads_hash ON raw_uploads(avatar_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities(user_id);
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
        # v0.2
        "ALTER TABLE sensitive_topics ADD COLUMN topic_description TEXT DEFAULT ''",
        "ALTER TABLE memories ADD COLUMN bigrams TEXT DEFAULT '{}'",
        # v0.4
        "ALTER TABLE avatars ADD COLUMN identity_desc TEXT DEFAULT ''",
        # v0.5: 风格档案（LLM 提取的说话风格，注入 System Prompt）
        "ALTER TABLE avatars ADD COLUMN style_profile TEXT DEFAULT ''",
        # v0.5: 回复字数统计（导入时计算，注入 System Prompt 控制长度）
        "ALTER TABLE avatars ADD COLUMN avg_reply_chars INTEGER DEFAULT 0",
        # v0.5: 记忆优先级（0=系统提取，1=用户手动；RAG 打分时手动条目 ×1.5）
        "ALTER TABLE memories ADD COLUMN priority INTEGER DEFAULT 0",
        # v0.5: 向量 embedding（JSON 序列化浮点数组，用于余弦相似度检索）
        "ALTER TABLE memories ADD COLUMN embedding TEXT DEFAULT NULL",
        # v0.5: 导入任务的聊天日期范围（用于UI展示，避免重复导入）
        "ALTER TABLE ingest_jobs ADD COLUMN date_start TEXT DEFAULT ''",
        "ALTER TABLE ingest_jobs ADD COLUMN date_end TEXT DEFAULT ''",
        # v0.5: 记忆对应的原始聊天日期（与 created_at 区分；NULL 表示无聊天来源）
        "ALTER TABLE memories ADD COLUMN chat_date TEXT DEFAULT NULL",
        # v0.7: 平台身份联邦表（对已存在的数据库补建）
        """CREATE TABLE IF NOT EXISTS user_identities (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            platform TEXT NOT NULL,
            external_id TEXT NOT NULL,
            extra TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(platform, external_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities(user_id)",
        # v0.8: 关系扩展 — 对方角色、称呼、自定义关系名
        "ALTER TABLE avatars ADD COLUMN counterpart_role TEXT DEFAULT ''",
        "ALTER TABLE avatars ADD COLUMN address_to_other TEXT DEFAULT ''",
        "ALTER TABLE avatars ADD COLUMN address_from_other TEXT DEFAULT ''",
        "ALTER TABLE avatars ADD COLUMN custom_rel_name TEXT DEFAULT ''",
        # v0.9: 用户状态 + 管理员标志
        "ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'",
        "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0",
        # v0.10: 真实对话样本（JSON数组，直接注入 prompt 作 few-shot 示例）
        "ALTER TABLE avatars ADD COLUMN conversation_samples TEXT DEFAULT '[]'",
        # v0.10: 四类风格特征（JSON，情绪表达/转移话题/追问习惯/标志句式）
        "ALTER TABLE avatars ADD COLUMN style_features TEXT DEFAULT '{}'",
        # v1.0: 言己币系统
        "ALTER TABLE users ADD COLUMN coins INTEGER DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS coin_transactions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL,
            ref_id TEXT DEFAULT '',
            balance_after INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_coin_tx_user ON coin_transactions(user_id)",
        """CREATE TABLE IF NOT EXISTS avatar_feature_unlocks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            avatar_id TEXT NOT NULL REFERENCES avatars(id),
            feature TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, avatar_id, feature)
        )""",
        """CREATE TABLE IF NOT EXISTS chat_content_unlocks (
            id TEXT PRIMARY KEY,
            creator_id TEXT NOT NULL REFERENCES users(id),
            avatar_id TEXT NOT NULL REFERENCES avatars(id),
            receiver_id TEXT NOT NULL REFERENCES users(id),
            messages_unlocked INTEGER DEFAULT 20,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(creator_id, avatar_id, receiver_id)
        )""",
        """CREATE TABLE IF NOT EXISTS takeover_sessions (
            id TEXT PRIMARY KEY,
            avatar_id TEXT NOT NULL REFERENCES avatars(id),
            creator_id TEXT NOT NULL REFERENCES users(id),
            receiver_id TEXT NOT NULL REFERENCES users(id),
            status TEXT DEFAULT 'active',
            last_receiver_msg_at TEXT DEFAULT (datetime('now')),
            started_at TEXT DEFAULT (datetime('now')),
            ended_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_takeover_active ON takeover_sessions(avatar_id, receiver_id, status)",
        """CREATE TABLE IF NOT EXISTS referral_records (
            id TEXT PRIMARY KEY,
            referrer_id TEXT NOT NULL REFERENCES users(id),
            referred_id TEXT NOT NULL REFERENCES users(id),
            coins_awarded INTEGER DEFAULT 20,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(referred_id)
        )""",
        """CREATE TABLE IF NOT EXISTS purchase_records (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            amount_fen INTEGER NOT NULL,
            coins INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            wx_order_no TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )""",
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
