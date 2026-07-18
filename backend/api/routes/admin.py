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
    from backend.api.routes.avatars import purge_avatar_rows, purge_avatar_files

    with get_db() as conn:
        # 先查该用户的所有 avatar_id
        avatar_ids = [r[0] for r in conn.execute(
            "SELECT id FROM avatars WHERE creator_id=?", (user_id,)
        ).fetchall()]

        for aid in avatar_ids:
            purge_avatar_rows(conn, aid)   # 统一子表清单，避免外键漏删
            conn.execute("DELETE FROM avatars WHERE id=?", (aid,))

        conn.execute("DELETE FROM user_identities WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    for aid in avatar_ids:
        purge_avatar_files(aid)
        try:
            from backend.services.chroma_store import delete_avatar_memories
            delete_avatar_memories(aid)
        except Exception as e:
            print(f"[ADMIN] Chroma 清理失败（继续）: {e}")

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
                      u.display_name AS receiver_name,
                      (SELECT 1 FROM chat_traces t WHERE t.message_id = c.id) AS has_trace
               FROM chat_messages c
               LEFT JOIN users u ON c.receiver_id=u.id
               WHERE c.avatar_id=?
               ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",
            (avatar_id, limit, offset)
        ).fetchall())
        chats.reverse()

    return jsonify({"chats": chats, "total": total, "page": page, "pages": max(1, -(-total // limit))})


# ─────────────────────────────────────────────
# 系统告警（降级/API失败等，由 alert() 写入）
# ─────────────────────────────────────────────

@admin_bp.get("/alerts")
@require_admin
def list_alerts():
    """
    告警列表 + 各标签计数。
    Query: tag（筛选）、limit（默认100）
    """
    tag   = (request.args.get("tag") or "").strip()
    limit = min(500, int(request.args.get("limit", 100)))

    with get_db() as conn:
        if tag:
            rows = rows_to_list(conn.execute(
                """SELECT a.id, a.tag, a.message, a.avatar_id, a.created_at, av.name AS avatar_name
                   FROM system_alerts a LEFT JOIN avatars av ON a.avatar_id = av.id
                   WHERE a.tag = ? ORDER BY a.created_at DESC LIMIT ?""",
                (tag, limit)).fetchall())
        else:
            rows = rows_to_list(conn.execute(
                """SELECT a.id, a.tag, a.message, a.avatar_id, a.created_at, av.name AS avatar_name
                   FROM system_alerts a LEFT JOIN avatars av ON a.avatar_id = av.id
                   ORDER BY a.created_at DESC LIMIT ?""",
                (limit,)).fetchall())
        # 各标签 24h / 7天 计数
        tag_counts = rows_to_list(conn.execute(
            """SELECT tag,
                      SUM(CASE WHEN created_at >= datetime('now','-1 day')  THEN 1 ELSE 0 END) AS cnt_24h,
                      SUM(CASE WHEN created_at >= datetime('now','-7 days') THEN 1 ELSE 0 END) AS cnt_7d
               FROM system_alerts
               WHERE created_at >= datetime('now','-7 days')
               GROUP BY tag ORDER BY cnt_24h DESC""").fetchall())
        total_24h = conn.execute(
            "SELECT COUNT(*) FROM system_alerts WHERE created_at >= datetime('now','-1 day')"
        ).fetchone()[0]

    return jsonify({"alerts": rows, "tag_counts": tag_counts, "total_24h": total_24h})


# ─────────────────────────────────────────────
# 数据概览（存储 / Top10 / RAG 命中率）
# ─────────────────────────────────────────────

def _dir_size(path) -> int:
    """目录总字节数（容错）"""
    import os as _os
    total = 0
    try:
        for root, _dirs, files in _os.walk(str(path)):
            for f in files:
                try:
                    total += _os.path.getsize(_os.path.join(root, f))
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _chroma_avatar_counts() -> dict:
    """每个替身在 Chroma 里的向量条数（一次性拉全部 metadata 统计）"""
    try:
        from backend.services.chroma_store import get_collection
        col = get_collection()
        out: dict = {}
        offset = 0
        while True:
            batch = col.get(include=["metadatas"], limit=5000, offset=offset)
            metas = batch.get("metadatas") or []
            if not metas:
                break
            for m in metas:
                aid = (m or {}).get("avatar_id", "")
                if aid:
                    out[aid] = out.get(aid, 0) + 1
            if len(metas) < 5000:
                break
            offset += 5000
        return out
    except Exception as e:
        print(f"[ADMIN] chroma 统计失败: {e}")
        return {}


@admin_bp.get("/data-summary")
@require_admin
def data_summary():
    """存储占用 + 全局计数 + Top10 替身（含7天RAG命中率）"""
    from backend.core.config import config as _cfg

    storage = {
        "sqlite_bytes":  _cfg.DB_PATH.stat().st_size if _cfg.DB_PATH.exists() else 0,
        "chroma_bytes":  _dir_size(_cfg.CHROMA_PATH),
        "uploads_bytes": _dir_size(_cfg.UPLOAD_FOLDER),
    }

    chroma_counts = _chroma_avatar_counts()

    with get_db() as conn:
        totals = {
            "users":    conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "avatars":  conn.execute("SELECT COUNT(*) FROM avatars WHERE status != 'deleted'").fetchone()[0],
            "memories": conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0],
            "vectors":  sum(chroma_counts.values()),
        }
        top = rows_to_list(conn.execute(
            """SELECT a.id, a.name, u.display_name AS creator,
                      COUNT(m.id) AS mem_count,
                      (SELECT COUNT(*) FROM chat_messages c WHERE c.avatar_id = a.id) AS msg_count,
                      (SELECT MAX(c.created_at) FROM chat_messages c WHERE c.avatar_id = a.id) AS last_active
               FROM avatars a
               LEFT JOIN users u ON a.creator_id = u.id
               LEFT JOIN memories m ON m.avatar_id = a.id
               WHERE a.status != 'deleted'
               GROUP BY a.id ORDER BY mem_count DESC LIMIT 10""").fetchall())
        # 7天 RAG 计数合并
        rag = {r["avatar_id"]: r for r in rows_to_list(conn.execute(
            """SELECT avatar_id,
                      SUM(vector_hits) AS v, SUM(keyword_fallbacks) AS k, SUM(empty_results) AS e
               FROM rag_counters WHERE date >= date('now','-7 days')
               GROUP BY avatar_id""").fetchall())}

    for row in top:
        row["vector_count"] = chroma_counts.get(row["id"], 0)
        r = rag.get(row["id"], {})
        v, k, e = r.get("v", 0) or 0, r.get("k", 0) or 0, r.get("e", 0) or 0
        total_q = v + k + e
        row["rag_total_7d"] = total_q
        row["rag_hit_rate"] = round(v / total_q * 100, 1) if total_q else None

    return jsonify({"storage": storage, "totals": totals, "top_avatars": top})


@admin_bp.get("/avatars/<avatar_id>/data-detail")
@require_admin
def avatar_data_detail(avatar_id):
    """单替身数据钻取：记忆分布 / 向量一致性 / 人设完整度 / 命中率趋势"""
    chroma_counts = _chroma_avatar_counts()
    vector_count = chroma_counts.get(avatar_id, 0)

    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            """SELECT id, name, relationship, status, created_at,
                      persona_prompt, style_profile, conversation_samples,
                      voice_model_id, avg_reply_chars
               FROM avatars WHERE id = ?""", (avatar_id,)).fetchone())
        if not avatar:
            return jsonify({"error": "替身不存在"}), 404

        mem_by_type = rows_to_list(conn.execute(
            """SELECT mem_type, COUNT(*) AS cnt FROM memories
               WHERE avatar_id = ? GROUP BY mem_type ORDER BY cnt DESC""",
            (avatar_id,)).fetchall())
        # 可向量化记忆数（episode/opinion 等，排除 wechat/style）
        vectorizable = conn.execute(
            """SELECT COUNT(*) FROM memories
               WHERE avatar_id = ? AND mem_type NOT IN ('wechat','style')""",
            (avatar_id,)).fetchone()[0]
        topics = conn.execute(
            "SELECT COUNT(*) FROM sensitive_topics WHERE avatar_id = ?", (avatar_id,)
        ).fetchone()[0]
        receivers = conn.execute(
            "SELECT COUNT(*) FROM avatar_receivers WHERE avatar_id = ?", (avatar_id,)
        ).fetchone()[0]
        messages = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE avatar_id = ?", (avatar_id,)
        ).fetchone()[0]
        rag_daily = rows_to_list(conn.execute(
            """SELECT date, vector_hits, keyword_fallbacks, empty_results
               FROM rag_counters WHERE avatar_id = ? AND date >= date('now','-7 days')
               ORDER BY date""", (avatar_id,)).fetchall())
        recent_alerts = rows_to_list(conn.execute(
            """SELECT tag, message, created_at FROM system_alerts
               WHERE avatar_id = ? ORDER BY created_at DESC LIMIT 10""",
            (avatar_id,)).fetchall())

    import json as _json
    try:
        samples_cnt = len(_json.loads(avatar.get("conversation_samples") or "[]"))
    except Exception:
        samples_cnt = 0

    v = sum(r["vector_hits"] for r in rag_daily)
    k = sum(r["keyword_fallbacks"] for r in rag_daily)
    e = sum(r["empty_results"] for r in rag_daily)
    total_q = v + k + e

    return jsonify({
        "avatar": {
            "id": avatar["id"], "name": avatar["name"],
            "relationship": avatar["relationship"], "status": avatar["status"],
            "created_at": avatar["created_at"],
        },
        "memories": {
            "by_type": mem_by_type,
            "vectorizable": vectorizable,
            "vector_count": vector_count,
            # 向量缺口 >0 说明有记忆没建向量（需要补建）
            "vector_gap": max(0, vectorizable - vector_count),
        },
        "persona": {
            "persona_prompt_chars":  len(avatar.get("persona_prompt") or ""),
            "style_profile_chars":   len(avatar.get("style_profile") or ""),
            "conversation_samples":  samples_cnt,
            "topics":                topics,
            "avg_reply_chars":       avatar.get("avg_reply_chars") or 0,
            "has_voice":             bool(avatar.get("voice_model_id")),
        },
        "usage": {"receivers": receivers, "messages": messages},
        "rag": {
            "daily": rag_daily,
            "hit_rate_7d": round(v / total_q * 100, 1) if total_q else None,
            "vector_hits": v, "keyword_fallbacks": k, "empty_results": e,
        },
        "recent_alerts": recent_alerts,
    })


# ─────────────────────────────────────────────
# 对话链路 Trace（话题检测/记忆检索/Prompt/LLM 全过程）
# ─────────────────────────────────────────────

@admin_bp.get("/traces/<message_id>")
@require_admin
def get_trace(message_id):
    """按用户消息 ID 查询该条对话的完整决策链"""
    import json as _json
    with get_db() as conn:
        row = row_to_dict(conn.execute(
            """SELECT t.trace, t.created_at, c.content AS user_message,
                      av.name AS avatar_name
               FROM chat_traces t
               JOIN chat_messages c ON t.message_id = c.id
               LEFT JOIN avatars av ON t.avatar_id = av.id
               WHERE t.message_id = ?""",
            (message_id,)).fetchone())
    if not row:
        return jsonify({"error": "该消息无 Trace 记录（可能已过保留期或 Trace 未开启）"}), 404
    try:
        trace = _json.loads(row["trace"])
    except Exception:
        trace = {}
    return jsonify({
        "user_message": row["user_message"],
        "avatar_name":  row["avatar_name"],
        "created_at":   row["created_at"],
        "trace":        trace,
    })
