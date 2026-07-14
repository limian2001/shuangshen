from __future__ import annotations
"""
人工接管路由

  GET  /api/takeover/<avatar_id>/unlock-status/<receiver_id>  — 查询接管解锁状态
  GET  /api/takeover/<avatar_id>/recent/<receiver_id>         — 获取最近 N 条对话
  POST /api/takeover/<avatar_id>/start/<receiver_id>          — 创建者开始接管
  POST /api/takeover/<session_id>/end                         — 创建者主动结束
  POST /api/takeover/<session_id>/message                     — 创建者发送消息
  GET  /api/takeover/<avatar_id>/receiver-status              — 接收者轮询（新消息）
  GET  /api/takeover/<session_id>/poll                        — 创建者轮询接收者新消息
  GET  /api/takeover/my-sessions                              — 创建者查看所有活跃 session

永久解锁逻辑：
  - FREE_FEATURES=True 时永远不扣币
  - 对于同一 creator+avatar+receiver 组合，只要曾经创建过任意 session，后续免费重入
  - 第一次创建时（FREE_FEATURES=False）才扣币
"""
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth, new_id
from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.services.coins import spend_coins
from backend.core.config import config

takeover_bp = Blueprint("takeover", __name__, url_prefix="/api/takeover")

TAKEOVER_COST      = 20  # 首次接管费用（言己币），FREE_FEATURES 时不生效
INACTIVITY_MINUTES = 5   # 接收者无响应超过 5 分钟自动结束


# ─── 工具函数 ────────────────────────────────────────────────────────────────

def _get_active_session(avatar_id: str, receiver_id: str):
    with get_db() as conn:
        return row_to_dict(conn.execute(
            "SELECT * FROM takeover_sessions WHERE avatar_id=? AND receiver_id=? AND status='active'",
            (avatar_id, receiver_id),
        ).fetchone())


