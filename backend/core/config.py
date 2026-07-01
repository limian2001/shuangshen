"""
核心配置 — 从 .env 读取所有配置项
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")


class Config:
    # 应用
    FLASK_ENV: str = os.getenv("FLASK_ENV", "development")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret")
    PORT: int = int(os.getenv("PORT", 5000))
    DEBUG: bool = FLASK_ENV == "development"

    # 数据库
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/shuangshen.db")
    # 解析出 SQLite 文件路径
    DB_PATH: Path = BASE_DIR / "data" / "shuangshen.db"

    # LLM
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "anthropic")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    LOCAL_LLM_BASE_URL: str = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434")
    LOCAL_LLM_MODEL: str = os.getenv("LOCAL_LLM_MODEL", "qwen2.5")
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    # DeepSeek Embedding（与 Chat 共用 API Key，注意确认模型名称）
    DEEPSEEK_EMBED_MODEL: str = os.getenv("DEEPSEEK_EMBED_MODEL", "text-embedding-v2")

    # RAG 检索配置
    RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", 6))
    RAG_POOL_LIMIT: int = int(os.getenv("RAG_POOL_LIMIT", 500))

    # 导入处理深度（None = 不限；后续按用户分层收费）
    # 0 表示不限制，非0表示字符数上限
    EXTRACT_MAX_CHARS: int = int(os.getenv("EXTRACT_MAX_CHARS", 0)) or None

    # 风格档案字数上限
    STYLE_PROFILE_MAX_CHARS: int = int(os.getenv("STYLE_PROFILE_MAX_CHARS", 300))

    # Chroma 向量库持久化目录
    CHROMA_PATH: Path = BASE_DIR / os.getenv("CHROMA_PATH", "data/chroma")

    # JWT
    JWT_SECRET: str = os.getenv("JWT_SECRET", "jwt-secret")
    JWT_EXPIRE_HOURS: int = int(os.getenv("JWT_EXPIRE_HOURS", 72))

    # 文件上传
    UPLOAD_FOLDER: Path = BASE_DIR / os.getenv("UPLOAD_FOLDER", "data/uploads")
    MAX_CONTENT_LENGTH: int = int(os.getenv("MAX_CONTENT_LENGTH_MB", 50)) * 1024 * 1024


config = Config()

# 确保必要目录存在
config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
config.CHROMA_PATH.mkdir(parents=True, exist_ok=True)
