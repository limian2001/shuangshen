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

    # 腾讯 CloudBase AI 网关（OpenAI 兼容；备案合规 + 免费额度）
    CLOUDBASE_BASE_URL: str    = os.getenv("CLOUDBASE_BASE_URL", "")   # https://<env>.api.tcloudbasegateway.com/v1/ai/cloudbase
    CLOUDBASE_API_KEY: str     = os.getenv("CLOUDBASE_API_KEY", "")
    CLOUDBASE_MODEL: str       = os.getenv("CLOUDBASE_MODEL", "deepseek-v4-flash")

    # Embedding 服务（腾讯混元官方 API — CloudBase 网关不含 embedding 模型）
    # API Key 在 console.cloud.tencent.com/hunyuan/api-key 创建
    EMBED_BASE_URL: str = os.getenv("EMBED_BASE_URL", "https://api.hunyuan.cloud.tencent.com/v1")
    EMBED_API_KEY: str  = os.getenv("EMBED_API_KEY", "")
    EMBED_MODEL: str    = os.getenv("EMBED_MODEL", "hunyuan-embedding")

    # 对话链路 Trace（全链路可见性）
    TRACE_ENABLED: bool       = os.getenv("TRACE_ENABLED", "1") != "0"
    TRACE_RETENTION_DAYS: int = int(os.getenv("TRACE_RETENTION_DAYS", 14))
    TRACE_MAX_ROWS: int       = int(os.getenv("TRACE_MAX_ROWS", 20000))

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

    # 微信小程序
    WECHAT_APPID: str = os.getenv("WECHAT_APPID", "")
    WECHAT_SECRET: str = os.getenv("WECHAT_SECRET", "")

    # JWT
    JWT_SECRET: str = os.getenv("JWT_SECRET", "jwt-secret")
    JWT_EXPIRE_HOURS: int = int(os.getenv("JWT_EXPIRE_HOURS", 72))

    # 对话历史
    # 前端展示的最大条数（0 = 不限制）
    CHAT_DISPLAY_LIMIT: int = int(os.getenv("CHAT_DISPLAY_LIMIT", 50))

    # 推广期：所有收费功能临时免费（True = 不扣言己币，但保留解锁记录供未来使用）
    FREE_FEATURES: bool = os.getenv("FREE_FEATURES", "true").lower() not in ("false", "0", "no")

    # 文件上传
    UPLOAD_FOLDER: Path = BASE_DIR / os.getenv("UPLOAD_FOLDER", "data/uploads")
    MAX_CONTENT_LENGTH: int = int(os.getenv("MAX_CONTENT_LENGTH_MB", 50)) * 1024 * 1024

    # ── 媒体服务 ──────────────────────────────────────────────────
    # 腾讯云 ASR（语音识别）
    TENCENT_ASR_SECRET_ID: str  = os.getenv("TENCENT_ASR_SECRET_ID", "")
    TENCENT_ASR_SECRET_KEY: str = os.getenv("TENCENT_ASR_SECRET_KEY", "")
    TENCENT_ASR_REGION: str     = os.getenv("TENCENT_ASR_REGION", "ap-guangzhou")

    # 火山引擎 TTS + 声音复刻（新版控制台，X-Api-* 认证）
    VOLC_APP_ID: str        = os.getenv("VOLC_APP_ID", "")
    VOLC_API_KEY: str       = os.getenv("VOLC_API_KEY", "")   # 新版 API Key（替代 ACCESS_TOKEN）
    # 以下为旧版兼容，新版不再使用
    VOLC_ACCESS_TOKEN: str  = os.getenv("VOLC_ACCESS_TOKEN", "")
    VOLC_CLUSTER: str       = os.getenv("VOLC_CLUSTER", "volcano_tts")
    # 声音复刻：控制台购买音色后获得的 S_ 开头 speaker_id，多个用英文逗号分隔
    VOLC_SPEAKER_IDS: str   = os.getenv("VOLC_SPEAKER_IDS", "")

    # ── 声音复刻供应商（aliyun=阿里云百炼 CosyVoice：复刻免费/1000配额/支持方言）──
    TTS_PROVIDER: str        = os.getenv("TTS_PROVIDER", "aliyun")   # aliyun | volc
    DASHSCOPE_API_KEY: str   = os.getenv("DASHSCOPE_API_KEY", "")
    DASHSCOPE_WORKSPACE: str = os.getenv("DASHSCOPE_WORKSPACE", "")  # 百炼业务空间 ID
    COSYVOICE_MODEL: str     = os.getenv("COSYVOICE_MODEL", "cosyvoice-v3.5-flash")
    # 复刻接口要求公网音频 URL，用本站域名 + 一次性签名 token 提供
    PUBLIC_BASE_URL: str     = os.getenv("PUBLIC_BASE_URL", "")

    # 声音复刻计费
    VOICE_CLONE_COST: int   = int(os.getenv("VOICE_CLONE_COST", 1000))   # 言己币
    VOICE_CLONE_FREE: bool  = os.getenv("VOICE_CLONE_FREE", "1") != "0"  # 限时免费开关
    VOICE_MAX_PER_USER: int = 2   # self 1 + other 1

    # 图片缓存（仅缩略图，超上限时按relevance_score淘汰）
    IMAGE_THUMB_MAX_WIDTH: int = int(os.getenv("IMAGE_THUMB_MAX_WIDTH", 300))
    IMAGE_MAX_PER_AVATAR: int  = int(os.getenv("IMAGE_MAX_PER_AVATAR", 200))


config = Config()

# 确保必要目录存在
config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
config.CHROMA_PATH.mkdir(parents=True, exist_ok=True)
