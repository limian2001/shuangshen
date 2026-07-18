# 替身 (Shuangshen) 系统架构文档

**版本：** v0.9  
**更新日期：** 2026-07-18  
**状态：** v0.9 AI 服务迁移（CloudBase LLM + TokenHub Embedding）+ 语音消息 + 声音复刻 V3 + 游客模式 + 可观测性（告警持久化 / RAG 命中统计 / admin 数据概览）

---

## 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v0.1 | 2024 | 初始版本：基础对话、SQLite、DeepSeek |
| v0.2 | 2024 | bigram 检索、LLM query expansion、查询缓存 |
| v0.3 | 2025 | 架构重构：敏感话题改注入式、重复话题情绪感知 |
| v0.4 | 2025 | 多关系类型、identity_desc、多文件导入、HTML 解析、记忆去重 |
| v0.5 | 2026-06 | 风格档案、向量检索（SQLite）、记忆优先级、原始数据存储、时间情景注入 |
| v0.6 | 2026-06 | 向量库迁移至 Chroma；双写存储架构；记忆 chat_date；导入日期范围追踪；新增微信长按复制格式解析 |
| v0.7 | 2026-07 | 微信手机号登录（两步认证）；管理后台（admin.py + admin.html）；移除安全硬拦截；回复字数强制约束 + 禁括号动作描写；max_tokens→2000；style_profile 显式捕捉消息长度 |
| v0.8 | 2026-07 | 媒体服务（TTS/声音复刻/ASR）全栈接入；小程序落地页（微信审核合规）；图片上下文注入对话；声音克隆 UI（录音+质检+按需播放）；接收者权限隔离；解绑替身 |
| v0.9 | 2026-07-18 | LLM 迁移至腾讯 CloudBase 网关（deepseek-v4-flash，失败自动回退 DeepSeek 直连）；Embedding 迁移至腾讯 TokenHub（kinfra-text-embedding-4b，2560维）——此前 DeepSeek embedding 接口不存在，RAG 一直静默降级关键词，本版修复并全量补建向量；声音复刻切换 V3 接口（X-Api-Key 认证，speaker_id 池分配）；微信式语音消息（气泡+重播+ASR 后台转写）；游客模式（小程序审核合规）；AI 生成内容显著标识；移除图片/vision 功能；可观测性：system_alerts 告警持久化 + rag_counters 命中统计 + admin 系统告警页/数据概览页 |

---

## 一、项目概述

替身是 AI 数字分身平台。创建者上传真实聊天记录、设置人设信息，系统学习其说话风格与个人特征，生成一个 AI 替身供家人、朋友与其互动。

**核心设计原则：**
- 不是聊天机器人，是模拟真实人格的数字替身
- 人设（创建者手写）优先级 > 系统提取的记忆 > LLM 通用知识
- 宁可模糊回答，不可编造事实

---

## 二、技术栈

| 组件 | 当前方案 | 备注 |
|------|----------|------|
| 后端框架 | Flask 3.x | Python 3.9+ |
| 关系型数据库 | SQLite（WAL 模式） | 生产可迁 PostgreSQL |
| 向量数据库 | ChromaDB（本地持久化） | v0.6 引入，替代 SQLite embedding 字段 |
| LLM | 腾讯 CloudBase AI 网关（`deepseek-v4-flash`，思维链模型） | OpenAI 兼容；免费额度+备案合规；失败自动回退 DeepSeek 直连（v0.9） |
| Embedding | 腾讯 TokenHub（`kinfra-text-embedding-4b`，2560维） | OpenAI 兼容 `/v1/embeddings`；独立 EMBED_API_KEY（v0.9） |
| 语音合成（TTS） | 火山引擎豆包 seed-tts-2.0（WebSocket） | 默认发音人；v0.8 接入 |
| 声音克隆（TTS-ICL） | 火山引擎声音复刻 2.0（V3 接口） | 训练/查询 `api/v3/tts/voice_clone|get_voice`，合成 WS + `seed-icl-2.0`；v0.9 切换 |
| 语音识别（ASR） | 腾讯云 SentenceRecognition | 实时录音转文字；v0.8 接入 |
| 前端（管理端） | 单文件 HTML + Vanilla JS | 零依赖，便于嵌入；通过 WebView 加载进小程序 |
| 前端（小程序） | 微信小程序原生（WXML/JS） | Shell：微信登录 → 获取 token → 打开 WebView |
| 认证 | JWT（Bearer Token） | 无状态 |

---

## 三、存储架构

### 3.1 双存储设计原则

v0.6 起，episode/opinion 类型的记忆同时写入 SQLite 和 Chroma，两者职责不同：

