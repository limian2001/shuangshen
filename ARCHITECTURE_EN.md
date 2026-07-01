# Shuangshen (替身) — System Architecture

**Version:** v0.6  
**Last Updated:** 2026-06-30  
**Status:** v0.5 fully implemented; v0.6 Chroma vector store integrated

---

## Version History

| Version | Date | Key Changes |
|---------|------|-------------|
| v0.1 | 2024 | Initial: basic chat, SQLite, DeepSeek |
| v0.2 | 2024 | Bigram retrieval, LLM query expansion, query cache |
| v0.3 | 2025 | Architecture refactor: topic injection model, repeat-topic emotion awareness |
| v0.4 | 2025 | Multi-relationship types, identity_desc, multi-file import, HTML parser, memory dedup |
| v0.5 | 2026-06 | Style profile system, vector retrieval (SQLite), memory priority, raw data storage, time-context injection |
| v0.6 | 2026-06 | Vector store migrated to Chroma; dual-write storage architecture; memory chat_date; import date-range tracking; WeChat long-press copy format parser |

---

## 1. Project Overview

Shuangshen is an AI digital-avatar platform. Creators upload real chat histories and define a persona; the system learns their speech style and personal characteristics, generating an AI avatar that family and friends can converse with.

**Core design principles:**
- Not a generic chatbot — a personality-faithful digital twin
- Creator-defined persona (identity_desc) > extracted memories > LLM general knowledge
- When uncertain, respond vaguely rather than fabricate facts

---

## 2. Tech Stack

| Component | Current Solution | Notes |
|-----------|-----------------|-------|
| Backend | Flask 3.x | Python 3.9+ |
| Relational DB | SQLite (WAL mode) | Migratable to PostgreSQL |
| Vector DB | ChromaDB (local persistent) | v0.6; replaces SQLite embedding field |
| LLM | DeepSeek Chat (`deepseek-chat`) | OpenAI-compatible endpoint |
| Embeddings | DeepSeek Embedding API (`text-embedding-v2`) | Shared API key with Chat |
| Frontend | Single-file HTML + Vanilla JS | Zero dependencies |
| Auth | JWT Bearer Token | Stateless |

---

## 3. Storage Architecture

### 3.1 Dual-Write Design Principle

From v0.6, episode/opinion memories are written to both SQLite and Chroma with distinct responsibilities:

```
SQLite memories table  ←  Source of truth: metadata, content, keywords, UI display, fallback search
        ↓ embed (DeepSeek API)
Chroma memories        ←  Search acceleration layer: HNSW vector index, handles similarity search only
```

**SQLite is the sole source of truth; Chroma is a rebuildable index.** If Chroma data is lost, `sync_to_chroma()` can rebuild the entire vector index from SQLite with no data loss. The reverse is not possible.

### 3.2 What Lives Where

| Storage | Content |
|---------|---------|
| SQLite `raw_uploads` | Full raw chat text (permanent, SHA256 dedup) |
| SQLite `memories` | All memory entries: content, metadata, keyword index |
| SQLite `avatars` | Persona, style_profile, avg_reply_chars, persona_prompt |
| SQLite `chat_messages` | Real-time conversation history with avatar |
| SQLite `ingest_jobs` | Import job records (including date_start / date_end) |
| **Chroma** `memories` | Vector index for episode/opinion types only (RAG retrieval) |
| Local disk | `data/shuangshen.db` (SQLite file) and `data/chroma/` (Chroma directory) |

> **Backup rule: only `data/` needs to be backed up.** Everything else can be rebuilt from code.

### 3.3 Why Dual-Write Instead of Chroma-Only

Chroma handles vector search; SQLite has three irreplaceable roles:

1. **Fallback retrieval:** When the DeepSeek Embedding API is unavailable, the system automatically falls back to SQLite keyword + bigram search. Without SQLite, an API outage would cause complete failure.
2. **UI display and pagination:** Memory list sorting, pagination, date/type filtering — all natural SQL queries. Chroma is not a relational database and has limited metadata query support.
3. **Chroma is rebuildable:** `sync_to_chroma(avatar_id)` re-embeds all SQLite memories and upserts them into Chroma. This is the recovery path for data repair and is automatically called after reparse.

