from __future__ import annotations
"""
言己币路由：余额查询 / 流水 / 管理员补发
"""
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth
from backend.services.coins import get_balance, award_coins
from backend.db.database import get_db, rows_to_list

coins_bp = Blueprint("coins", __name__, url_prefix="/api/coins")


@coins_bp.get("")
@require_auth
def balance():
    """返回当前余额"""
    return jsonify({"coins": get_balance(g.user_id)})


@coins_bp.get("/history")
@require_auth
def history():
    """返回言己币流水（最近100条）"""
    limit = min(int(request.args.get("limit", 20)), 100)
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT amount, reason, ref_id, balance_after, created_at
               FROM coin_transactions WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (g.user_id, limit),
        ).fetchall())
    return jsonify(rows)


@coins_bp.post("/admin/award")
@require_auth
def admin_award():
    """
    管理员给全部用户（或指定用户）补发言己币。
    Body: {"amount": 10, "reason": "launch_gift", "user_id": "可选"}
    """
    with get_db() as conn:
        user = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (g.user_id,)
        ).fetchone()
    if not user or not user["is_admin"]:
        return jsonify({"error": "无权限"}), 403

    data = request.get_json() or {}
    amount = int(data.get("amount", 10))
    reason = data.get("reason", "admin_award")
    target = data.get("user_id")

    with get_db() as conn:
        if target:
            user_ids = [target]
        else:
            rows = conn.execute("SELECT id FROM users").fetchall()
            user_ids = [r["id"] for r in rows]

    count = 0
    for uid in user_ids:
        try:
            award_coins(uid, amount, reason)
            count += 1
        except Exception:
            pass

    return jsonify({"awarded": count, "amount": amount})


@coins_bp.get("/admin/history/<user_id>")
@require_auth
def admin_user_history(user_id):
    """管理员查看指定用户的言己币余额和流水"""
    with get_db() as conn:
        admin = conn.execute(
            "SELECT is_admin FROM users WHERE id=?", (g.user_id,)
        ).fetchone()
    if not admin or not admin["is_admin"]:
        return jsonify({"error": "无权限"}), 403

    limit = min(int(request.args.get("limit", 50)), 200)
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT amount, reason, ref_id, balance_after, created_at
               FROM coin_transactions WHERE user_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall())
        bal_row = conn.execute(
            "SELECT coins FROM users WHERE id=?", (user_id,)
        ).fetchone()

    return jsonify({
        "balance": bal_row["coins"] if bal_row else 0,
        "transactions": rows,
    })


@coins_bp.post("/share/record")
@require_auth
def record_share():
    """
    分享小程序得 10 币，每自然日（北京时间 UTC+8）最多 3 次。
    前端在用户点击"分享"按钮时调用，服务端做速率限制。
    """
    with get_db() as conn:
        today_count = conn.execute(
            """SELECT COUNT(*) FROM coin_transactions
               WHERE user_id = ? AND reason = 'share_reward'
               AND DATE(created_at, '+8 hours') = DATE('now', '+8 hours')""",
            (g.user_id,),
        ).fetchone()[0]

    if today_count >= 3:
        return jsonify({
            "awarded": False,
            "limit_reached": True,
            "today_count": today_count,
            "message": "今日分享奖励已达上限（每日 3 次）",
        })

    balance = award_coins(g.user_id, 10, "share_reward")
    new_count = today_count + 1
    return jsonify({
        "awarded": True,
        "coins": 10,
        "balance": balance,
        "today_count": new_count,
        "remaining": 3 - new_count,
        "message": f"获得 10 言己币（今日第 {new_count}/3 次）",
    })


@coins_bp.get("/share/status")
@require_auth
def share_status():
    """返回今日分享奖励状态（已领次数 / 剩余次数）"""
    with get_db() as conn:
        today_count = conn.execute(
            """SELECT COUNT(*) FROM coin_transactions
               WHERE user_id = ? AND reason = 'share_reward'
               AND DATE(created_at, '+8 hours') = DATE('now', '+8 hours')""",
            (g.user_id,),
        ).fetchone()[0]
    return jsonify({
        "today_count": today_count,
        "remaining": max(0, 3 - today_count),
        "limit": 3,
    })
