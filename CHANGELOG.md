# Changelog

All notable changes to this project will be documented in this file.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.5.0] — 2026-06-29

### Summary
Major architectural upgrade: style profile system, vector retrieval, memory priority weighting, raw data archiving, and configurable processing depth.

### Added

**`backend/services/llm_provider.py`**
- `embed(text) -> Optional[List[float]]` — single-text DeepSeek Embedding API call; returns `None` on failure (caller falls back to keyword search)
- `batch_embed(texts) -> List[Optional[List[float]]]` — array input, single API call; index-aligned result list

**`backend/services/memory_store.py`**
- `get_style_samples(avatar_id, limit)` — queries `wechat`/`style` memories directly, used exclusively by `rebuild_persona_prompt`
- `_try_embed(content, mem_type)` — computes embedding for `episode`/`opinion` on single insert
- `_vector_search(rows, query_vec, top_k)` — cosine similarity ranking with priority/mem_type weighting
- `_keyword_search(rows, query, top_k)` — refactored keyword path, now shares weighting logic with vector path
- `_cosine(a, b)` — pure-Python cosine similarity (no numpy required)

**`backend/api/routes/ingest.py`**
- `_save_raw_upload(avatar_id, creator_id, filename, file_type, content)` — stores full raw text in `raw_uploads` with SHA256 dedup; idempotent
- `_generate_style_profile(style_samples, avatar)` — calls LLM to extract speech habits (address terms, fillers, rhythm, emoji use); stores ≤`STYLE_PROFILE_MAX_CHARS` chars in `avatars.style_profile`

**`backend/db/database.py`**
- New table `raw_uploads` (id, avatar_id, creator_id, filename, file_type, content TEXT, char_count, content_hash, process_version, processed_at, created_at)
- Indexes: `idx_raw_uploads_avatar`, unique `idx_raw_uploads_hash(avatar_id, content_hash)`
- Migrations: `avatars.style_profile TEXT`, `memories.priority INTEGER DEFAULT 0`, `memories.embedding TEXT`

**`backend/core/config.py`**
- `DEEPSEEK_EMBED_MODEL` (default: `text-embedding-v2`)
- `RAG_TOP_K` (default: `6`)
- `RAG_POOL_LIMIT` (default: `500`)
- `EXTRACT_MAX_CHARS` (default: `None` = unlimited; future billing tier hook)
- `STYLE_PROFILE_MAX_CHARS` (default: `300`)

### Changed

**`backend/services/memory_store.py`**
- `add_memory` — new `priority: int = 0` parameter; stores `embedding` for `episode`/`opinion` types
- `add_memories_batch` — new `priority` field in items dict; batch embeds `episode`/`opinion` in a single API call before insert; returns `{"added": int, "skipped": int}` (unchanged)
- `search_memories` — permanently excludes `wechat`/`style` types; tries vector cosine first, falls back to keyword; applies `priority=1 ×1.5` and `opinion ×1.3` in both paths; uses `config.RAG_TOP_K` / `config.RAG_POOL_LIMIT`
- `get_recent_memories` — now excludes `wechat`/`style` types (breaking: callers that relied on these types must use `get_style_samples` instead)

**`backend/services/persona_builder.py`**
- `build_system_prompt` — injects `style_profile` block between memories and identity_desc; removes `wechat`/`style` from inline memory rendering (they no longer reach RAG)
- `rebuild_persona_prompt` — fetches style samples via `get_style_samples()` instead of filtering `get_recent_memories()`; guards on `not style_samples and not opinion_samples`

**`backend/api/routes/ingest.py`**
- `_do_import` — new params `raw_content`, `raw_filename`, `raw_file_type`; calls `_save_raw_upload` and `_generate_style_profile`; reports `style_profile_updated` in response
- `paste_text` — passes raw text params to `_do_import`
- `upload_wechat` — saves each file individually via `_save_raw_upload` before merging; passes `raw_content=None` to avoid double-save
- `_extract_semantic_memories` — uses `config.EXTRACT_MAX_CHARS` (falls back to 3000 if None)

**`backend/api/routes/memories.py`**
- `add` (POST) — passes `priority=1` to `add_memory` for user-manually-added memories

### Architecture decisions recorded in
- `ARCHITECTURE_CN.md` (v0.5)
- `ARCHITECTURE_EN.md` (v0.5)

---

## [0.4.0] — 2025

- Multi-relationship types (son, daughter, boyfriend, girlfriend, elder_brother, younger_sister, …)
- `identity_desc` field on avatars (creator-written, highest priority in system prompt)
- Multi-file upload (.txt + .html); HTML export parser with 3-level fallback
- Memory deduplication (content hash in `add_memories_batch`)
- Message-level dedup in `_merge_parsed` (timestamp + sender + content key)
- PC WeChat HTML parser: `_parse_html_blocks` → `_parse_ts_name_content` → `parse_text` fallback
- Sender detection endpoint (`/detect`) for two-step import confirmation
- Delete avatar endpoint (soft delete)

## [0.3.0] — 2025

- Architecture refactor: sensitive topics changed to prompt injection model
- Repeat-topic emotion awareness: detects repeated keywords in recent history, injects empathy hint
- `topic_guard.py` rewritten with semantic LLM matching
- Global sensitive topics (avatar_id = NULL) replace per-avatar topics

## [0.2.0] — 2024

- Chinese bigram indexing for memory retrieval
- LLM query expansion (synonym generation, cached in `query_expansions` table)
- No-memory graceful degradation in system prompt
- Query expansion results persisted to DB (two-level cache: in-process dict + SQLite)

## [0.1.0] — 2024

- Initial version: basic chat, SQLite, DeepSeek integration
- JWT authentication
- Avatar creation, memory import, conversation history