```
SQLite memories 表   ←  权威数据源：元数据、内容、关键词、展示、降级检索
        ↓ embed（TokenHub kinfra-text-embedding-4b）
Chroma memories      ←  搜索加速层：HNSW 向量索引，只负责"找最相似"
```

**SQLite 是唯一的数据源，Chroma 是可重建的索引。** 如果 Chroma 数据丢失，可通过 `sync_to_chroma()` 从 SQLite 全量重建，无数据损失。反过来不行。

### 3.2 各存储内容

| 存储位置 | 存储内容 |
|----------|----------|
| SQLite `raw_uploads` | 原始聊天文本全文（永久，SHA256 去重） |
| SQLite `memories` | 所有记忆条目的内容、元数据、关键词索引 |
| SQLite `avatars` | 人设、style_profile、avg_reply_chars、persona_prompt |
| SQLite `chat_messages` | 用户与替身的实时对话历史 |
| SQLite `ingest_jobs` | 导入任务记录（含 date_start / date_end 聊天日期范围） |
| **Chroma** `memories` | 仅 episode/opinion 的向量索引（用于 RAG 检索） |
| 本地磁盘 | `data/shuangshen.db`（SQLite 文件）和 `data/chroma/`（Chroma 目录） |

> **备份原则：只需备份 `data/` 目录**，其余均可从代码重建。

### 3.3 为什么双写而不只写 Chroma

Chroma 负责向量检索，SQLite 有三个不可替代的作用：

1. **降级检索兜底**：Embedding 服务不可用时，自动切到 SQLite 关键词 + bigram 检索，功能照常，只是质量略降。**v0.9 起每次降级都会写入 system_alerts 并计入 rag_counters，不再静默**（v0.9 之前 DeepSeek embedding 接口实际不存在，RAG 曾长期静默降级而无人知晓——这是可观测性建设的直接动因）。
2. **UI 展示与分页**：记忆列表的 `ORDER BY`、分页、按日期/类型筛选，SQL 原生支持；Chroma 不适合做这类关系型查询。
3. **Chroma 可被重建**：`sync_to_chroma(avatar_id)` 能把 SQLite 里的记忆全量重新 embed 后写入 Chroma，是数据修复的兜底路径（重解析时也会自动调用）。

### 3.4 wechat / style 类型不进 Chroma

这两种类型仅用于生成 style_profile 和 persona_prompt，不参与 RAG 检索，因此不写入 Chroma，也不计算 embedding。

---

## 四、数据模型

### 4.1 核心表

```sql
-- 替身表
avatars (
  id, creator_id, name, relationship,
  persona_prompt  TEXT,   -- LLM 生成的人格摘要（≤300字）
  style_profile   TEXT,   -- LLM 提取的说话风格档案（≤300字）
  avg_reply_chars INTEGER, -- 「我」的平均回复字数，注入 System Prompt 控制长度
  identity_desc   TEXT,   -- 创建者手写人设（最高优先级）
  voice_model_id  TEXT,   -- 声音克隆的 speaker_id（S_ 开头，seed-icl-2.0 合成用）
  voice_language  TEXT DEFAULT 'zh',  -- zh | yue | en
  status, share_code, created_at, updated_at
)

-- 替身-接收者绑定表（v0.8）
avatar_receivers (
  id, avatar_id, receiver_id,
  bound_at TEXT DEFAULT (datetime('now')),
  UNIQUE(avatar_id, receiver_id)
)

-- 声音样本表（v0.8）
voice_samples (
  id, avatar_id,
  filename      TEXT,
  duration_sec  FLOAT DEFAULT 0,
  file_path     TEXT,          -- 本地存储路径
  clone_voice_id TEXT DEFAULT '', -- 上传到火山引擎后返回的 speaker_id
  status        TEXT DEFAULT 'pending', -- pending | uploaded | training | ready | failed
  created_at    TEXT
)

-- 记忆表
memories (
  id, avatar_id,
  content    TEXT,        -- 记忆内容
  mem_type   TEXT,        -- episode | opinion | style | wechat
  priority   INTEGER DEFAULT 0, -- 0=系统提取，1=用户手动（RAG 打分 ×1.5）
  chat_date  TEXT,        -- 记忆对应的原始聊天日期（NULL=无聊天来源）
  topic_tags TEXT,        -- JSON 数组
  keywords   TEXT,        -- JSON 关键词权重（降级检索用）
  source     TEXT,        -- manual | wechat_import | llm_extracted
  created_at TEXT         -- 记录写入数据库的时间（与 chat_date 区分）
  -- 注：embedding 字段已废弃，向量改存 Chroma
)

-- 原始上传表
raw_uploads (
  id, avatar_id, creator_id,
  filename        TEXT,
  file_type       TEXT,   -- txt | html | paste
  content         TEXT,   -- 原始全文（永久保存）
  char_count      INTEGER,
  content_hash    TEXT,   -- SHA256，防重复存储
  process_version TEXT,   -- 处理时的算法版本
  processed_at    TEXT,   -- NULL = 尚未处理
  created_at
)

-- 导入任务表
ingest_jobs (
  id, avatar_id, filename, status,
  total_msgs  INTEGER,
  imported    INTEGER,
  date_start  TEXT,       -- 本次导入覆盖的聊天起始日期
  date_end    TEXT,       -- 本次导入覆盖的聊天结束日期
  error_msg   TEXT,
  created_at, finished_at
)
```

