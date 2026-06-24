#!/usr/bin/env python3
"""
数据重置脚本 — 清空替身数据，方便重新测试

用法：
  python3 scripts/reset_data.py                   # 交互式选择替身
  python3 scripts/reset_data.py --all             # 重置所有替身的所有数据
  python3 scripts/reset_data.py --avatar <id>     # 重置指定替身
  python3 scripts/reset_data.py --topics          # 只清空全局话题预设
  python3 scripts/reset_data.py --all --topics    # 全部数据 + 全局话题

可选 --keep-avatar  保留替身记录本身（只清空数据，不删除替身）
"""
import sys
import os
import argparse
import sqlite3

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.config import config

DB_PATH = str(config.DB_PATH)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=OFF")  # 允许跨表删除
    return conn


def list_avatars(conn):
    rows = conn.execute(
        "SELECT id, name, relationship, created_at FROM avatars ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def reset_avatar_data(conn, avatar_id: str, keep_avatar: bool = True):
    """清空指定替身的所有数据（记忆、对话、导入记录、人格 Prompt）"""
    counts = {}

    cur = conn.execute("DELETE FROM memories WHERE avatar_id = ?", (avatar_id,))
    counts["memories"] = cur.rowcount

    cur = conn.execute("DELETE FROM chat_messages WHERE avatar_id = ?", (avatar_id,))
    counts["chat_messages"] = cur.rowcount

    cur = conn.execute("DELETE FROM ingest_jobs WHERE avatar_id = ?", (avatar_id,))
    counts["ingest_jobs"] = cur.rowcount

    # 清空 persona_prompt（重置为空，保留替身记录）
    conn.execute(
        "UPDATE avatars SET persona_prompt = '' WHERE id = ?", (avatar_id,)
    )
    counts["persona_prompt"] = "已清空"

    # 替身专属话题（虽已废弃，顺手清掉）
    cur = conn.execute(
        "DELETE FROM sensitive_topics WHERE avatar_id = ?", (avatar_id,)
    )
    counts["avatar_topics"] = cur.rowcount

    if not keep_avatar:
        conn.execute(
            "DELETE FROM avatar_receivers WHERE avatar_id = ?", (avatar_id,)
        )
        conn.execute("DELETE FROM avatars WHERE id = ?", (avatar_id,))
        counts["avatar_deleted"] = True

    return counts


def reset_global_topics(conn):
    """清空全局话题预设（avatar_id IS NULL）"""
    cur = conn.execute("DELETE FROM sensitive_topics WHERE avatar_id IS NULL")
    return cur.rowcount


def reset_query_cache(conn):
    """清空 LLM 查询扩展缓存"""
    cur = conn.execute("DELETE FROM query_expansions")
    return cur.rowcount


def print_summary(name, counts):
    print(f"\n  替身「{name}」重置完成：")
    for k, v in counts.items():
        print(f"    {k}: {v}")


def interactive_select(avatars):
    print("\n可用的替身：")
    for i, a in enumerate(avatars):
        print(f"  [{i+1}] {a['name']}  ({a['relationship']})  id={a['id'][:8]}...")
    print("  [0] 退出")
    try:
        choice = int(input("\n选择要重置的替身编号（输入序号）：").strip())
    except (ValueError, EOFError):
        print("输入无效，退出。")
        sys.exit(0)
    if choice == 0:
        sys.exit(0)
    if choice < 1 or choice > len(avatars):
        print("编号超出范围，退出。")
        sys.exit(1)
    return avatars[choice - 1]


def main():
    parser = argparse.ArgumentParser(description="替身数据重置工具")
    parser.add_argument("--all", action="store_true", help="重置所有替身的数据")
    parser.add_argument("--avatar", type=str, help="指定替身 ID（前缀即可）")
    parser.add_argument("--topics", action="store_true", help="同时清空全局话题预设")
    parser.add_argument("--cache", action="store_true", help="同时清空查询扩展缓存")
    parser.add_argument("--keep-avatar", action="store_true", default=True,
                        help="保留替身记录，只清空数据（默认 True）")
    parser.add_argument("--delete-avatar", action="store_true",
                        help="同时删除替身记录本身（慎用）")
    args = parser.parse_args()

    keep = not args.delete_avatar

    conn = get_conn()
    avatars = list_avatars(conn)

    if not avatars:
        print("数据库中没有替身，无需重置。")
        # 仍可清全局话题
    else:
        print(f"\n数据库路径: {DB_PATH}")
        print(f"共找到 {len(avatars)} 个替身")

    # ── 确定操作对象 ──
    targets = []
    if args.all:
        targets = avatars
        print("\n⚠️  将重置 所有 替身的数据！")
        confirm = input("确认？输入 yes 继续：").strip()
        if confirm.lower() != "yes":
            print("已取消。")
            sys.exit(0)
    elif args.avatar:
        matched = [a for a in avatars if a["id"].startswith(args.avatar)]
        if not matched:
            print(f"未找到 ID 前缀为 {args.avatar} 的替身。")
            sys.exit(1)
        targets = matched
    elif avatars:
        selected = interactive_select(avatars)
        confirm = input(f"\n确认重置替身「{selected['name']}」的所有数据？(yes/no) ").strip()
        if confirm.lower() != "yes":
            print("已取消。")
            sys.exit(0)
        targets = [selected]

    # ── 执行重置 ──
    try:
        for a in targets:
            counts = reset_avatar_data(conn, a["id"], keep_avatar=keep)
            print_summary(a["name"], counts)

        if args.topics:
            n = reset_global_topics(conn)
            print(f"\n  全局话题预设：已删除 {n} 条")

        if args.cache:
            n = reset_query_cache(conn)
            print(f"\n  查询扩展缓存：已清空 {n} 条")

        conn.commit()
        print("\n✅ 重置完成！重启后端服务即可使用新人设。")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ 发生错误，已回滚：{e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
