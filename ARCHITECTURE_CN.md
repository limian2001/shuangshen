# 替身 (Shuangshen) 系统架构文档

**版本：** v0.8  
**更新日期：** 2026-07-15  
**状态：** v0.8 媒体服务全栈接入（TTS / 声音复刻 / ASR）+ 小程序落地页 + 接收者权限模型

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
| LLM | DeepSeek Chat (`deepseek-chat`) | OpenAI 兼容接口 |
| Embedding | DeepSeek Embedding API (`text-embedding-v2`) | 与 Chat API 共用 key |
| 语音合成（TTS） | 火山引擎豆包 seed-tts-2.0（WebSocket） | 默认发音人；v0.8 接入 |
| 声音克隆（TTS-ICL） | 火山引擎声音复刻 2.0（`volcano_icl` HTTP） | 使用克隆的 voice_id；v0.8 接入 |
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
        ↓ embed（DeepSeek API）
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

1. **降级检索兜底**：DeepSeek Embedding API 不可用时，自动切到 SQLite 关键词 + bigram 检索，功能照常，只是质量略降。
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
  voice_model_id  TEXT,   -- 声音克隆成功后的 voice_id（volcano_icl 使用）
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
④ 调用 DeepSeek Chat API（max_tokens=2000，temperature=0.75）
    │
    ▼
⑤ 返回回复，写入 chat_messages 表（SQLite）
    │
    ├─（若请求含 image_description）图片语境注入：
    │   LLM 输入：「[我发了一张图片，内容：{desc}]\n{message}」
    │   RAG 检索 / 对话历史仍使用原始 message（图片描述不污染记忆）
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

## 七、媒体服务（v0.8）

### 7.1 服务与用途对应

| 服务 | 厂商 | 用途 | 调用时机 |
|------|------|------|----------|
| seed-tts-2.0（WebSocket） | 火山引擎 | 无克隆声音时的默认 TTS | 用户点击 🔊 按钮 |
| volcano_icl（HTTP POST） | 火山引擎 | 有克隆声音时的个性化 TTS | 同上，优先 |
| 声音复刻 2.0（mega_tts） | 火山引擎 | 上传样本 / 训练 / 状态查询 | 创建者在声音页操作 |
| SentenceRecognition | 腾讯云 | 语音消息转文字（ASR） | 用户发送语音输入时 |

### 7.2 TTS 路由逻辑

```
tts_synthesize(text, voice_id, language)
    │
    ├─ voice_id 非空  →  _tts_icl_http()
    │                    POST https://openspeech.bytedance.com/api/v1/tts
    │                    cluster=volcano_icl, voice_type={voice_id}
    │                    Auth: x-api-key: {VOLC_API_KEY}
    │
    └─ voice_id 为空  →  _tts_ws_async()
                         WSS wss://openspeech.bytedance.com/api/v3/tts/bidirection
                         Auth: X-Api-Key: {VOLC_API_KEY}
                              X-Api-Resource-Id: seed-tts-2.0
```

### 7.3 声音克隆流程

```
用户录音 / 上传音频文件（≥10s，非静音，客户端质检）
        │
        ▼
POST /api/media/voice-sample/<avatar_id>   （Flask）
        │
        ├→ 保存到 data/uploads/voice_samples/<avatar_id>/
        ├→ 写 voice_samples 表（status=uploading）
        ├→ voice_clone_upload() → 上传到火山引擎 mega_tts
        │   Auth: X-Api-App-Id + X-Api-Access-Key（不同于 TTS！）
        │   返回 clone_voice_id 写回 voice_samples
        └→ voice_clone_train() → 触发训练
           training_text 为固定中文朗读文本
           status 更新为 training
                │
                ▼
GET /api/media/voice-status/<avatar_id>   轮询
        └→ voice_clone_status() 查询训练结果
           成功 → avatars.voice_model_id = clone_voice_id
                  voice_samples.status = ready
```

### 7.4 客户端录音与质量检测

前端使用 `MediaRecorder API`（Chrome → `audio/webm;codecs=opus`，Safari → `audio/mp4`）录制，录完后用 `AudioContext.decodeAudioData` 检测：

- 时长 ≥ 10 秒
- RMS 音量 ≥ 0.005（非静音）

两项通过后显示"上传录音"按钮，否则提示重录。上传到后端后，MIME 类型根据扩展名正确识别（`.m4a → audio/mp4`，`.webm → audio/webm`，`.mp3 → audio/mpeg`，其余 → `audio/wav`）。