### 4.2 记忆类型说明

| mem_type | 来源 | 是否进 Chroma | 是否参与 RAG | 用途 |
|----------|------|:---:|:---:|------|
| `episode` | LLM 从聊天提取 | ✅ | ✅ | 事实性记忆（住哪、工作、经历等） |
| `opinion` | LLM 从聊天提取 | ✅ | ✅（×1.3 加权） | 个人观点、喜好、态度 |
| `style` | LLM 从聊天提取 | ❌ | ❌ | 仅用于生成 style_profile |
| `wechat` | 原始聊天样本 | ❌ | ❌ | 仅用于生成 persona_prompt / style_profile |

### 4.3 记忆优先级与 chat_date

```
priority = 1（用户手动添加）  →  RAG 打分 × 1.5
priority = 0（系统自动提取）  →  RAG 打分 × 1.0

chat_date（非空）  →  UI 展示原始聊天日期（如 "2020年11月23日"）
chat_date（NULL） →  展示 created_at（记录导入时间）
```

### 4.4 v0.9 新增表与字段

```sql
-- 语音消息（chat_messages 扩展列）
msg_type   TEXT DEFAULT 'text'    -- text | voice
audio_path TEXT                   -- 语音文件路径（重播用）
duration   INTEGER DEFAULT 0      -- 语音秒数（气泡展示）

-- 系统告警（运维数据，自动修剪：30天 / 5000条）
CREATE TABLE system_alerts (
    id TEXT PRIMARY KEY,
    tag TEXT NOT NULL,            -- LLM降级|EMBED降级|RAG降级|TTS降级|LLM重试|API异常
    message TEXT NOT NULL,
    avatar_id TEXT,               -- RAG 类告警关联替身
    created_at TEXT
);

-- RAG 命中统计（按替身按日聚合，体量极小，不修剪）
CREATE TABLE rag_counters (
    avatar_id TEXT, date TEXT,
    vector_hits INTEGER,          -- 向量检索命中
    keyword_fallbacks INTEGER,    -- 降级关键词
    empty_results INTEGER,        -- 无结果
    PRIMARY KEY (avatar_id, date)
);
```

> **数据保留原则（明确约定）：自动修剪只作用于 `system_alerts` 运维日志。**
> 替身相关的一切数据——memories、raw_uploads、chat_messages、Chroma 向量、
> 人设、语音样本——**永不自动删除**。越早的数据可能越重要，如未来体量过大，
> 再单独制定策略（如冷数据迁 OSS），绝不静默清理。

---

## 五、数据导入流程

### 5.1 完整流程图

```
用户上传 .txt / .html 或粘贴文本
        │
        ▼
① 保存原始文本 → raw_uploads（SQLite）← 永久保存，SHA256 去重
        │
        ▼
② 解析（wechat_parser.py）
   支持四种格式：
   • 微信 PC 导出 TXT（时间戳 + 发件人 在同一行）
   • 微信「长按多选→复制」（发件人/完整日期/内容 各占一行，含年月日）← v0.6 新增
   • 手机复制粘贴（发件人 + HH:MM 在同一行）
   • 简化格式「名字: 内容」
        │
        ├──────────────────────────────────────┐
        ▼                                      ▼
③ 我的消息（my_messages）             所有消息（all_messages）
        │                                      │
        ├→ memories，mem_type=wechat（SQLite）  │
        │   保留 chat_date（消息时间戳）        │
        │                                      ▼
        ├→ avatars.style_profile（SQLite）  ④ LLM 语义提取
        │   LLM 提取说话风格，≤300字            → episode（事实/经历）
        │                                      → opinion（观点/喜好）
        └→ avatars.avg_reply_chars（SQLite）   写入 memories（SQLite）
            统计平均回复字数                    同时 upsert Chroma（向量）
                                               chat_date = 对话起始日期
        │
        ▼
⑤ rebuild_persona_prompt → avatars.persona_prompt（SQLite）
```