### 3.4 wechat / style Types Are Excluded from Chroma

These types are used only for generating `style_profile` and `persona_prompt`, not for RAG retrieval. They are never embedded or written to Chroma.

---

## 4. Data Model

### 4.1 Core Tables

```sql
-- Avatar table
avatars (
  id, creator_id, name, relationship,
  persona_prompt  TEXT,    -- LLM-generated personality summary (≤300 chars)
  style_profile   TEXT,    -- LLM-extracted speech style profile (≤300 chars)
  avg_reply_chars INTEGER, -- Average reply length; injected to control response length
  identity_desc   TEXT,    -- Creator-written persona description (highest priority)
  voice_model_id  TEXT,    -- Voice cloning model (reserved)
  status, share_code, created_at, updated_at
)

-- Memory table
memories (
  id, avatar_id,
  content    TEXT,         -- Memory content
  mem_type   TEXT,         -- episode | opinion | style | wechat
  priority   INTEGER DEFAULT 0, -- 0=system-extracted, 1=user-manual (RAG ×1.5)
  chat_date  TEXT,         -- Original chat date (NULL if no chat source)
  topic_tags TEXT,         -- JSON array
  keywords   TEXT,         -- JSON keyword weights (fallback retrieval)
  source     TEXT,         -- manual | wechat_import | llm_extracted
  created_at TEXT          -- DB insert time (distinct from chat_date)
  -- Note: embedding column deprecated; vectors stored in Chroma
)

-- Raw uploads table
raw_uploads (
  id, avatar_id, creator_id,
  filename        TEXT,
  file_type       TEXT,    -- txt | html | paste
  content         TEXT,    -- Full raw text (permanent)
  char_count      INTEGER,
  content_hash    TEXT,    -- SHA256, prevents duplicate storage
  process_version TEXT,    -- Algorithm version at processing time
  processed_at    TEXT,    -- NULL = not yet processed
  created_at
)

-- Import jobs table
ingest_jobs (
  id, avatar_id, filename, status,
  total_msgs  INTEGER,
  imported    INTEGER,
  date_start  TEXT,        -- Earliest chat date in this import
  date_end    TEXT,        -- Latest chat date in this import
  error_msg   TEXT,
  created_at, finished_at
)
```

### 4.2 Memory Type Reference

| mem_type | Source | In Chroma | Participates in RAG | Purpose |
|----------|--------|:---------:|:-------------------:|---------|
| `episode` | LLM-extracted from chat | ✅ | ✅ | Factual memories (location, job, events) |
| `opinion` | LLM-extracted from chat | ✅ | ✅ (×1.3 weight) | Personal views, preferences, attitudes |
| `style` | LLM-extracted from chat | ❌ | ❌ | Feeds style_profile generation only |
| `wechat` | Raw chat samples | ❌ | ❌ | Feeds persona_prompt / style_profile only |

### 4.3 Memory Priority and chat_date

```
priority = 1 (user-manual)      →  RAG score × 1.5
priority = 0 (system-extracted) →  RAG score × 1.0

chat_date (non-null)  →  UI displays original chat date (e.g. "2020年11月23日")
chat_date (NULL)      →  UI displays created_at (import timestamp)
```

---

## 5. Data Ingestion Pipeline

### 5.1 Full Flow

```
User uploads .txt / .html or pastes text
        │
        ▼
① Save raw text → raw_uploads (SQLite) ← permanent, SHA256 dedup
        │
        ▼
② Parse (wechat_parser.py)
   Four supported formats:
   • WeChat PC export TXT (timestamp + sender on same line)
   • WeChat long-press multi-select copy (sender / full-date / content on separate lines) ← v0.6
   • Mobile copy-paste (sender + HH:MM on same line)
   • Simple format "Name: content"
        │
        ├──────────────────────────────────────┐
        ▼                                      ▼
③ My messages (my_messages)           All messages (all_messages)
        │                                      │
        ├→ memories, mem_type=wechat (SQLite)  │
        │   preserve chat_date from timestamp  │
        │                                      ▼
        ├→ avatars.style_profile (SQLite)   ④ LLM semantic extraction
        │   LLM extracts speech style, ≤300c   → episode (facts / events)
        │                                      → opinion (views / preferences)
        └→ avatars.avg_reply_chars (SQLite)    write to memories (SQLite)
            average reply length               upsert to Chroma (vector)
                                               chat_date = conversation start date
        │
        ▼
⑤ rebuild_persona_prompt → avatars.persona_prompt (SQLite)
```