### 7.5 前端语音播放（按需加载）

AI 回复消息默认显示文字 + 🔊 小喇叭图标。用户点击喇叭才发起 TTS 请求，期间显示"⏳ 合成中…"，加载完成后切换为音频播放器、隐藏文字。用户可随时点击"💬 点击显示文字"切换回文字模式。TTS 失败静默回退到文字模式。

---

## 八、权限模型（v0.8）

### 8.1 用户角色

| 角色 | 产生方式 | 能力 |
|------|----------|------|
| 创建者（Creator） | 自己创建替身 | 全部能力：对话 + 记忆/喂数据/人设/声音/共享 |
| 接收者（Receiver） | 通过共享码绑定 | 仅对话 + 解绑 |

### 8.2 前端实现

`loadReceived()` 从 `/api/avatars/received` 获取绑定替身列表，在每个 avatar 对象上标记 `_isReceived: true`。

`setCurrentAvatar(avatar)` 检查该标记，动态控制侧边栏和移动端横向导航的显示：

```
_isReceived = true  → 隐藏：记忆 / 喂数据 / 人设 / 声音 / 共享
                      显示：解绑替身
_isReceived = false → 全部管理 Tab 正常显示
```

### 8.3 后端权限保障

各管理接口在后端独立做 `creator_id == g.user_id` 校验，前端隐藏仅为 UX 优化，不构成安全边界。接收者即使直接调用管理接口也会收到 403。

---

## 九、向量检索设计（v0.6）

### 9.1 Embedding 方案

- **调用方：** DeepSeek Embedding API（与 Chat API 共用同一 key）
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

### 9.4 降级链路

```
DeepSeek embed 失败
  └→ Chroma 无法查询
       └→ 自动降级：SQLite 关键词 + bigram + LLM 同义扩展
            └→ 功能照常，检索质量略降
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
| `DEEPSEEK_API_KEY` | — | DeepSeek Chat + Embed 共用 |
| `DEEPSEEK_EMBED_MODEL` | `text-embedding-v2` | Embedding 模型 |
| `CHROMA_PATH` | `data/chroma` | Chroma 持久化目录 |
| `RAG_TOP_K` | 6 | 每次对话检索记忆条数 |
| `RAG_POOL_LIMIT` | 500 | SQLite 降级时的候选池上限 |
| `EXTRACT_MAX_CHARS` | None（不限） | LLM 语义提取的输入深度 |
| `STYLE_PROFILE_MAX_CHARS` | 300 | 风格档案字数上限 |
| `VOLC_APP_ID` | — | 火山引擎 App ID（声音复刻 upload/train/status 用） |
| `VOLC_API_KEY` | — | 火山引擎 API Key（TTS WebSocket + ICL HTTP 用） |
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

POST /api/chat/:id/message           发送消息（支持 image_description、voice 参数）
GET  /api/chat/:id/history           对话历史

POST   /api/media/voice-sample/:id          上传声音样本（multipart，自动触发训练）
DELETE /api/media/voice-sample/:id/:sid     删除声音样本
GET    /api/media/voice-status/:id          查询声音克隆状态 + 样本列表
POST   /api/media/tts/:id                   TTS 合成（返回 audio/mpeg 二进制）
POST   /api/media/asr                       语音识别（WAV → 文字）

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
```

### 管理后台认证

管理后台使用 `require_admin` 装饰器，在 JWT 认证基础上额外校验 `users.is_admin = 1`。Web 页面入口为 `/admin`（服务 `frontend/admin.html`）。

---

## 十三、待解决问题与后续规划

| 优先级 | 问题 | 方案 |
|--------|------|------|
| 高 | 声音克隆训练状态轮询 | 前端自动轮询 voice-status，训练完成时 toast 提示 |
| 中 | TTS 流式播放（减少首字节延迟） | 考虑 WebSocket 流式返回音频片段 |
| 中 | 手动/自动记忆冲突检测 | priority 权重已设计，冲突提示待做 |
| 中 | Function Calling（工具调用型检索） | LLM 自主决定是否 RAG，替代硬编码 |
| 低 | persona_prompt 滚动重建 | 记忆超 N 条时增量更新 |
| 低 | 多用户 Chroma 隔离 | 切换为 per-avatar collection 或 Qdrant |
| 低 | 多语言支持 | 预留，暂不做 |