### 5.2 重新解析（Reparse）

点击「重新解析」时，从 `raw_uploads` 重读原始文本，重跑全部流程。由于 `add_memories_batch` 会跳过 SQLite 中已有内容，重解析后额外调用 `sync_to_chroma(avatar_id)`，将所有 episode/opinion 记忆从 SQLite 全量同步到 Chroma。

### 5.3 导入日期追踪

每次导入完成后，解析到的最早和最晚聊天时间写入 `ingest_jobs.date_start / date_end`，前端历史列表展示为 `📅 2020年11月23日 ~ 2021年03月15日`，避免重复导入或遗漏时间段。

---

## 六、对话流程

每次用户发送一条消息：

```
用户消息
    │
    ▼
① 话题检测（topic_guard.py）
   命中预设话题 → 生成 topic_hints 字符串注入 System Prompt（不再硬拦截）
   注：安全硬拦截已在 v0.7 移除（产品定位为熟人通信，无金融欺诈场景）
    │
    ▼
② 记忆检索（memory_store.search_memories）
   ┌─────────────────────────────────────────────┐
   │ 优先：Chroma 向量检索（HNSW，余弦距离）       │
   │ 降级：SQLite 关键词 + bigram + LLM 同义扩展  │
   └─────────────────────────────────────────────┘
   过滤：只检索 episode + opinion 类型
   加权：priority=1 ×1.5，opinion ×1.3
   返回：top_k 条（默认 6）
    │
    ▼
③ 构建 System Prompt（persona_builder.build_system_prompt）
   ┌──────────────────────────────────────────────┐
   │ 角色定位（relationship template）             │
   │ 行为准则（禁止编造、不承认AI身份等）          │
   │ 【相关记忆】— RAG top_k 条                   │
   │ 【说话风格档案】— style_profile ≤300字        │
   │ 【人设背景】— identity_desc（用户手写）       │
   │ 【人格分析】— persona_prompt（LLM摘要）       │
   │ 【回复长度规则】— avg_reply_chars（硬性上限：均值×2，违反即暴露AI）│
   │ 【禁括号动作描写】— 禁止「（笑了笑）」等括号表情/动作           │
   │ 【时间情景提示】— 当前时段（凌晨/早上/…）                       │
   │ （可选）敏感话题提示                                             │
   └─────────────────────────────────────────────────────────────────┘
    │
    ▼
④ 调用 LLM（CloudBase deepseek-v4-flash，max_tokens=2000+思维链余量，
   temperature=0.75；失败自动回退 DeepSeek 直连并记 LLM降级 告警）
    │
    ▼
⑤ 返回回复，写入 chat_messages 表（SQLite）
    │
    ├─（语音消息入口 /api/chat/:id/voice）ASR 文本走同一管线，
    │   用户消息记 msg_type=voice + audio_path + duration
    │   （图片/vision 功能已于 v0.9 移除：DeepSeek 系模型不支持视觉，
    │     且图片对人格模拟信息增量有限）
    │
    └─（若请求含 voice:true）内联 TTS（可选，当前前端未启用）：
        调用 tts_synthesize → 返回 audio_base64 字段
```

### System Prompt 各块优先级

| 块 | 优先级 | 作用 |
|----|--------|------|
| identity_desc（用户手写） | **最高** | 核心定义，LLM 必须遵守 |
| style_profile（风格档案） | 高 | 说话习惯、称呼偏好等细节 |
| persona_prompt（人格摘要） | 中 | LLM 对这个人的整体性格分析 |
| 相关记忆（RAG） | 中 | 动态检索，随问题变化 |
| relationship template | 基础 | 角色基调，所有回答的底色 |

---

## 七、媒体服务（v0.8 接入，v0.9 重写声音克隆与语音消息）

### 7.1 服务与用途对应

| 服务 | 厂商 | 用途 | 调用时机 |
|------|------|------|----------|
| seed-tts-2.0（WebSocket） | 火山引擎 | 无克隆声音时的默认 TTS | 用户点击 🔊 按钮 |
| seed-icl-2.0（同一 WebSocket 端点） | 火山引擎 | 克隆音色的个性化 TTS | 同上，优先；失败自动降级默认音色（记 TTS降级 告警） |
| 声音复刻 V3（`/api/v3/tts/voice_clone`） | 火山引擎 | 上传样本即训练 / 状态查询 | 创建者在声音页操作 |
| SentenceRecognition | 腾讯云 | 语音消息转文字（ASR） | 用户发送语音消息时 |

### 7.2 TTS 路由逻辑（v0.9）

