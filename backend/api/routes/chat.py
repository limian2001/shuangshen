from __future__ import annotations
"""
对话路由 v0.3 — 替身核心对话接口

新流程：
1. 安全硬拦截（转账/借钱等，直接返回）
2. 取近期对话历史
3. 获取敏感话题提示（不再硬拦，注入 prompt）
4. 构建上下文（RAG 记忆 + 对话历史 + 话题提示 + 重复检测）
5. 调用 LLM 生成回复
6. 存储对话历史
"""
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth, new_id
from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.services.topic_guard import is_safety_blocked, get_topic_hints
from backend.services.persona_builder import get_chat_context
from backend.services.llm_provider import llm
from backend.core.config import config

chat_bp = Blueprint("chat", __name__, url_prefix="/api/chat")

MAX_HISTORY = 12  # LLM 上下文最多带入轮数


@chat_bp.post("/<avatar_id>")
@require_auth
def send_message(avatar_id):
    """
    与替身对话。

    Body: {"message": "你好"}
    Returns: {"reply": "...", "safety_blocked": bool}
    """
    if not _receiver_can_chat(avatar_id, g.user_id):
        return jsonify({"error": "你还没有绑定这个替身，请先获取共享码并绑定"}), 403

    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "消息不能为空"}), 400

    # ① 安全硬拦截（仅限金融欺诈类，必须零漏报）
    safety = is_safety_blocked(message)
    if safety.blocked:
        _save_message(avatar_id, g.user_id, "user", message)
        _save_message(avatar_id, g.user_id, "assistant", safety.reply)
        print(f"[SAFETY_BLOCK] avatar={avatar_id[:8]} msg={repr(message[:30])}")
        return jsonify({
            "reply": safety.reply,
            "safety_blocked": True,
        })

    # ② 取近期对话历史（用于上下文感知 + 重复话题检测）
    history = _get_recent_history(avatar_id, g.user_id, limit=MAX_HISTORY)

    # ③ 获取敏感话题提示（命中则生成提示字符串，注入 prompt）
    topic_hints = get_topic_hints(avatar_id, message)
    if topic_hints:
        print(f"[TOPIC_HINT] avatar={avatar_id[:8]} hint_len={len(topic_hints)}")

    # ④ 构建对话上下文（RAG + 上下文感知 + 话题提示）
    try:
        system_prompt, retrieved_memories = get_chat_context(
            avatar_id=avatar_id,
            user_query=message,
            recent_history=history,
            topic_hints=topic_hints,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    # ⑤ 调用 LLM（所有消息都经过 LLM，保证情绪感知和语境连贯）
    messages = history + [{"role": "user", "content": message}]
    print(f"[LLM] avatar={avatar_id[:8]} history={len(history)} memories={len(retrieved_memories)}")
    try:
        reply = llm.chat(
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=600,
            temperature=0.75,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": f"LLM 调用失败: {str(e)}"}), 500

    # ⑥ 存储对话历史
    _save_message(avatar_id, g.user_id, "user", message)
    _save_message(avatar_id, g.user_id, "assistant", reply)

    return jsonify({
        "reply": reply,
        "safety_blocked": False,
        "retrieved_memory_count": len(retrieved_memories),
    })


@chat_bp.get("/<avatar_id>/history")
@require_auth
def get_history(avatar_id):
    """获取对话历史"""
    if not _receiver_can_chat(avatar_id, g.user_id):
        return jsonify({"error": "无权限"}), 403

    default_limit = config.CHAT_DISPLAY_LIMIT if config.CHAT_DISPLAY_LIMIT > 0 else 50
    limit = int(request.args.get("limit", default_limit))
    # 前端可传 limit=0 表示不限，但受服务器配置约束
    if config.CHAT_DISPLAY_LIMIT > 0 and limit > config.CHAT_DISPLAY_LIMIT:
        limit = config.CHAT_DISPLAY_LIMIT
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT id, role, content, created_at FROM chat_messages
               WHERE avatar_id = ? AND receiver_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (avatar_id, g.user_id, limit),
        ).fetchall())
    rows.reverse()
    return jsonify(rows)


# ─────────────────────────────────────────────
# 内部辅助
# ─────────────────────────────────────────────

def _receiver_can_chat(avatar_id: str, user_id: str) -> bool:
    with get_db() as conn:
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
