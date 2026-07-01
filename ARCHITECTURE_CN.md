# 替身 (Shuangshen) 系统架构文档

**版本：** v0.6  
**更新日期：** 2026-06-30  
**状态：** v0.5 已实现；v0.6 Chroma 向量库已接入

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
| 前端 | 单文件 HTML + Vanilla JS | 零依赖，便于嵌入 |
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
  voice_model_id  TEXT,   -- 声音克隆（预留）
  status, share_code, created_at, updated_at
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
   全局敏感话题 → 生成 topic_hints 字符串
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
   │ 【回复长度规则】— avg_reply_chars             │
   │ 【时间情景提示】— 当前时段（凌晨/早上/…）    │
   │ （可选）敏感话题提示                          │
   └──────────────────────────────────────────────┘
    │
    ▼
④ 调用 DeepSeek Chat API
    │
    ▼
⑤ 返回回复，写入 chat_messages 表（SQLite）
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

## 七、向量检索设计（v0.6）

### 7.1 Embedding 方案

- **调用方：** DeepSeek Embedding API（与 Chat API 共用同一 key）
- **写入时机：** `add_memories_batch` 批量 embed 后 upsert Chroma；单条 `add_memory` 单次 embed
- **存储：** Chroma `memories` collection（不再存 SQLite `embedding` 字段）

### 7.2 Chroma Collection 结构

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

### 7.3 检索加权逻辑

```python
# Chroma 返回 cosine distance（0=完全一致）→ 转为相似度分
score = 1.0 - distance

if priority == 1:       score *= 1.5   # 用户手动记忆加权
if mem_type == 'opinion': score *= 1.3 # 观点类记忆加权

results.sort(by=score, desc).take(top_k)
```

### 7.4 降级链路

```
DeepSeek embed 失败
  └→ Chroma 无法查询
       └→ 自动降级：SQLite 关键词 + bigram + LLM 同义扩展
            └→ 功能照常，检索质量略降
```

### 7.5 扩容路径

| 规模 | 方案 |
|------|------|
| 当前（单用户 3GB 聊天记录） | Chroma 本地持久化 |
| 多用户生产环境 | Qdrant / pgvector（更换 chroma_store.py 适配层即可） |

---

## 八、原始数据永久存储

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

## 九、配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `DEEPSEEK_API_KEY` | — | DeepSeek Chat + Embed 共用 |
| `DEEPSEEK_EMBED_MODEL` | `text-embedding-v2` | Embedding 模型 |
| `CHROMA_PATH` | `data/chroma` | Chroma 持久化目录 |
| `RAG_TOP_K` | 6 | 每次对话检索记忆条数 |
| `RAG_POOL_LIMIT` | 500 | SQLite 降级时的候选池上限 |
| `EXTRACT_MAX_CHARS` | None（不限） | LLM 语义提取的输入深度 |
| `STYLE_PROFILE_MAX_CHARS` | 300 | 风格档案字数上限 |

**计划分层（待产品决策）：**

| 用户档位 | EXTRACT_MAX_CHARS | RAG_POOL_LIMIT |
|----------|-------------------|----------------|
| 免费 | 5,000 字 | 500 条 |
| 标准 | 20,000 字 | 2,000 条 |
| 高级 | 不限 | 全量（Chroma 向量检索） |

---

## 十、接口概览

```
POST /api/auth/register
POST /api/auth/login

GET/POST  /api/avatars               创建/列出替身
GET/PUT   /api/avatars/:id           详情/更新
DELETE    /api/avatars/:id           软删除
POST      /api/avatars/:id/share     获取共享码
POST      /api/avatars/join/:code    绑定替身

POST /api/ingest/:id/paste           粘贴文本导入
POST /api/ingest/:id/wechat          文件上传（多文件，.txt/.html）
POST /api/ingest/:id/detect          检测发言人（不导入）
GET  /api/ingest/:id/jobs            导入历史（含 date_start/date_end）
POST /api/ingest/:id/reparse         从原始记录重新解析（含 Chroma 同步）
GET  /api/ingest/:id/raw_count       原始记录数量

POST /api/chat/:id/message           发送消息
GET  /api/chat/:id/history           对话历史

GET/POST  /api/topics                全局敏感话题

GET    /api/memories/:id             查看记忆库
POST   /api/memories/:id             手动添加记忆（priority=1）
DELETE /api/memories/:id/:mem_id     删除记忆
```

---

## 十一、待解决问题与后续规划

| 优先级 | 问题 | 方案 |
|--------|------|------|
| 中 | 声音克隆 | CosyVoice 2，暂缓 |
| 中 | 手动/自动记忆冲突检测 | priority 权重已设计，冲突提示待做 |
| 中 | Function Calling（工具调用型检索） | LLM 自主决定是否 RAG，替代硬编码 |
| 低 | persona_prompt 滚动重建 | 记忆超 N 条时增量更新 |
| 低 | 多用户 Chroma 隔离 | 切换为 per-avatar collection 或 Qdrant |
| 低 | 多语言支持 | 预留，暂不做 |