```
tts_synthesize(text, voice_id, language)
    │              两条路径同走 WSS wss://openspeech.bytedance.com/api/v3/tts/bidirection
    │              Auth: X-Api-Key: {VOLC_API_KEY}
    │
    ├─ voice_id 非空（S_ 开头 speaker_id）
    │       X-Api-Resource-Id: seed-icl-2.0
    │       失败 → 自动降级默认音色重试（alert("TTS降级")）
    │
    └─ voice_id 为空
            X-Api-Resource-Id: seed-tts-2.0，speaker=默认音色
```

### 7.3 声音克隆流程（V3，v0.9 重写）

```
用户录音（客户端质检：≥10s、非静音）
        │  webm/m4a → 前端转 24kHz 单声道 WAV（V3 不支持 webm）
        ▼
POST /api/media/voice-sample/<avatar_id>
        │
        ├→ speaker_id 分配：优先沿用替身已绑定的；否则从 VOLC_SPEAKER_IDS
        │   池中取一个未被占用的（S_ 开头，控制台购买音色获得；
        │   替身删除后其 speaker_id 自动回池复用）
        ├→ 保存音频到 data/uploads/voice_samples/<avatar_id>/
        ├→ voice_clone_upload()
        │   POST https://openspeech.bytedance.com/api/v3/tts/voice_clone
        │   Headers: X-Api-Key + X-Api-Request-Id（与 TTS 同一个 Key）
        │   Body: {speaker_id, audio:{data:base64, format}, language:0}
        │   ★ 上传即训练，无独立 train 步骤；2.0 商品服务端按 model_type=5 训练
        └→ avatars.voice_model_id = speaker_id，样本 status=training
                │
                ▼
GET /api/media/voice-status/<avatar_id>   轮询
        └→ POST /api/v3/tts/get_voice
           status: 0=NotFound 1=Training 2=Success 3=Failed 4=Active
           2/4 即可合成；响应含剩余训练次数（每槽位15次）与试听 demo_audio
```

**历史坑位记录（避免重蹈）：** V1 接口（`/api/v1/mega_tts/*`，Bearer;token 认证）与账号购买的 2.0 商品不匹配，会出现 grant not found / InvalidModelType 等错误；商品、训练接口版本、合成 Resource-Id 三者必须配套（2.0 → V3 训练 + seed-icl-2.0 合成）。

### 7.4 客户端录音与质量检测

前端使用 `MediaRecorder API`（Chrome → `audio/webm;codecs=opus`，Safari → `audio/mp4`）录制，录完后用 `AudioContext.decodeAudioData` 检测：时长 ≥ 10 秒、RMS 音量 ≥ 0.005（非静音）。通过后先转 24kHz WAV 再上传。

### 7.5 前端语音播放（按需加载）

AI 回复消息在**替身声音克隆成功时**显示 🔊 小喇叭（无克隆声音不显示）。点击才发起 TTS 请求（不点不合成、不计费），加载后切换为音频播放器。每条 bot 消息右下角常驻"AI 生成"角标（合规要求，不被喇叭替换）。

### 7.6 语音消息（v0.9 新增，微信式交互）

**设计原则：语音是消息本体，文字是 AI 的"翻译稿"。**

```
按住"按住说话" → 波形实时反馈（AnalyserNode RMS → 15根波条）
   │  左滑取消；松开发送；权限弹框期间松手有 pendingStop 防幽灵录音
   ▼
松开 → webm 转 16kHz WAV（腾讯 ASR 不支持 webm 容器）
   → 语音气泡立即上屏（乐观渲染，本地 blob 可即点重播）
   → POST /api/chat/:id/voice（multipart: audio + duration）
        ├→ 腾讯 ASR 转文字（识别失败 422，气泡标 ⚠️）
        ├→ 音频存 data/uploads/chat_voice/<avatar_id>/<msg_id>.wav
        ├→ 文字走统一 LLM 管线生成回复
        └→ chat_messages 记录 msg_type=voice + audio_path + duration
             content = ASR 文本（仅供 LLM 上下文，不作为消息展示）

历史加载：msg_type=voice 渲染语音气泡，点击经鉴权接口
GET /api/chat/:id/audio/:msg_id 拉取音频重播。
麦克风流缓存复用（一次授权），退出语音模式/页面隐藏时释放。
```

---

## 八、权限模型（v0.8，v0.9 改服务端事实判断 + 游客模式）

### 8.1 用户角色

| 角色 | 产生方式 | 能力 |
|------|----------|------|
| 创建者（Creator） | 自己创建替身 | 全部能力：对话 + 记忆/喂数据/人设/声音/共享 |
| 接收者（Receiver） | 通过共享码绑定 | 仅对话 + 解绑 |