### 5.2 Reparse

Clicking "Reparse" re-reads `raw_uploads` and re-runs the full pipeline. Because `add_memories_batch` skips content already in SQLite, reparse explicitly calls `sync_to_chroma(avatar_id)` afterward to sync all episode/opinion memories from SQLite into Chroma.

### 5.3 Import Date Tracking

On each import, the earliest and latest parsed chat timestamps are written to `ingest_jobs.date_start / date_end`. The UI import history displays this as `📅 2020-11-23 ~ 2021-03-15`, helping users avoid gaps or duplicate imports.

---

## 6. Conversation Flow

For each user message:

```
User message
    │
    ▼
① Topic guard (topic_guard.py)
   Check global sensitive topics → generate topic_hints string
    │
    ▼
② Memory retrieval (memory_store.search_memories)
   ┌──────────────────────────────────────────────────┐
   │ Primary:  Chroma vector search (HNSW, cosine)    │
   │ Fallback: SQLite keyword + bigram + LLM expansion│
   └──────────────────────────────────────────────────┘
   Filter: episode + opinion types only
   Weights: priority=1 ×1.5, opinion ×1.3
   Return: top_k results (default 6)
    │
    ▼
③ Build System Prompt (persona_builder.build_system_prompt)
   ┌────────────────────────────────────────────────────┐
   │ Role definition (relationship template)             │
   │ Behavior rules (no fabrication, no AI reveal, etc.) │
   │ [Relevant Memories] — RAG top_k results             │
   │ [Speech Style Profile] — style_profile ≤300 chars   │
   │ [Persona Background] — identity_desc (creator)      │
   │ [Personality Analysis] — persona_prompt (LLM)       │
   │ [Reply Length Rule] — avg_reply_chars               │
   │ [Time Context Hint] — current time period           │
   │ (optional) Sensitive topic hints                    │
   └────────────────────────────────────────────────────┘
    │
    ▼
④ Call DeepSeek Chat API
    │
    ▼
⑤ Return response, write to chat_messages (SQLite)
```

### System Prompt Block Priority

| Block | Priority | Role |
|-------|----------|------|
| identity_desc (creator-written) | **Highest** | Core definition; LLM must comply |
| style_profile (style archive) | High | Speech habits, address preferences |
| persona_prompt (personality summary) | Medium | Overall character analysis |
| Relevant memories (RAG) | Medium | Dynamic, changes per query |
| Relationship template | Base | Tone foundation for all responses |

---

## 7. Vector Retrieval Design (v0.6)

### 7.1 Embedding Approach

- **Provider:** DeepSeek Embedding API (shared API key with Chat)
- **Write path:** `add_memories_batch` batch-embeds then upserts to Chroma; `add_memory` single-embeds
- **Storage:** Chroma `memories` collection (SQLite `embedding` column deprecated)

### 7.2 Chroma Collection Structure

```python
collection.upsert(
    ids        = [memory_id],        # Same ID as SQLite memories.id
    documents  = [content],
    embeddings = [vec],              # DeepSeek-computed, passed externally
    metadatas  = [{
        "avatar_id":  avatar_id,     # Used for where-filter
        "mem_type":   "episode",
        "priority":   0,
        "topic_tags": '["fact"]',
    }],
)
```

### 7.3 Retrieval Weighting

```python
# Chroma returns cosine distance (0=identical) → convert to similarity score
score = 1.0 - distance

if priority == 1:          score *= 1.5   # User-manual memory boost
if mem_type == 'opinion':  score *= 1.3   # Opinion-type boost

results.sort(by=score, desc).take(top_k)
```

