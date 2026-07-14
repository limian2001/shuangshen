from __future__ import annotations
"""
管理后台 API — 仅限 is_admin=1 的用户访问
"""
import hashlib, secrets
from flask import Blueprint, request, jsonify

from backend.utils.auth import require_admin, new_id
from backend.db.database import get_db, row_to_dict, rows_to_list

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


# ─────────────────────────────────────────────
# Dashboard 统计
# ─────────────────────────────────────────────

@admin_bp.get("/stats")
@require_admin
def stats():
    with get_db() as conn:
        user_count    = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        avatar_count  = conn.execute("SELECT COUNT(*) FROM avatars WHERE status != 'deleted'").fetchone()[0]
        msg_count     = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
        memory_count  = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        upload_count  = conn.execute("SELECT COUNT(*) FROM raw_uploads").fetchone()[0]

        recent_users = rows_to_list(conn.execute(
            """SELECT id, display_name, phone, email, status, is_admin, created_at
               FROM users ORDER BY created_at DESC LIMIT 10"""
        ).fetchall())

        daily_msgs = rows_to_list(conn.execute(
            """SELECT DATE(created_at) as day, COUNT(*) as cnt
               FROM chat_messages
               WHERE created_at >= datetime('now', '-7 days')
               GROUP BY day ORDER BY day"""
        ).fetchall())

    return jsonify({
        "user_count":   user_count,
        "avatar_count": avatar_count,
        "msg_count":    msg_count,
        "memory_count": memory_count,
        "upload_count": upload_count,
        "recent_users": recent_users,
        "daily_msgs":   daily_msgs,
    })


# ─────────────────────────────────────────────
# 用户管理
# ─────────────────────────────────────────────

