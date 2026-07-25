#!/usr/bin/env python3
"""
火山音色槽位查看 / 清理

用法（容器内）：
  docker cp scripts/volc_slots.py shuangshen:/app/volc_slots.py
  docker exec shuangshen python3 /app/volc_slots.py          # 查看占用情况
  docker exec shuangshen python3 /app/volc_slots.py --clean  # 清空全部占用（DB 层）

说明：
- 火山 S_ 槽位是可重复训练的容器，清空只需解除 DB 里的占用记录，
  下次复刻会自动分配并覆盖训练，不会浪费已购买的槽位。
- 清理只动声音绑定字段，不会删除替身、记忆、聊天等任何数据。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.config import config
from backend.db.database import get_db, rows_to_list

CLEAN = "--clean" in sys.argv
pool = [s.strip() for s in config.VOLC_SPEAKER_IDS.split(",") if s.strip()]

print(f"已购槽位（VOLC_SPEAKER_IDS）: {len(pool)} 个\n")

with get_db() as conn:
    uv = rows_to_list(conn.execute(
        """SELECT v.id, v.name, v.voice_id, v.status, u.display_name AS owner
           FROM user_voices v LEFT JOIN users u ON v.user_id = u.id
           WHERE v.provider='volc' AND v.voice_id != ''""").fetchall())
    av = rows_to_list(conn.execute(
        """SELECT id, name, voice_model_id FROM avatars
           WHERE voice_model_id LIKE 'S_%' AND status != 'deleted'""").fetchall())

print("── 当前占用 ──")
if uv:
    print("【我的声音】(新版账号级，删除声音即自动释放)")
    for r in uv:
        print(f"  {r['voice_id']}  ← {r['name']}（{r['owner']}）status={r['status']}")
if av:
    print("【替身级旧数据】(历史遗留，需本脚本清理才释放) ⚠️")
    for r in av:
        print(f"  {r['voice_model_id']}  ← 替身「{r['name']}」")
if not uv and not av:
    print("  （无占用）")

used = {r["voice_id"] for r in uv} | {r["voice_model_id"] for r in av}
free = [s for s in pool if s not in used]
print(f"\n占用 {len(used)} / {len(pool)}，空闲 {len(free)} 个：{free or '无'}")

if CLEAN:
    print("\n── 执行清理 ──")
    with get_db() as conn:
        n_av = conn.execute(
            "UPDATE avatars SET voice_model_id='' WHERE voice_model_id LIKE 'S_%'").rowcount
        n_uv = conn.execute(
            "DELETE FROM user_voices WHERE provider='volc'").rowcount
        conn.execute(
            """UPDATE avatars SET user_voice_id=NULL WHERE user_voice_id NOT IN
               (SELECT id FROM user_voices) AND user_voice_id IS NOT NULL""")
    print(f"✅ 清空替身级旧绑定 {n_av} 条")
    print(f"✅ 删除火山声音记录 {n_uv} 条")
    print(f"✅ 全部 {len(pool)} 个槽位已释放，可重新录入")
    print("   （替身/记忆/聊天数据均未受影响）")
else:
    if used:
        print("\n如需全部释放，重跑本脚本并加 --clean 参数")