def _is_unlocked(avatar_id: str, creator_id: str, receiver_id: str) -> bool:
    """曾经有过接管记录（任意状态） = 已解锁，可免费重入"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM takeover_sessions WHERE avatar_id=? AND creator_id=? AND receiver_id=?",
            (avatar_id, creator_id, receiver_id),
        ).fetchone()
    return row is not None


def _maybe_auto_end(session: dict) -> bool:
    """如果接收者超过 5 分钟无新消息，自动结束接管。返回 True 表示已结束。"""
    if not session or session.get("status") != "active":
        return False
    last = session.get("last_receiver_msg_at") or session["started_at"]
    cutoff = (datetime.utcnow() - timedelta(minutes=INACTIVITY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    if last < cutoff:
        with get_db() as conn:
            conn.execute(
                "UPDATE takeover_sessions SET status='ended', ended_at=datetime('now') WHERE id=?",
                (session["id"],),
            )
        return True
    return False


def _assert_creator(avatar_id: str, user_id: str):
    with get_db() as conn:
        avatar = conn.execute(
            "SELECT creator_id FROM avatars WHERE id=? AND status='active'",
            (avatar_id,),
        ).fetchone()
    if not avatar:
        raise ValueError("替身不存在")
    if avatar["creator_id"] != user_id:
        raise PermissionError("无权限")


# ─── 路由 ────────────────────────────────────────────────────────────────────

@takeover_bp.get("/<avatar_id>/unlock-status/<receiver_id>")
@require_auth
def takeover_unlock_status(avatar_id, receiver_id):
    """查询某对话的接管解锁状态（是否曾经解锁 / 是否有活跃 session）"""
    try:
        _assert_creator(avatar_id, g.user_id)
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 403

    active = _get_active_session(avatar_id, receiver_id)
    unlocked = _is_unlocked(avatar_id, g.user_id, receiver_id)

    with get_db() as conn:
        coins_row = conn.execute("SELECT coins FROM users WHERE id=?", (g.user_id,)).fetchone()

    return jsonify({
        "unlocked": unlocked or config.FREE_FEATURES,
        "active_session_id": active["id"] if active else None,
        "cost": TAKEOVER_COST,
        "coins": (coins_row["coins"] or 0) if coins_row else 0,
        "free_mode": config.FREE_FEATURES,
    })


@takeover_bp.get("/<avatar_id>/recent/<receiver_id>")
@require_auth
def recent_messages(avatar_id, receiver_id):
    """返回最近 N 条对话记录（创建者查看上下文用）"""
    try:
        _assert_creator(avatar_id, g.user_id)
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 403

    limit = min(int(request.args.get("limit", 5)), 20)
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT role, content, created_at FROM chat_messages
               WHERE avatar_id=? AND receiver_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (avatar_id, receiver_id, limit),
        ).fetchall())
    rows.reverse()  # 转为时间正序
    return jsonify({"messages": rows})


@takeover_bp.post("/<avatar_id>/start/<receiver_id>")
@require_auth
def start_takeover(avatar_id, receiver_id):
    """
    创建者开始人工接管。
    - 已有活跃 session → 直接返回 session_id
    - 已曾经接管过此对话（任意 session 记录） → 永久解锁，免费重入
    - 全新对话 + FREE_FEATURES=False → 扣 TAKEOVER_COST 言己币
    """
    try:
        _assert_creator(avatar_id, g.user_id)
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 403

    with get_db() as conn:
        bound = conn.execute(
            "SELECT 1 FROM avatar_receivers WHERE avatar_id=? AND receiver_id=?",
            (avatar_id, receiver_id),
        ).fetchone()
    if not bound:
        return jsonify({"error": "该用户尚未绑定此替身"}), 400

    # 已有活跃 session → 直接返回
    existing = _get_active_session(avatar_id, receiver_id)
    if existing:
        with get_db() as conn:
            recent = rows_to_list(conn.execute(
                """SELECT role, content, created_at FROM chat_messages
                   WHERE avatar_id=? AND receiver_id=?
                   ORDER BY created_at DESC LIMIT 5""",
                (avatar_id, receiver_id),
            ).fetchall())
        recent.reverse()
        return jsonify({
            "session_id": existing["id"],
            "already_active": True,
            "recent_messages": recent,
        })

    # 判断是否需要扣币
    already_unlocked = _is_unlocked(avatar_id, g.user_id, receiver_id)
    if not already_unlocked and not config.FREE_FEATURES:
        ok, balance = spend_coins(g.user_id, TAKEOVER_COST, "takeover", avatar_id)
        if not ok:
            return jsonify({"error": f"言己币不足，需要 {TAKEOVER_COST} 币，当前余额 {balance}"}), 402

    session_id = new_id()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO takeover_sessions (id, avatar_id, creator_id, receiver_id, status)
               VALUES (?, ?, ?, ?, 'active')""",
            (session_id, avatar_id, g.user_id, receiver_id),
        )
        # 同步拿最近 5 条消息
        recent = rows_to_list(conn.execute(
            """SELECT role, content, created_at FROM chat_messages
               WHERE avatar_id=? AND receiver_id=?
               ORDER BY created_at DESC LIMIT 5""",
            (avatar_id, receiver_id),
        ).fetchall())
    recent.reverse()

    return jsonify({
        "session_id": session_id,
        "already_active": False,
        "recent_messages": recent,
        "free": already_unlocked or config.FREE_FEATURES,
    })


@takeover_bp.post("/<session_id>/end")
@require_auth
def end_takeover(session_id):
    """创建者主动结束接管"""
    with get_db() as conn:
        session = row_to_dict(conn.execute(
            "SELECT id FROM takeover_sessions WHERE id=? AND creator_id=?",
            (session_id, g.user_id),
        ).fetchone())
    if not session:
        return jsonify({"error": "Session 不存在或无权限"}), 404

    with get_db() as conn:
        conn.execute(
            "UPDATE takeover_sessions SET status='ended', ended_at=datetime('now') WHERE id=?",
            (session_id,),
        )
    return jsonify({"ok": True})