@admin_bp.get("/users")
@require_admin
def list_users():
    q     = (request.args.get("q") or "").strip()
    page  = max(1, int(request.args.get("page", 1)))
    limit = min(100, int(request.args.get("limit", 20)))
    offset = (page - 1) * limit

    with get_db() as conn:
        if q:
            like = f"%{q}%"
            total = conn.execute(
                "SELECT COUNT(*) FROM users WHERE display_name LIKE ? OR phone LIKE ? OR email LIKE ?",
                (like, like, like)
            ).fetchone()[0]
            users = rows_to_list(conn.execute(
                """SELECT u.id, u.display_name, u.phone, u.email, u.status, u.is_admin,
                          u.coins, u.created_at,
                          (SELECT COUNT(*) FROM avatars a WHERE a.creator_id=u.id AND a.status!='deleted') AS avatar_count,
                          (SELECT COUNT(*) FROM chat_messages cm JOIN avatars av ON cm.avatar_id=av.id WHERE av.creator_id=u.id) AS msg_count
                   FROM users u
                   WHERE u.display_name LIKE ? OR u.phone LIKE ? OR u.email LIKE ?
                   ORDER BY u.created_at DESC LIMIT ? OFFSET ?""",
                (like, like, like, limit, offset)
            ).fetchall())
        else:
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            users = rows_to_list(conn.execute(
                """SELECT u.id, u.display_name, u.phone, u.email, u.status, u.is_admin,
                          u.coins, u.created_at,
                          (SELECT COUNT(*) FROM avatars a WHERE a.creator_id=u.id AND a.status!='deleted') AS avatar_count,
                          (SELECT COUNT(*) FROM chat_messages cm JOIN avatars av ON cm.avatar_id=av.id WHERE av.creator_id=u.id) AS msg_count
                   FROM users u ORDER BY u.created_at DESC LIMIT ? OFFSET ?""",
                (limit, offset)
            ).fetchall())

    return jsonify({
        "users": users,
        "total": total,
        "page":  page,
        "pages": max(1, -(-total // limit)),  # ceil div
    })


@admin_bp.get("/users/<user_id>")
@require_admin
def get_user(user_id):
    with get_db() as conn:
        user = row_to_dict(conn.execute(
            "SELECT id, display_name, phone, email, status, is_admin, role, coins, created_at FROM users WHERE id = ?",
            (user_id,)
        ).fetchone())
        if not user:
            return jsonify({"error": "用户不存在"}), 404

        avatars = rows_to_list(conn.execute(
            """SELECT a.id, a.name, a.relationship, a.status, a.created_at,
                      (SELECT COUNT(*) FROM memories m WHERE m.avatar_id=a.id) AS memory_count,
                      (SELECT COUNT(*) FROM raw_uploads r WHERE r.avatar_id=a.id) AS upload_count,
                      (SELECT COUNT(*) FROM chat_messages c WHERE c.avatar_id=a.id) AS msg_count
               FROM avatars a WHERE a.creator_id=? ORDER BY a.created_at DESC""",
            (user_id,)
        ).fetchall())

        # 第三方登录绑定
        identities = rows_to_list(conn.execute(
            "SELECT platform, external_id, created_at FROM user_identities WHERE user_id=?",
            (user_id,)
        ).fetchall())

    return jsonify({"user": user, "avatars": avatars, "identities": identities})


@admin_bp.post("/users/<user_id>/reset_password")
@require_admin
def reset_password(user_id):
    data = request.get_json() or {}
    new_pwd = (data.get("password") or "").strip()
    if len(new_pwd) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400

    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{new_pwd}".encode()).hexdigest()
    pwd_hash = f"{salt}:{h}"

    with get_db() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (pwd_hash, user_id))
    return jsonify({"ok": True})


@admin_bp.post("/users/<user_id>/toggle_suspend")
@require_admin
def toggle_suspend(user_id):
    with get_db() as conn:
        user = row_to_dict(conn.execute(
            "SELECT status FROM users WHERE id=?", (user_id,)
        ).fetchone())
        if not user:
            return jsonify({"error": "用户不存在"}), 404
        new_status = "active" if user["status"] == "suspended" else "suspended"
        conn.execute("UPDATE users SET status=? WHERE id=?", (new_status, user_id))
    return jsonify({"status": new_status})


@admin_bp.delete("/users/<user_id>")
@require_admin
def delete_user(user_id):
    """删除用户及其所有数据（不可逆）"""
    with get_db() as conn:
        # 先查该用户的所有 avatar_id
        avatar_ids = [r[0] for r in conn.execute(
            "SELECT id FROM avatars WHERE creator_id=?", (user_id,)
        ).fetchall()]

        for aid in avatar_ids:
            conn.execute("DELETE FROM memories WHERE avatar_id=?", (aid,))
            conn.execute("DELETE FROM chat_messages WHERE avatar_id=?", (aid,))
            conn.execute("DELETE FROM raw_uploads WHERE avatar_id=?", (aid,))
            conn.execute("DELETE FROM ingest_jobs WHERE avatar_id=?", (aid,))
            conn.execute("DELETE FROM sensitive_topics WHERE avatar_id=?", (aid,))
            conn.execute("DELETE FROM avatar_receivers WHERE avatar_id=?", (aid,))
            conn.execute("DELETE FROM avatars WHERE id=?", (aid,))

        conn.execute("DELETE FROM user_identities WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# 替身管理
# ─────────────────────────────────────────────

@admin_bp.get("/avatars")
@require_admin
def list_avatars():
    page  = max(1, int(request.args.get("page", 1)))
    limit = min(100, int(request.args.get("limit", 20)))
    offset = (page - 1) * limit

    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM avatars WHERE status!='deleted'").fetchone()[0]
        avatars = rows_to_list(conn.execute(
            """SELECT a.id, a.name, a.relationship, a.status, a.created_at,
                      u.display_name AS creator_name, u.phone AS creator_phone,
                      (SELECT COUNT(*) FROM memories m WHERE m.avatar_id=a.id) AS memory_count,
                      (SELECT COUNT(*) FROM chat_messages c WHERE c.avatar_id=a.id) AS msg_count,
                      (SELECT COUNT(*) FROM raw_uploads r WHERE r.avatar_id=a.id) AS upload_count
               FROM avatars a JOIN users u ON a.creator_id=u.id
               WHERE a.status!='deleted'
               ORDER BY a.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall())

    return jsonify({"avatars": avatars, "total": total, "page": page, "pages": max(1, -(-total // limit))})


@admin_bp.get("/avatars/<avatar_id>")
@require_admin
def get_avatar(avatar_id):
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            """SELECT a.*, u.display_name AS creator_name, u.phone AS creator_phone
               FROM avatars a JOIN users u ON a.creator_id=u.id WHERE a.id=?""",
            (avatar_id,)
        ).fetchone())
        if not avatar:
            return jsonify({"error": "替身不存在"}), 404

        # 上传记录（含原始文本摘要）
        uploads = rows_to_list(conn.execute(
            """SELECT id, filename, file_type, char_count, processed_at, created_at,
                      SUBSTR(content, 1, 200) AS content_preview
               FROM raw_uploads WHERE avatar_id=? ORDER BY created_at DESC""",
            (avatar_id,)
        ).fetchall())

        # 导入任务
        jobs = rows_to_list(conn.execute(
            """SELECT id, filename, status, total_msgs, imported, error_msg,
                      date_start, date_end, created_at, finished_at
               FROM ingest_jobs WHERE avatar_id=? ORDER BY created_at DESC""",
            (avatar_id,)
        ).fetchall())

        # 记忆（全量，按类型排列）
        memories = rows_to_list(conn.execute(
            """SELECT id, content, mem_type, topic_tags, source, priority, chat_date, created_at
               FROM memories WHERE avatar_id=? ORDER BY mem_type, created_at DESC""",
            (avatar_id,)
        ).fetchall())

        # 最近200条对话（跨所有接收者）
        chats = rows_to_list(conn.execute(
            """SELECT c.id, c.role, c.content, c.created_at,
                      u.display_name AS receiver_name
               FROM chat_messages c
               LEFT JOIN users u ON c.receiver_id=u.id
               WHERE c.avatar_id=?
               ORDER BY c.created_at DESC LIMIT 200""",
            (avatar_id,)
        ).fetchall())
        chats.reverse()

    return jsonify({
        "avatar":   avatar,
        "uploads":  uploads,
        "jobs":     jobs,
        "memories": memories,
        "chats":    chats,
    })


@admin_bp.get("/avatars/<avatar_id>/chat")
@require_admin
def get_avatar_chat(avatar_id):
    """获取替身的完整对话历史（分页）"""
    page  = max(1, int(request.args.get("page", 1)))
    limit = min(500, int(request.args.get("limit", 100)))
    offset = (page - 1) * limit

    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE avatar_id=?", (avatar_id,)
        ).fetchone()[0]
        chats = rows_to_list(conn.execute(
            """SELECT c.id, c.role, c.content, c.created_at,
                      u.display_name AS receiver_name
               FROM chat_messages c
               LEFT JOIN users u ON c.receiver_id=u.id
               WHERE c.avatar_id=?
               ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",
            (avatar_id, limit, offset)
        ).fetchall())
        chats.reverse()

    return jsonify({"chats": chats, "total": total, "page": page, "pages": max(1, -(-total // limit))})
