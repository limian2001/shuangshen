from __future__ import annotations
"""
记忆存取服务 v0.2

开发环境：SQLite + 关键词权重检索（bigram 增强版）
生产环境：替换为向量数据库（Pinecone / Weaviate）

接口保持不变，只需替换 search_memories() 的实现。
"""
import json
import re
from collections import Counter
from math import log

from backend.db.database import get_db, rows_to_list
from backend.utils.auth import new_id


# ─────────────────────────────────────────────
# 写入
# ─────────────────────────────────────────────

def add_memory(
    avatar_id: str,
    content: str,
    mem_type: str = "opinion",
    topic_tags: list[str] = None,
    source: str = "manual",
) -> dict:
    """添加单条记忆"""
    mid = new_id()
    tags = json.dumps(topic_tags or [], ensure_ascii=False)
    keywords = json.dumps(_extract_keywords(content), ensure_ascii=False)

    with get_db() as conn:
        conn.execute(
            """INSERT INTO memories (id, avatar_id, content, mem_type, topic_tags, source, keywords)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (mid, avatar_id, content, mem_type, tags, source, keywords),
        )
    return {"id": mid, "content": content, "mem_type": mem_type}


def add_memories_batch(avatar_id: str, items: list[dict]) -> int:
    """批量添加记忆（用于微信导入）"""
    count = 0
    with get_db() as conn:
        for item in items:
            content = item.get("content", "").strip()
            if not content:
                continue
            mid = new_id()
            tags = json.dumps(item.get("topic_tags", []), ensure_ascii=False)
            kw = json.dumps(_extract_keywords(content), ensure_ascii=False)
            conn.execute(
                """INSERT INTO memories (id, avatar_id, content, mem_type, topic_tags, source, keywords)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (mid, avatar_id, content,
                 item.get("mem_type", "wechat"),
                 tags, item.get("source", "wechat_import"), kw),
            )
            count += 1
    return count


# ─────────────────────────────────────────────
# 检索
# ─────────────────────────────────────────────

# 进程内一级缓存（减少 DB 读次数）
_query_expand_cache: dict[str, list[str]] = {}


def search_memories(avatar_id: str, query: str, top_k: int = 8) -> list[dict]:
    """
    检索最相关的记忆片段 v0.2。

    流程：
    1. 对查询做 LLM 同义词扩展（缓存避免重复调用）
    2. 用原始词 + 扩展词 + bigram 对记忆库打分
    3. 返回 top_k 最相关结果

    生产版：替换为向量相似度检索
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, content, mem_type, topic_tags, keywords, created_at
               FROM memories WHERE avatar_id = ?
               ORDER BY created_at DESC LIMIT 500""",
            (avatar_id,),
        ).fetchall()

    if not rows:
        return []

    # 原始分词
    base_words = set(_tokenize(query))

    # LLM 查询扩展（同义词）
    expanded_words = set(_expand_query(query))

    # 合并：原始词权重 1.0，扩展词权重 0.7（扩展词置信度略低）
    query_weights: dict[str, float] = {}
    for w in base_words:
        query_weights[w] = 1.0
    for w in expanded_words:
        if w not in query_weights:
            query_weights[w] = 0.7

    scored = []
    for row in rows:
        mem_kw = json.loads(row["keywords"] or "{}")
        # 加权交集打分
        score = sum(mem_kw.get(w, 0) * weight for w, weight in query_weights.items())
        # opinion 类型额外加权
        if row["mem_type"] == "opinion":
            score *= 1.3
        if score > 0:
            scored.append((score, dict(row)))

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


def get_recent_memories(avatar_id: str, limit: int = 20) -> list[dict]:
    """获取最新的记忆（用于构建 Prompt 时兜底）"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, content, mem_type, topic_tags, created_at
               FROM memories WHERE avatar_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (avatar_id, limit),
        ).fetchall()
    return rows_to_list(rows)


def list_memories(avatar_id: str, page: int = 1, per_page: int = 20) -> dict:
    offset = (page - 1) * per_page
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE avatar_id = ?", (avatar_id,)
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT id, content, mem_type, topic_tags, source, created_at
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
