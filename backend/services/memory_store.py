from __future__ import annotations
"""
记忆存取服务 v0.6

检索策略：
  - 优先：Chroma 向量库（HNSW 索引，余弦相似度）
  - 降级：SQLite 关键词 + bigram 打分（Chroma 不可用时自动切换）
  - wechat / style 类型永久排除在 RAG 之外（仅供 persona/style_profile 生成用）
  - priority=1（用户手动添加）的记忆 RAG 打分 ×1.5

存储分工：
  - SQLite：所有记忆的元数据（content、mem_type、topic_tags、source、
            created_at、chat_date、priority）+ 关键词索引（降级用）
  - Chroma：episode/opinion 的向量索引（不再在 SQLite 存 embedding）
"""
import json
import re
from collections import Counter
from typing import List, Optional


from backend.db.database import get_db, rows_to_list
from backend.utils.auth import new_id
from backend.core.config import config

# wechat / style 类型不参与 RAG 检索，也不写入 Chroma
_NON_RAG_TYPES = ("wechat", "style")


# ─────────────────────────────────────────────
# 写入
# ─────────────────────────────────────────────

def add_memory(
    avatar_id: str,
    content: str,
    mem_type: str = "opinion",
    topic_tags: Optional[List[str]] = None,
    source: str = "manual",
    priority: int = 0,
    chat_date: Optional[str] = None,
) -> dict:
    """
    添加单条记忆。
    episode/opinion 类型同时写入 Chroma 向量库。
    priority=1 表示用户手动添加（RAG 打分 ×1.5）。
    """
    mid = new_id()
    tags_str = json.dumps(topic_tags or [], ensure_ascii=False)
    keywords = json.dumps(_extract_keywords(content), ensure_ascii=False)

    # ① 写 SQLite（元数据 + 关键词，不再存 embedding）
    with get_db() as conn:
        conn.execute(
            """INSERT INTO memories
               (id, avatar_id, content, mem_type, topic_tags, source, keywords, priority, chat_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, avatar_id, content, mem_type, tags_str, source, keywords, priority, chat_date or None),
        )

    # ② episode/opinion → 写 Chroma
    if mem_type not in _NON_RAG_TYPES:
        try:
            from backend.services.llm_provider import llm
            from backend.services.chroma_store import upsert_memories
            vec = llm.embed(content)
            if vec:
                upsert_memories([{
                    "id": mid, "content": content, "embedding": vec,
                    "avatar_id": avatar_id, "mem_type": mem_type,
                    "priority": priority, "topic_tags": tags_str,
                }])
        except Exception as e:
            print(f"[MEMORY] Chroma upsert 失败（记忆已存 SQLite）: {e}")

    return {"id": mid, "content": content, "mem_type": mem_type}


def add_memories_batch(avatar_id: str, items: List[dict]) -> dict:
    """
    批量添加记忆（用于微信导入），自动跳过重复内容。

    存储分工：
      - 所有类型 → SQLite（元数据 + 关键词）
      - episode/opinion → 同时写 Chroma（向量索引）

    Returns:
        {"added": int, "skipped": int}
    """
    if not items:
        return {"added": 0, "skipped": 0}

    # 拉取已有内容做去重
    with get_db() as conn:
        existing: set = {
            row[0] for row in conn.execute(
                "SELECT content FROM memories WHERE avatar_id = ?", (avatar_id,)
            ).fetchall()
        }

    to_insert = []
    skipped = 0
    for item in items:
        content = item.get("content", "").strip()
        if not content or content in existing:
            skipped += 1
            continue
        to_insert.append(item)
        existing.add(content)

    if not to_insert:
        return {"added": 0, "skipped": skipped}

    # ── 批量计算 embedding（仅 episode/opinion）──
    rag_candidates = [
        (i, item["content"], item)
        for i, item in enumerate(to_insert)
        if item.get("mem_type", "wechat") not in _NON_RAG_TYPES
    ]
    raw_vecs: dict = {}   # index → List[float]
    if rag_candidates:
        try:
            from backend.services.llm_provider import llm
            texts = [c for _, c, _ in rag_candidates]
            vecs = llm.batch_embed(texts)
            for (idx, _, _), vec in zip(rag_candidates, vecs):
                if vec:
                    raw_vecs[idx] = vec
        except Exception as e:
            print(f"[MEMORY] batch_embed 失败（降级关键词）: {e}")

    # ── ① 批量写 SQLite ──
    id_map: dict = {}   # index → memory_id（供 Chroma 写入用）
    added = 0
    with get_db() as conn:
        for i, item in enumerate(to_insert):
            content = item["content"].strip()
            mid = new_id()
            id_map[i] = mid
            tags = json.dumps(item.get("topic_tags", []), ensure_ascii=False)
            kw = json.dumps(_extract_keywords(content), ensure_ascii=False)
            priority = int(item.get("priority", 0))
            chat_date = item.get("chat_date") or None
            conn.execute(
                """INSERT INTO memories
                   (id, avatar_id, content, mem_type, topic_tags, source, keywords, priority, chat_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mid, avatar_id, content,
                 item.get("mem_type", "wechat"),
                 tags, item.get("source", "wechat_import"), kw, priority, chat_date),
            )
            added += 1

    # ── ② 批量写 Chroma（episode/opinion 且有向量）──
    chroma_items = []
    for idx, content, item in rag_candidates:
        vec = raw_vecs.get(idx)
        if vec:
            chroma_items.append({
                "id":         id_map[idx],
                "content":    content,
                "embedding":  vec,
                "avatar_id":  avatar_id,
                "mem_type":   item.get("mem_type", "episode"),
                "priority":   int(item.get("priority", 0)),
                "topic_tags": json.dumps(item.get("topic_tags", []), ensure_ascii=False),
            })
    if chroma_items:
        try:
            from backend.services.chroma_store import upsert_memories
            upsert_memories(chroma_items)
        except Exception as e:
            print(f"[MEMORY] Chroma 批量写入失败（记忆已存 SQLite，检索降级关键词）: {e}")

    if skipped:
        print(f"[MEMORY] avatar={avatar_id} 跳过重复/空 {skipped} 条，写入 {added} 条（Chroma {len(chroma_items)} 条）")
    return {"added": added, "skipped": skipped}