### 8.2 前端实现（v0.9 改为服务端事实判断）

早期靠调用方传 `_isReceived: true` 标志，多入口下极易漏传（曾导致接收者看到全部管理 Tab + "无权限"红 toast 误报）。v0.9 起 `setCurrentAvatar(avatar)` **内部**判定，不信任调用方：

```
判定优先级：
1. avatar.creator_id 存在且 ≠ currentUser.user_id  → 接收者（服务端事实）
2. creator_id 缺失时退回 _isReceived 标志（兜底）
判定后回写 avatar._isReceived 供下游（showAvatarPage 守卫、checkAvatarVoice）使用

接收者 → 隐藏：记忆 / 喂数据 / 人设 / 声音 / 共享；显示：解绑替身
        （CSS class + style.setProperty('important') 双保险，兼容微信 WebView）
```

`/api/avatars/received` 为此返回 `creator_id` 字段。

### 8.3 后端权限保障

各管理接口在后端独立做 `creator_id == g.user_id` 校验，前端隐藏仅为 UX 优化，不构成安全边界。接收者即使直接调用管理接口也会收到 403。

### 8.4 游客模式（v0.9，小程序审核合规）

微信要求"先浏览后登录"。小程序落地页有"游客进入"按钮 → webview 带 `?guest=1` 打开 → 前端进入游客态：可浏览全部菜单与界面（空状态展示，`api()` 对无 token 请求静默返回不弹 401）；点创建替身/绑定/发消息/充值等实际操作时 `requireLogin()` 拦截，小程序内跳回原生登录页（`wx.miniProgram.navigateTo`），网页端回登录表单。退出登录经 `home?logout=1` 通知小程序清除其侧 token，回到原生落地页。

---

## 九、向量检索设计（v0.6 引入，v0.9 修复 Embedding）

### 9.1 Embedding 方案（v0.9 重写）

- **调用方：** 腾讯 TokenHub `kinfra-text-embedding-4b`（2560 维，OpenAI 兼容 `/v1/embeddings`，独立 `EMBED_API_KEY`），5xx/超时自动重试 2 次
- **历史教训：** v0.9 之前配置的 "DeepSeek Embedding" 接口在 DeepSeek 官方 API 中**从不存在**（模型名 `text-embedding-v2` 实为阿里云的），embed 一直静默失败、RAG 长期跑关键词降级。v0.9 修复后已对全部存量记忆执行 `scripts/backfill_vectors.py` 补建（199 条）
- **写入时机：** `add_memories_batch` 批量 embed 后 upsert Chroma；单条 `add_memory` 单次 embed
- **存储：** Chroma `memories` collection（不再存 SQLite `embedding` 字段）

### 9.2 Chroma Collection 结构

```python
collection.upsert(
    ids        = [memory_id],          # 与 SQLite memories.id 一致
    documents  = [content],
    embeddings = [vec],                # DeepSeek 计算，外部传入
    metadatas  = [{
        "avatar_id":  avatar_id,       # 用于 where 过滤
        "mem_type":   "episode",
        "priority":   0,
        "topic_tags": '["事实"]',
    }],
)
```

### 9.3 检索加权逻辑

```python
# Chroma 返回 cosine distance（0=完全一致）→ 转为相似度分
score = 1.0 - distance

if priority == 1:       score *= 1.5   # 用户手动记忆加权
if mem_type == 'opinion': score *= 1.3 # 观点类记忆加权

results.sort(by=score, desc).take(top_k)
```

### 9.4 降级链路（v0.9 起全程可观测）

```
embed 失败（TokenHub 异常）
  └→ alert("EMBED降级") 写 system_alerts
       └→ alert("RAG降级", avatar_id) + rag_counters.keyword_fallbacks +1
            └→ 自动降级：SQLite 关键词 + bigram 检索，功能照常
向量命中 → rag_counters.vector_hits +1
无结果   → rag_counters.empty_results +1

admin 数据概览页展示每替身 7 天命中率（≥60% 绿 / 30-60% 黄 / <30% 红），
长期偏低即算法失效信号。
```

### 9.5 扩容路径

| 规模 | 方案 |
|------|------|
| 当前（单用户 3GB 聊天记录） | Chroma 本地持久化 |
| 多用户生产环境 | Qdrant / pgvector（更换 chroma_store.py 适配层即可） |

---

## 十、原始数据永久存储

**原则：** 原始文本永久保存在 `raw_uploads`，处理逻辑可随算法迭代重新运行。

