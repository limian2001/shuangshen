from __future__ import annotations
"""
Chroma 向量库客户端 v0.6

架构设计：
  - 单一 collection "memories" 存所有替身的 episode/opinion 记忆向量
  - 通过 metadata.avatar_id 过滤，不为每个替身单独建 collection
  - 不使用 Chroma 内置 embedding（由 DeepSeek API 统一计算后传入）
  - SQLite 继续保留全部记忆元数据，Chroma 只存向量索引

wechat / style 类型不写入 Chroma（同之前逻辑，仅供风格档案生成用）。
"""
import os
from typing import Optional, List

from backend.core.config import config

_client = None
_collection = None


def _get_client():
    global _client
    if _client is None:
        import chromadb
        from chromadb.config import Settings
        _client = chromadb.PersistentClient(
            path=str(config.CHROMA_PATH),
            settings=Settings(anonymized_telemetry=False),
        )
    return _client


def get_collection():
    """获取（或创建）memories collection，使用余弦距离"""
    global _collection
    if _collection is None:
        client = _get_client()
        _collection = client.get_or_create_collection(
            name="memories",
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


# ─────────────────────────────────────────────
# 写入
# ─────────────────────────────────────────────

def upsert_memories(items: List[dict]) -> int:
    """
    批量写入/更新向量记忆。

    items 每条格式：
      {
        "id":        str,           # 与 SQLite memories.id 一致
        "content":   str,           # 记忆文本
        "embedding": List[float],   # DeepSeek 计算的向量
        "avatar_id": str,
        "mem_type":  str,           # episode | opinion
        "priority":  int,           # 0 | 1
        "topic_tags": str,          # JSON 字符串，如 '["事实"]'
      }

    返回成功写入条数。
    """
    if not items:
        return 0

    col = get_collection()
    col.upsert(
        ids=[item["id"] for item in items],
        documents=[item["content"] for item in items],
        embeddings=[item["embedding"] for item in items],
        metadatas=[{
            "avatar_id":  item["avatar_id"],
            "mem_type":   item["mem_type"],
            "priority":   int(item.get("priority", 0)),
            "topic_tags": item.get("topic_tags", "[]"),
        } for item in items],
    )
    return len(items)


def delete_memory(memory_id: str) -> None:
    """删除单条向量记忆（记忆被用户手动删除时调用）"""
    try:
        col = get_collection()
        col.delete(ids=[memory_id])
    except Exception as e:
        print(f"[CHROMA] delete_memory 失败（忽略）: {e}")


def delete_avatar_memories(avatar_id: str) -> int:
    """删除某替身的全部向量记忆（替身删除时调用）"""
    try:
        col = get_collection()
        existing = col.get(where={"avatar_id": {"$eq": avatar_id}})
        if existing and existing["ids"]:
            col.delete(ids=existing["ids"])
            return len(existing["ids"])
    except Exception as e:
        print(f"[CHROMA] delete_avatar_memories 失败（忽略）: {e}")
    return 0


# ─────────────────────────────────────────────
# 检索
# ─────────────────────────────────────────────

def query_memories(
    avatar_id: str,
    query_embedding: List[float],
    top_k: int = 6,
) -> List[dict]:
    """
    向量检索：在指定替身的记忆里找最相关的 top_k 条。

    返回列表，每条：
      {"id", "content", "mem_type", "priority", "topic_tags", "score"}

    score 已应用 priority / opinion 加权（与之前 _vector_search 逻辑一致）。
    """
    col = get_collection()

    # 先确认该替身在 Chroma 里有多少条记忆，避免 n_results > 实际数量时报错
    try:
        count_result = col.get(
            where={"avatar_id": {"$eq": avatar_id}},
            include=[],   # 只要 count，不拉数据
        )
        available = len(count_result["ids"]) if count_result and count_result["ids"] else 0
    except Exception:
        available = 0

    if available == 0:
        return []

    n = min(top_k, available)

    try:
        results = col.query(
            query_embeddings=[query_embedding],
            n_results=n,
            where={"avatar_id": {"$eq": avatar_id}},
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        print(f"[CHROMA] query 失败: {e}")
        return []

    if not results or not results.get("ids") or not results["ids"][0]:
        return []

    ids       = results["ids"][0]
    documents = results["documents"][0]
    metadatas = results["metadatas"][0]
    distances = results["distances"][0]

    items = []
    for i, mid in enumerate(ids):
        meta = metadatas[i]
        # Chroma cosine distance: 0=完全一致, 2=完全相反 → 转成相似度分数
        score = 1.0 - distances[i]

        # 与原 _vector_search 保持一致的加权逻辑
        if int(meta.get("priority", 0)) == 1:
            score *= 1.5
        if meta.get("mem_type") == "opinion":
            score *= 1.3

        items.append({
            "id":         mid,
            "content":    documents[i],
            "mem_type":   meta.get("mem_type", "episode"),
            "priority":   int(meta.get("priority", 0)),
            "topic_tags": meta.get("topic_tags", "[]"),
            "score":      score,
        })

    items.sort(key=lambda x: x["score"], reverse=True)
    return items