# ─────────────────────────────────────────────
# 检索
# ─────────────────────────────────────────────

# 进程内一级缓存（减少 DB 读次数）
_query_expand_cache: dict[str, list[str]] = {}


def search_memories(
    avatar_id: str,
    query: str,
    top_k: Optional[int] = None,
) -> List[dict]:
    """
    检索最相关的记忆片段 v0.6。

    策略（自动选择）：
    1. Chroma 向量检索（HNSW，余弦距离，优先）
    2. SQLite 关键词检索（bigram + LLM 同义扩展，降级）

    过滤：wechat / style 类型永久排除（Chroma 写入时已过滤，SQLite 降级时显式过滤）
    加权：priority=1 ×1.5，opinion ×1.3（两条路径均一致）
    """
    if top_k is None:
        top_k = config.RAG_TOP_K

    # ── 优先：Chroma 向量检索 ──
    try:
        from backend.services.llm_provider import llm
        from backend.services.chroma_store import query_memories as chroma_query
        query_vec = llm.embed(query)
        if query_vec:
            results = chroma_query(avatar_id, query_vec, top_k=top_k)
            if results:
                return results
            # results 为空说明该替身在 Chroma 里没有记忆，降级到关键词
    except Exception as e:
        print(f"[MEMORY] Chroma 检索失败，降级关键词: {e}")

    # ── 降级：SQLite 关键词检索 ──
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, content, mem_type, topic_tags, keywords, priority, created_at
               FROM memories
               WHERE avatar_id = ? AND mem_type NOT IN ('wechat', 'style')
               ORDER BY created_at DESC LIMIT ?""",
            (avatar_id, config.RAG_POOL_LIMIT),
        ).fetchall()

    if not rows:
        return []

    return _keyword_search(rows, query, top_k)



def _keyword_search(rows, query: str, top_k: int) -> List[dict]:
    """关键词 + bigram + LLM 同义扩展检索 + priority/mem_type 加权"""
    base_words = set(_tokenize(query))
    expanded_words = set(_expand_query(query))

    query_weights: dict = {}
    for w in base_words:
        query_weights[w] = 1.0
    for w in expanded_words:
        if w not in query_weights:
            query_weights[w] = 0.7

    scored = []
    for row in rows:
        row_dict = dict(row)
        mem_kw = json.loads(row_dict.get("keywords") or "{}")
        score = sum(mem_kw.get(w, 0) * weight for w, weight in query_weights.items())
        if row_dict.get("priority") == 1:
            score *= 1.5
        if row_dict.get("mem_type") == "opinion":
            score *= 1.3
        if score > 0:
            scored.append((score, row_dict))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:top_k]]


def _expand_query(query: str) -> list[str]:
    """
    用 LLM 对查询做同义词扩展，返回相关词列表。

    缓存策略（两级）：
    1. 进程内字典（最快，重启清空）
    2. DB query_expansions 表（持久，重启后仍有效）
    同一查询只调用一次 LLM，后续直接复用。
    """
    cache_key = query.strip()[:50]

    # ── 一级缓存：进程内字典 ──
    if cache_key in _query_expand_cache:
        return _query_expand_cache[cache_key]

    # ── 二级缓存：DB 持久化 ──
    try:
        from backend.db.database import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT expanded FROM query_expansions WHERE query_hash = ?",
                (cache_key,),
            ).fetchone()
        if row:
            expanded = json.loads(row["expanded"])
            _query_expand_cache[cache_key] = expanded  # 回填一级缓存
            # 更新命中次数
            with get_db() as conn:
                conn.execute(
                    """UPDATE query_expansions
                       SET hit_count = hit_count + 1, updated_at = datetime('now')
                       WHERE query_hash = ?""",
                    (cache_key,),
                )
            return expanded
    except Exception:
        pass  # DB 查询失败不影响主流程

    # ── 缓存未命中：调用 LLM ──
    try:
        from backend.services.llm_provider import llm
        prompt = (
            f"对于这个问题：「{query}」\n"
            "请列出与其语义相关的中文关键词（同义词、近义词、相关概念），"
            "用于搜索相关记忆。只输出关键词，用逗号分隔，不超过10个词，不要解释。\n"
            "示例：收入怎么样 → 薪资,工资,月薪,年薪,挣钱,赚钱,收入"
        )
        raw = llm.chat(
            system_prompt="你是一个关键词提取助手，只输出逗号分隔的词，不输出其他内容。",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=60,
            temperature=0.1,
        ).strip()

        # 解析关键词并做 bigram 扩展
        words = [w.strip() for w in re.split(r'[,，、\s]+', raw) if w.strip()]
        expanded = []
        for w in words:
            expanded.append(w)
            chars = re.findall(r'[一-鿿]', w)
            for i in range(len(chars) - 1):
                expanded.append(chars[i] + chars[i + 1])

        # 写入一级缓存
        _query_expand_cache[cache_key] = expanded

        # 写入 DB 持久化
        try:
            from backend.db.database import get_db
            with get_db() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO query_expansions
                       (query_hash, query_text, expanded, hit_count, updated_at)
                       VALUES (?, ?, ?, 1, datetime('now'))""",
                    (cache_key, query.strip()[:200], json.dumps(expanded, ensure_ascii=False)),
                )
        except Exception as e:
            print(f"[MEMORY] 写入扩展缓存失败（不影响结果）: {e}")

        return expanded

    except Exception as e:
        print(f"[MEMORY] 查询扩展失败，使用原始词: {e}")
        _query_expand_cache[cache_key] = []
        return []