### 7.4 Fallback Chain

```
DeepSeek embed fails
  └→ Chroma query not possible
       └→ Auto-fallback: SQLite keyword + bigram + LLM query expansion
            └→ System remains functional; retrieval quality slightly reduced
```

### 7.5 Scaling Roadmap

| Scale | Solution |
|-------|----------|
| Current (single user, ~3 GB chat data) | Chroma local persistent |
| Multi-tenant production | Qdrant / pgvector (swap out chroma_store.py adapter) |

---

## 8. Raw Data Permanent Storage

**Principle:** Raw text is preserved permanently in `raw_uploads`. Processing logic can be re-run as algorithms evolve.

```
raw_uploads.content          ← Full raw text (survives original file deletion)
raw_uploads.content_hash     ← SHA256; prevents duplicate storage of same content
raw_uploads.process_version  ← Tracks which algorithm version processed this upload
raw_uploads.processed_at     ← NULL = not yet processed / pending reprocessing
```

**Scaling path:**
1. **Current:** TEXT in SQLite — simple, reliable, sufficient for MVP
2. **At scale:** Add `storage_url` column; migrate content to OSS (Alibaba Cloud / S3); SQLite retains metadata only

---

## 9. Configuration

| Config Key | Default | Description |
|------------|---------|-------------|
| `DEEPSEEK_API_KEY` | — | Shared key for Chat + Embedding |
| `DEEPSEEK_EMBED_MODEL` | `text-embedding-v2` | Embedding model |
| `CHROMA_PATH` | `data/chroma` | Chroma persistence directory |
| `RAG_TOP_K` | 6 | Memories retrieved per conversation turn |
| `RAG_POOL_LIMIT` | 500 | SQLite fallback candidate pool cap |
| `EXTRACT_MAX_CHARS` | None (unlimited) | LLM semantic extraction input depth |
| `STYLE_PROFILE_MAX_CHARS` | 300 | Max chars for style profile |

**Planned tiers (product decision pending):**

| Tier | EXTRACT_MAX_CHARS | RAG_POOL_LIMIT |
|------|-------------------|----------------|
| Free | 5,000 chars | 500 records |
| Standard | 20,000 chars | 2,000 records |
| Premium | Unlimited | Full history (Chroma vector) |

---

## 10. API Overview

```
POST /api/auth/register
POST /api/auth/login

GET/POST  /api/avatars               Create / list avatars
GET/PUT   /api/avatars/:id           Detail / update
DELETE    /api/avatars/:id           Soft delete
POST      /api/avatars/:id/share     Get share code
POST      /api/avatars/join/:code    Bind avatar

POST /api/ingest/:id/paste           Import via pasted text
POST /api/ingest/:id/wechat          File upload (multi-file, .txt/.html)
POST /api/ingest/:id/detect          Detect senders (no import)
GET  /api/ingest/:id/jobs            Import history (includes date_start/date_end)
POST /api/ingest/:id/reparse         Reparse from raw records (includes Chroma sync)
GET  /api/ingest/:id/raw_count       Raw record count

POST /api/chat/:id/message           Send message
GET  /api/chat/:id/history           Conversation history

GET/POST  /api/topics                Global sensitive topics

GET    /api/memories/:id             Browse memory store
POST   /api/memories/:id             Manually add memory (priority=1)
DELETE /api/memories/:id/:mem_id     Delete memory
```

---

## 11. Open Issues & Roadmap

| Priority | Issue | Solution |
|----------|-------|----------|
| Medium | Voice cloning | CosyVoice 2, deferred |
| Medium | Manual vs. auto memory conflict detection | Priority weighting done; conflict hints pending |
| Medium | Function Calling (agentic retrieval) | LLM decides whether to RAG, replaces hardcoded pipeline |
| Low | Incremental persona_prompt rebuild | Trigger on memory count threshold |
| Low | Per-avatar Chroma isolation at scale | Switch to per-avatar collections or Qdrant namespaces |
| Low | Multi-language support | Reserved, not planned |
