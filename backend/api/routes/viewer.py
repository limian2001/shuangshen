from __future__ import annotations
"""
查看器路由
  Feature 1: 查看绑定用户列表及统计（10 币终身解锁每替身）
  Feature 2: 查看聊天内容（10 币/20 条，最新在前）
"""
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth, new_id
from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.services.coins import spend_coins

viewer_bp = Blueprint("viewer", __name__, url_prefix="/api/viewer")

VIEWER_STATS_COST = 10   # 查看绑定用户列表：10 币终身解锁
CHAT_BLOCK_COST   = 10   # 查看聊天记录：每 20 条 10 币
CHAT_BLOCK_SIZE   = 20


def _assert_creator(avatar_id: str, user_id: str):
    """验证是替身创建者，否则抛异常"""
    with get_db() as conn:
        avatar = conn.execute(
            "SELECT creator_id FROM avatars WHERE id = ? AND status != 'deleted'",
            (avatar_id,),
        ).fetchone()
    if not avatar:
        raise ValueError("替身不存在")
    if avatar["creator_id"] != user_id:
        raise PermissionError("无权限")


# ─────────── Feature 1：绑定用户列表 ────────────────────────────────────────

@viewer_bp.get("/<avatar_id>/unlock-status")
@require_auth
def unlock_status(avatar_id):
    """Feature 1 是否已解锁，及解锁费用"""
    try:
        _assert_creator(avatar_id, g.user_id)
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 403

    with get_db() as conn:
        unlocked = conn.execute(
            "SELECT 1 FROM avatar_feature_unlocks WHERE user_id=? AND avatar_id=? AND feature='viewer_stats'",
            (g.user_id, avatar_id),
        ).fetchone()
        coins_row = conn.execute("SELECT coins FROM users WHERE id=?", (g.user_id,)).fetchone()

    return jsonify({
        "unlocked": bool(unlocked),
        "cost": VIEWER_STATS_COST,
        "coins": (coins_row["coins"] or 0) if coins_row else 0,
    })


@viewer_bp.post("/<avatar_id>/unlock")
@require_auth
def unlock_viewer_stats(avatar_id):
    """消费 10 币，终身解锁查看绑定用户统计"""
    try:
        _assert_creator(avatar_id, g.user_id)
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 403

    with get_db() as conn:
        already = conn.execute(
            "SELECT 1 FROM avatar_feature_unlocks WHERE user_id=? AND avatar_id=? AND feature='viewer_stats'",
            (g.user_id, avatar_id),
        ).fetchone()
    if already:
        return jsonify({"ok": True, "already_unlocked": True})

    ok, balance = spend_coins(g.user_id, VIEWER_STATS_COST, "feature_unlock", avatar_id)
    if not ok:
        return jsonify({"error": f"言己币不足，需要 {VIEWER_STATS_COST} 币，当前余额 {balance}"}), 402

    with get_db() as conn:
        conn.execute(
            "INSERT INTO avatar_feature_unlocks (id, user_id, avatar_id, feature) VALUES (?, ?, ?, 'viewer_stats')",
            (new_id(), g.user_id, avatar_id),
        )
    return jsonify({"ok": True, "balance": balance})