def sync_to_chroma(avatar_id: str) -> dict:
    """
    将某替身在 SQLite 里的 episode/opinion 记忆全量同步到 Chroma。

    使用场景：
      - 重新解析（reparse）后刷新向量库
      - 从旧版本迁移（Chroma 引入前已有的数据）

    返回 {"synced": int, "failed": int}
    """
    from backend.services.chroma_store import upsert_memories

    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT id, content, mem_type, priority, topic_tags
               FROM memories
               WHERE avatar_id = ? AND mem_type NOT IN ('wechat', 'style')""",
            (avatar_id,),
        ).fetchall())

    if not rows:
        return {"synced": 0, "failed": 0}

    try:
        from backend.services.llm_provider import llm
    except Exception as e:
        print(f"[MEMORY] sync_to_chroma: LLM 初始化失败: {e}")
        return {"synced": 0, "failed": len(rows)}

    CHUNK = 50   # DeepSeek batch_embed 单次上限
    synced = 0
    failed = 0

    for i in range(0, len(rows), CHUNK):
        chunk = rows[i : i + CHUNK]
        texts = [r["content"] for r in chunk]
        try:
            vecs = llm.batch_embed(texts)
        except Exception as e:
            print(f"[MEMORY] sync_to_chroma batch_embed 失败: {e}")
            failed += len(chunk)
            continue

        items = []
        for row, vec in zip(chunk, vecs):
            if vec:
                items.append({
                    "id":         row["id"],
                    "content":    row["content"],
                    "embedding":  vec,
                    "avatar_id":  avatar_id,
                    "mem_type":   row["mem_type"],
                    "priority":   int(row.get("priority") or 0),
                    "topic_tags": row.get("topic_tags") or "[]",
                })
            else:
                failed += 1

        if items:
            try:
                upsert_memories(items)
                synced += len(items)
            except Exception as e:
                print(f"[MEMORY] sync_to_chroma upsert 失败: {e}")
                failed += len(items)

    print(f"[MEMORY] sync_to_chroma avatar={avatar_id} 同步 {synced} 条，失败 {failed} 条")
    return {"synced": synced, "failed": failed}


def get_recent_memories(avatar_id: str, limit: int = 20) -> List[dict]:
    """
    获取最新的 episode/opinion 记忆（用于构建 Prompt 时兜底）。
    wechat/style 类型已排除，由 rebuild_persona_prompt 单独查询。
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, content, mem_type, topic_tags, created_at
               FROM memories
               WHERE avatar_id = ? AND mem_type NOT IN ('wechat', 'style')
               ORDER BY created_at DESC LIMIT ?""",
            (avatar_id, limit),
        ).fetchall()
    return rows_to_list(rows)


def get_style_samples(avatar_id: str, limit: int = 50) -> List[str]:
    """
    获取 wechat/style 类型记忆内容，供 rebuild_persona_prompt 使用。
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT content FROM memories
               WHERE avatar_id = ? AND mem_type IN ('wechat', 'style')
                 AND length(content) > 10
               ORDER BY created_at DESC LIMIT ?""",
            (avatar_id, limit),
        ).fetchall()
    return [row[0] for row in rows]


def list_memories(avatar_id: str, page: int = 1, per_page: int = 20) -> dict:
    offset = (page - 1) * per_page
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE avatar_id = ?", (avatar_id,)
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT id, content, mem_type, topic_tags, source, created_at, chat_date
               FROM memories WHERE avatar_id = ?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (avatar_id, per_page, offset),
        ).fetchall()
    return {"total": total, "page": page, "items": rows_to_list(rows)}


# ─────────────────────────────────────────────
# 关键词提取（开发用，生产换 Embedding）
# ─────────────────────────────────────────────

STOPWORDS = {
    "的", "了", "是", "在", "我", "他", "她", "你", "它", "这", "那", "和", "与",
    "也", "就", "都", "而", "但", "或", "所", "以", "因", "为", "到", "于", "从",
    "把", "被", "让", "使", "有", "没", "不", "很", "非", "常", "一", "个",
    "a", "the", "is", "in", "on", "at", "to", "of", "and", "or",
}


def _tokenize(text: str) -> list[str]:
    """
    分词 v0.2：词组 + 中文字符 bigram（相邻双字组合）
    bigram 提升同字前缀/后缀的召回，如"工资" vs "工作"可区分；
    语义层面的近义词（收入 vs 薪资）由查询扩展（_expand_query）处理。
    """
    lower = text.lower()

    # ① 提取2字以上词组
    words = re.findall(r'[一-鿿]{2,}|[a-zA-Z]{3,}', lower)
    words = [w for w in words if w not in STOPWORDS]

    # ② 中文字符 bigram（相邻两个汉字）
    chinese_chars = re.findall(r'[一-鿿]', lower)
    bigrams = [
        chinese_chars[i] + chinese_chars[i + 1]
        for i in range(len(chinese_chars) - 1)
        if (chinese_chars[i] + chinese_chars[i + 1]) not in STOPWORDS
    ]

    return words + bigrams


def _extract_keywords(text: str, top_n: int = 30) -> dict:
    """提取关键词及其 TF 权重（含 bigram，top_n 提升至 30）"""
    words = _tokenize(text)
    if not words:
        return {}
    counts = Counter(words)
    total = len(words)
    return {w: round(c / total, 4) for w, c in counts.most_common(top_n)}