```
raw_uploads.content      ← 原始全文（不管文件有没有保留，数据库里有）
raw_uploads.content_hash ← SHA256，防止同一文件重复存储
raw_uploads.process_version ← 处理时的算法版本（v0.5 / v0.6 / …）
raw_uploads.processed_at ← NULL = 尚未处理 / 待重处理
```

**扩容路径：**
1. **当前：** TEXT 存 SQLite，简单可靠
2. **规模增大：** 加 `storage_url` 列，内容迁至 OSS（阿里云 / S3），SQLite 只存元数据

---

## 十一、配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `LLM_PROVIDER` | `cloudbase` | anthropic / openai / local / deepseek / cloudbase |
| `CLOUDBASE_BASE_URL` | — | CloudBase AI 网关（…/v1/ai/cloudbase） |
| `CLOUDBASE_API_KEY` | — | CloudBase API Key |
| `CLOUDBASE_MODEL` | `deepseek-v4-flash` | 思维链模型；调用侧自动加 token 余量防正文被推理耗尽 |
| `DEEPSEEK_API_KEY` | — | CloudBase 失败时的直连回退 |
| `EMBED_BASE_URL` | TokenHub | OpenAI 兼容 embedding 服务地址 |
| `EMBED_API_KEY` | — | Embedding 独立 Key |
| `EMBED_MODEL` | `kinfra-text-embedding-4b` | 2560 维 |
| `CHROMA_PATH` | `data/chroma` | Chroma 持久化目录 |
| `RAG_TOP_K` | 6 | 每次对话检索记忆条数 |
| `RAG_POOL_LIMIT` | 500 | SQLite 降级时的候选池上限 |
| `EXTRACT_MAX_CHARS` | None（不限） | LLM 语义提取的输入深度 |
| `STYLE_PROFILE_MAX_CHARS` | 300 | 风格档案字数上限 |
| `VOLC_APP_ID` | — | 火山引擎 App ID |
| `VOLC_API_KEY` | — | 火山引擎 API Key（TTS + 声音复刻 V3 共用） |
| `VOLC_SPEAKER_IDS` | — | 控制台购买的 S_ 音色池，逗号分隔；每替身占一个，删除后回池 |
| `TENCENT_ASR_SECRET_ID` | — | 腾讯云 ASR 密钥 ID |
| `TENCENT_ASR_SECRET_KEY` | — | 腾讯云 ASR 密钥 |
| `TENCENT_ASR_REGION` | `ap-guangzhou` | ASR 地域 |

**计划分层（待产品决策）：**

| 用户档位 | EXTRACT_MAX_CHARS | RAG_POOL_LIMIT |
|----------|-------------------|----------------|
| 免费 | 5,000 字 | 500 条 |
| 标准 | 20,000 字 | 2,000 条 |
| 高级 | 不限 | 全量（Chroma 向量检索） |

---

## 十二、接口概览

```
POST /api/auth/register
POST /api/auth/login
POST /api/auth/wechat            微信静默登录（返回 token 或 need_phone:true）
POST /api/auth/wechat_phone      微信手机号授权注册（两步认证第二步）

GET/POST  /api/avatars               创建/列出替身
GET/PUT   /api/avatars/:id           详情/更新
DELETE    /api/avatars/:id           软删除
POST      /api/avatars/:id/share     获取共享码
POST      /api/avatars/join/:code    绑定替身
GET       /api/avatars/received      接收者：我绑定的替身列表
DELETE    /api/avatars/received/:id  接收者：解绑替身

POST /api/ingest/:id/paste           粘贴文本导入
POST /api/ingest/:id/wechat          文件上传（多文件，.txt/.html）
POST /api/ingest/:id/detect          检测发言人（不导入）
GET  /api/ingest/:id/jobs            导入历史（含 date_start/date_end）
POST /api/ingest/:id/reparse         从原始记录重新解析（含 Chroma 同步）
GET  /api/ingest/:id/raw_count       原始记录数量

POST /api/chat/:id                   发送文字消息（支持 voice 参数同步返回 TTS）
POST /api/chat/:id/voice             发送语音消息（multipart；ASR→LLM 统一管线）
GET  /api/chat/:id/audio/:msg_id     语音消息重播（鉴权返回音频）
GET  /api/chat/:id/history           对话历史（含 msg_type/duration）

POST   /api/media/voice-sample/:id          上传声音样本（multipart，V3 上传即训练）
DELETE /api/media/voice-sample/:id/:sid     删除声音样本
GET    /api/media/voice-status/:id          查询声音克隆状态 + 样本列表
POST   /api/media/tts/:id                   TTS 合成（克隆音色优先，失败降级默认音色）
POST   /api/media/stt                       语音识别（WAV → 文字）

GET/POST  /api/topics                全局敏感话题

GET    /api/memories/:id             查看记忆库
POST   /api/memories/:id             手动添加记忆（priority=1）
DELETE /api/memories/:id/:mem_id     删除记忆

GET  /api/admin/stats                        统计概览（用户数/角色数/消息数 + 7日趋势）
GET  /api/admin/users?q=&page=               用户列表（可搜索分页）
GET  /api/admin/users/:id                    用户详情 + 替身列表
POST /api/admin/users/:id/reset_password     重置密码
POST /api/admin/users/:id/toggle_suspend     停用/启用
DELETE /api/admin/users/:id                  删除用户全部数据
GET  /api/admin/avatars                      所有替身列表
GET  /api/admin/avatars/:id                  替身详情（配置/上传/记忆/对话）
GET  /api/admin/avatars/:id/chat             替身对话历史（分页）
GET  /api/admin/alerts?tag=&limit=           系统告警列表 + 各标签 24h/7天计数（v0.9）
GET  /api/admin/data-summary                 存储占用 + 全局计数 + Top10 替身含命中率（v0.9）
GET  /api/admin/avatars/:id/data-detail      单替身数据钻取：记忆分布/向量一致性/命中率趋势（v0.9）
```

