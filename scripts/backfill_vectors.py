#!/usr/bin/env python3
"""
历史记忆向量补建（一次性）
之前 embedding 服务不可用，已导入的记忆没有向量。
本脚本遍历所有替身，调用 sync_to_chroma 重建向量索引。
用法（容器内）:
  docker cp scripts/backfill_vectors.py shuangshen:/app/backfill_vectors.py
  docker exec shuangshen python3 /app/backfill_vectors.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.database import get_db, rows_to_list
from backend.services.memory_store import sync_to_chroma

with get_db() as conn:
    avatars = rows_to_list(conn.execute(
        "SELECT id, name FROM avatars WHERE status != 'deleted'"
    ).fetchall())

print(f"共 {len(avatars)} 个替身待补建向量\n")
total_ok = total_fail = 0
for a in avatars:
    r = sync_to_chroma(a["id"])
    ok, fail = r.get("synced", 0), r.get("failed", 0)
    total_ok += ok
    total_fail += fail
    print(f"  {a['name']:12s} 同步 {ok} 条" + (f"，失败 {fail} 条" if fail else ""))

print(f"\n完成：共同步 {total_ok} 条" + (f"，失败 {total_fail} 条 ⚠️" if total_fail else "，全部成功 ✅"))