@takeover_bp.post("/<session_id>/message")
@require_auth
def creator_send_message(session_id):
    """创建者在接管中发送消息（存为 assistant 角色，接收者可见）"""
    with get_db() as conn:
        session = row_to_dict(conn.execute(
            "SELECT * FROM takeover_sessions WHERE id=? AND creator_id=? AND status='active'",
            (session_id, g.user_id),
        ).fetchone())
    if not session:
        return jsonify({"error": "接管 Session 不存在或已结束"}), 404

    data = request.get_json() or {}
    content = (data.get("message") or "").strip()
    if not content:
        return jsonify({"error": "消息不能为空"}), 400

    msg_id = new_id()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO chat_messages (id, avatar_id, receiver_id, role, content)
               VALUES (?, ?, ?, 'assistant', ?)""",
            (msg_id, session["avatar_id"], session["receiver_id"], content),
        )

    return jsonify({"ok": True, "message_id": msg_id})


@takeover_bp.get("/<avatar_id>/receiver-status")
@require_auth
def receiver_check_takeover(avatar_id):
    """
    接收者轮询：当前是否有人工接管？
    Query: since=<timestamp>  — 只拉取该时间之后的新消息
    """
    session = _get_active_session(avatar_id, g.user_id)
    if not session or _maybe_auto_end(session):
        return jsonify({"active": False})

    since = request.args.get("since", session["started_at"])
    with get_db() as conn:
        new_msgs = rows_to_list(conn.execute(
            """SELECT id, role, content, created_at FROM chat_messages
               WHERE avatar_id=? AND receiver_id=? AND role='assistant' AND created_at > ?
               ORDER BY created_at ASC LIMIT 20""",
            (avatar_id, g.user_id, since),
        ).fetchall())

    return jsonify({
        "active": True,
        "session_id": session["id"],
        "new_messages": new_msgs,
    })


@takeover_bp.get("/<session_id>/poll")
@require_auth
def creator_poll(session_id):
    """创建者轮询：拉取接收者新消息（含双方所有新消息）"""
    with get_db() as conn:
        session = row_to_dict(conn.execute(
            "SELECT * FROM takeover_sessions WHERE id=? AND creator_id=?",
            (session_id, g.user_id),
        ).fetchone())
    if not session:
        return jsonify({"error": "Session 不存在"}), 404

    ended = _maybe_auto_end(session)
    since = request.args.get("since", session["started_at"])

    with get_db() as conn:
        msgs = rows_to_list(conn.execute(
            """SELECT id, role, content, created_at FROM chat_messages
               WHERE avatar_id=? AND receiver_id=? AND created_at > ?
               ORDER BY created_at ASC LIMIT 50""",
            (session["avatar_id"], session["receiver_id"], since),
        ).fetchall())

        if not ended:
            user_msgs = [m for m in msgs if m["role"] == "user"]
            if user_msgs:
                conn.execute(
                    "UPDATE takeover_sessions SET last_receiver_msg_at=? WHERE id=?",
                    (user_msgs[-1]["created_at"], session["id"]),
                )

    return jsonify({
        "session_id": session_id,
        "status": "ended" if ended else session["status"],
        "messages": msgs,
    })


@takeover_bp.get("/my-sessions")
@require_auth
def my_sessions():
    """创建者：查看所有活跃接管 session"""
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT ts.id, ts.avatar_id, ts.receiver_id, ts.status,
                      ts.started_at, ts.last_receiver_msg_at,
                      a.name AS avatar_name,
                      u.display_name AS receiver_name
               FROM takeover_sessions ts
               JOIN avatars a ON a.id = ts.avatar_id
               JOIN users u ON u.id = ts.receiver_id
               WHERE ts.creator_id = ? AND ts.status = 'active'
               ORDER BY ts.started_at DESC""",
            (g.user_id,),
        ).fetchall())

    for s in rows:
        _maybe_auto_end(s)

    return jsonify(rows)
