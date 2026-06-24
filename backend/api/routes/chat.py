from __future__ import annotations
"""
对话路由 — 替身核心对话接口

流程：
1. 敏感话题拦截（topic_guard）
2. 构建上下文（persona_builder RAG）
3. 调用 LLM（llm_provider）
4. 存储对话历史
"""
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth, new_id
from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.services.topic_guard import check_message
from backend.services.persona_builder import get_chat_context
from backend.services.llm_provider import llm

chat_bp = Blueprint("chat", __name__, url_prefix="/api/chat")

MAX_HISTORY = 10  # 对话历史最多带入 N 轮


@chat_bp.post("/<avatar_id>")
@require_auth
def send_message(avatar_id):
    """
    与替身对话。

    Body: {"message": "你好"}
    Returns: {"reply": "...", "intercepted": bool, "strategy": "..."}
    """
    # 验证接收者权限
    if not _receiver_can_chat(avatar_id, g.user_id):
        return jsonify({"error": "你还没有绑定这个替身，请先获取共享码并绑定"}), 403

    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "消息不能为空"}), 400

    # ① 敏感话题拦截
    guard = check_message(avatar_id, message)
    print(f"[TOPIC_GUARD] avatar={avatar_id[:8]} msg={repr(message[:30])} blocked={guard.blocked} strategy={guard.strategy}")
    if guard.blocked:
        _save_message(avatar_id, g.user_id, "user", message)
        _save_message(avatar_id, g.user_id, "assistant", guard.reply)
        return jsonify({
            "reply": guard.reply,
            "intercepted": True,
            "strategy": guard.strategy,
        })

    # ② 构建对话上下文
    try:
        system_prompt, retrieved_memories = get_chat_context(avatar_id, message)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    # ③ 取近期对话历史
    history = _get_recent_history(avatar_id, g.user_id, limit=MAX_HISTORY)
    # 加入本条消息
    messages = history + [{"role": "user", "content": message}]

    # ④ 调用 LLM
    print(f"[SYSTEM_PROMPT]\n{system_prompt[:500]}\n[/SYSTEM_PROMPT]")
    print(f"[MEMORIES_USED] {len(retrieved_memories)} 条")
    try:
        reply = llm.chat(
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=600,
            temperature=0.75,
        )
    except RuntimeError as e:
        # LLM 未配置时给友好提示
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": f"LLM 调用失败: {str(e)}"}), 500

    # ⑤ 存储对话历史
    _save_message(avatar_id, g.user_id, "user", message)
    _save_message(avatar_id, g.user_id, "assistant", reply)

    return jsonify({
        "reply": reply,
        "intercepted": False,
        "retrieved_memory_count": len(retrieved_memories),
    })


@chat_bp.get("/<avatar_id>/history")
@require_auth
def get_history(avatar_id):
    """获取对话历史"""
    if not _receiver_can_chat(avatar_id, g.user_id):
        return jsonify({"error": "无权限"}), 403

    limit = int(request.args.get("limit", 30))
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT id, role, content, created_at FROM chat_messages
               WHERE avatar_id = ? AND receiver_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (avatar_id, g.user_id, limit),
        ).fetchall())
    rows.reverse()  # 正序返回
    return jsonify(rows)


# ─────────────────────────────────────────────
# 内部辅助
# ─────────────────────────────────────────────

def _receiver_can_chat(avatar_id: str, user_id: str) -> bool:
    """检查用户是否可以与替身对话（绑定了替身，或是创建者自己测试）"""
    with get_db() as conn:
        # 创建者自己测试也允许
        avatar = row_to_dict(conn.execute(
            "SELECT creator_id FROM avatars WHERE id = ? AND status = 'active'",
            (avatar_id,),
        ).fetchone())
        if not avatar:
            return False
        if avatar["creator_id"] == user_id:
            return True
        row = conn.execute(
            "SELECT 1 FROM avatar_receivers WHERE avatar_id = ? AND receiver_id = ?",
            (avatar_id, user_id),
        ).fetchone()
    return row is not None


def _save_message(avatar_id: str, receiver_id: str, role: str, content: str):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO chat_messages (id, avatar_id, receiver_id, role, content)
               VALUES (?, ?, ?, ?, ?)""",
            (new_id(), avatar_id, receiver_id, role, content),
        )


def _get_recent_history(avatar_id: str, receiver_id: str, limit: int) -> list[dict]:
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT role, content FROM chat_messages
               WHERE avatar_id = ? AND receiver_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (avatar_id, receiver_id, limit),
        ).fetchall())
    rows.reverse()
    return [{"role": r["role"], "content": r["content"]} for r in rows]