### 管理后台认证

管理后台使用 `require_admin` 装饰器，在 JWT 认证基础上额外校验 `users.is_admin = 1`。Web 页面入口为 `/admin`（服务 `frontend/admin.html`）。

---

## 十三、可观测性与运维（v0.9）

### 13.1 告警体系

所有降级与故障统一经 `llm_provider.alert(tag, msg, avatar_id=None)` 上报，双通道：

```
alert() ──┬── stdout：⚠️ [ALERT][标签] …（docker logs | grep ALERT 巡检）
          └── SQLite system_alerts（admin 系统告警页展示，30天/5000条自动修剪）
```

| 标签 | 触发点 | 含义 |
|------|--------|------|
| `LLM降级` | CloudBase 失败回退 DeepSeek 直连 | 网关额度耗尽或异常 |
| `LLM重试` | 思维链耗尽额度致正文为空，扩容重试 | 偶发正常，频繁需关注 |
| `EMBED降级` | TokenHub embedding 失败（已含 3 次重试） | 向量化不可用 |
| `RAG降级` | 检索退化为关键词匹配（带 avatar_id） | 影响该替身回忆质量 |
| `TTS降级` | 克隆音色合成失败回落标准音色 | 用户听到的不是本人声音 |
| `API异常` | Flask 未捕获异常（500），含路径与堆栈摘要 | 代码缺陷 |

admin 侧栏"系统告警"挂 24h 计数红点（2 分钟轮询），设计目标：**任何静默降级都必须可见**——此机制源于 embedding 曾坏数月无人察觉的教训。

### 13.2 数据概览与算法效果监控

`rag_counters` 按替身按日聚合三类检索结果，admin 数据概览页据此计算 7 天向量命中率；健康预期是 Top 替身命中率长期 ≥60%（绿）。单替身钻取页含**向量一致性检测**：可向量化记忆数 vs Chroma 实际向量数，出现缺口即提示"需补建"（跑 `scripts/backfill_vectors.py`）。

### 13.3 运维脚本（scripts/，服务器 docker cp 进容器执行）

| 脚本 | 用途 |
|------|------|
| `test_cloudbase.py` | CloudBase 对话 + TokenHub embedding 连通性三件套 |
| `test_voice_clone.py` | 声音复刻 V3 鉴权与训练状态（只读） |
| `test_clone_tts.py` | 克隆音色合成端到端（产出 mp3 试听） |
| `backfill_vectors.py` | 全量替身向量补建（幂等） |
| `find_embed_model.py` | embedding 服务/模型探测（排障用） |

---

## 十四、待解决问题与后续规划

| 优先级 | 问题 | 方案 |
|--------|------|------|
| 高 | 声音克隆训练状态轮询 | 前端自动轮询 voice-status，训练完成时 toast 提示 |
| 中 | TTS 流式播放（减少首字节延迟） | 考虑 WebSocket 流式返回音频片段 |
| 中 | 手动/自动记忆冲突检测 | priority 权重已设计，冲突提示待做 |
| 中 | Function Calling（工具调用型检索） | LLM 自主决定是否 RAG，替代硬编码 |
| 低 | persona_prompt 滚动重建 | 记忆超 N 条时增量更新 |
| 低 | 多用户 Chroma 隔离 | 切换为 per-avatar collection 或 Qdrant |
| 低 | 多语言支持 | 预留，暂不做 |