@viewer_bp.get("/<avatar_id>/binders")
@require_auth
def get_binders(avatar_id):
    """查看绑定用户列表及统计（需先解锁）"""
    try:
        _assert_creator(avatar_id, g.user_id)
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 403

    with get_db() as conn:
        unlocked = conn.execute(
            "SELECT 1 FROM avatar_feature_unlocks WHERE user_id=? AND avatar_id=? AND feature='viewer_stats'",
            (g.user_id, avatar_id),
        ).fetchone()
    if not unlocked:
        return jsonify({"error": "尚未解锁，请先解锁查看功能"}), 403

    cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT
                u.id AS receiver_id,
                u.display_name,
                u.phone,
                ar.bound_at,
                COUNT(cm.id) AS total_messages,
                MIN(cm.created_at) AS first_msg_at,
                MAX(cm.created_at) AS last_msg_at,
                SUM(CASE WHEN cm.created_at >= ? AND cm.role='user' THEN 1 ELSE 0 END) AS msgs_24h
               FROM avatar_receivers ar
               JOIN users u ON u.id = ar.receiver_id
               LEFT JOIN chat_messages cm
                 ON cm.avatar_id = ? AND cm.receiver_id = ar.receiver_id
               WHERE ar.avatar_id = ?
               GROUP BY u.id, u.display_name, u.phone, ar.bound_at
               ORDER BY last_msg_at DESC""",
            (cutoff_24h, avatar_id, avatar_id),
        ).fetchall())

    for r in rows:
        phone = r.get("phone") or ""
        r["phone_tail"] = phone[-4:] if len(phone) >= 4 else "****"
        r.pop("phone", None)

    return jsonify(rows)


# ─────────── Feature 2：聊天内容查看 ────────────────────────────────────────

@viewer_bp.get("/<avatar_id>/chat/<receiver_id>/unlock-status")
@require_auth
def chat_unlock_status(avatar_id, receiver_id):
    """返回已解锁消息数和下一次解锁费用"""
    try:
        _assert_creator(avatar_id, g.user_id)
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 403

    with get_db() as conn:
        unlock = row_to_dict(conn.execute(
            "SELECT messages_unlocked FROM chat_content_unlocks WHERE creator_id=? AND avatar_id=? AND receiver_id=?",
            (g.user_id, avatar_id, receiver_id),
        ).fetchone())
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE avatar_id=? AND receiver_id=?",
            (avatar_id, receiver_id),
        ).fetchone()["n"]
        coins_row = conn.execute("SELECT coins FROM users WHERE id=?", (g.user_id,)).fetchone()

    return jsonify({
        "messages_unlocked": unlock["messages_unlocked"] if unlock else 0,
        "total_messages": total,
        "cost_per_block": CHAT_BLOCK_COST,
        "block_size": CHAT_BLOCK_SIZE,
        "coins": (coins_row["coins"] or 0) if coins_row else 0,
    })


@viewer_bp.post("/<avatar_id>/chat/<receiver_id>/unlock")
@require_auth
def unlock_chat_block(avatar_id, receiver_id):
    """消费 10 币，解锁下 20 条聊天记录"""
    try:
        _assert_creator(avatar_id, g.user_id)
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 403

    ok, balance = spend_coins(
        g.user_id, CHAT_BLOCK_COST, "chat_unlock",
        f"{avatar_id}:{receiver_id}",
    )
    if not ok:
        return jsonify({"error": f"言己币不足，需要 {CHAT_BLOCK_COST} 币，当前余额 {balance}"}), 402

    with get_db() as conn:
        existing = row_to_dict(conn.execute(
            "SELECT id, messages_unlocked FROM chat_content_unlocks WHERE creator_id=? AND avatar_id=? AND receiver_id=?",
            (g.user_id, avatar_id, receiver_id),
        ).fetchone())
        if existing:
            new_count = existing["messages_unlocked"] + CHAT_BLOCK_SIZE
            conn.execute(
                "UPDATE chat_content_unlocks SET messages_unlocked=?, updated_at=datetime('now') WHERE id=?",
                (new_count, existing["id"]),
            )
        else:
            new_count = CHAT_BLOCK_SIZE
            conn.execute(
                """INSERT INTO chat_content_unlocks
                   (id, creator_id, avatar_id, receiver_id, messages_unlocked)
                   VALUES (?, ?, ?, ?, ?)""",
                (new_id(), g.user_id, avatar_id, receiver_id, new_count),
            )

    return jsonify({"ok": True, "messages_unlocked": new_count, "balance": balance})


@viewer_bp.get("/<avatar_id>/chat/<receiver_id>")
@require_auth
def get_chat_content(avatar_id, receiver_id):
    """获取已解锁的聊天记录（最新在前，offset 分页）"""
    try:
        _assert_creator(avatar_id, g.user_id)
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 403

    with get_db() as conn:
        unlock = row_to_dict(conn.execute(
            "SELECT messages_unlocked FROM chat_content_unlocks WHERE creator_id=? AND avatar_id=? AND receiver_id=?",
            (g.user_id, avatar_id, receiver_id),
        ).fetchone())

    if not unlock:
        return jsonify({"messages": [], "messages_unlocked": 0})

    offset = int(request.args.get("offset", 0))
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT role, content, created_at FROM chat_messages
               WHERE avatar_id=? AND receiver_id=?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (avatar_id, receiver_id, CHAT_BLOCK_SIZE, offset),
        ).fetchall())

    return jsonify({"messages": rows, "messages_unlocked": unlock["messages_unlocked"]})
