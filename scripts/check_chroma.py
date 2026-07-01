"""
快速检查 Chroma 向量库内容
用法：python scripts/check_chroma.py [avatar_id]
"""
import sys
import os

# 确保能找到 backend 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.config import config
import chromadb
from chromadb.config import Settings

client = chromadb.PersistentClient(
    path=str(config.CHROMA_PATH),
    settings=Settings(anonymized_telemetry=False),
)

col = client.get_or_create_collection("memories")
total = col.count()
print(f"\n=== Chroma 向量库 ===")
print(f"Collection: memories")
print(f"总向量数: {total}")

if total == 0:
    print("（空库，还没有导入记忆）")
    sys.exit(0)

# 按 avatar_id 过滤（可选）
avatar_id = sys.argv[1] if len(sys.argv) > 1 else None

if avatar_id:
    result = col.get(where={"avatar_id": {"$eq": avatar_id}}, include=["documents", "metadatas"])
    items = list(zip(result["ids"], result["documents"], result["metadatas"]))
    print(f"替身 {avatar_id} 的记忆数: {len(items)}")
else:
    # 拉前 20 条看看
    result = col.get(limit=20, include=["documents", "metadatas"])
    items = list(zip(result["ids"], result["documents"], result["metadatas"]))
    print(f"（显示前 {len(items)} 条，指定 avatar_id 可过滤）\n")

print()
for mid, doc, meta in items[:20]:
    print(f"ID:      {mid}")
    print(f"类型:    {meta.get('mem_type')} | 优先级: {meta.get('priority')} | 替身: {meta.get('avatar_id','')[:8]}...")
    print(f"内容:    {doc[:80]}{'...' if len(doc)>80 else ''}")
    print()
