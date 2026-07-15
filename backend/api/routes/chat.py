from __future__ import annotations
"""
对话路由 — 可选 TTS 同步返回

Body JSON:
  {
    "message": "你好",
    "voice":   false   # true 时同步合成并返回 audio_base64
  }

Returns:
  {
    "reply":                  "...",
    "retrieved_memory_count": 3,
    "audio_base64":           "...",  # 仅 voice=true 且成功时
    "audio_error":            "..."   # 仅 voice=true 且 TTS 失败时
  }

人工接管中：返回 {"reply": null, "takeover": true}
"""
import base64

from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth, new_id
from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.services.topic_guard import get_topic_hints
from backend.services.persona_builder import get_chat_context
from backend.services.llm_provider import llm
from backend.services.media import tts_synthesize
from backend.core.config import config

chat_bp = Blueprint("chat", __name__, url_prefix="/api/chat")

MAX_HISTORY = 12  # LLM 上下文最多带入轮数


@chat_bp.post("/<avatar_id>")
@require_auth
def send_message(avatar_id):
    """与替身对话。"""
    if not _receiver_can_chat(avatar_id, g.user_id):
        return jsonify({"error": "你还没有绑定这个替身，请先获取共享码并绑定"}), 403

    data       = request.get_json() or {}
    message    = (data.get("message") or "").strip()
    want_voice = bool(data.get("voice", False))

    if not message:
        return jsonify({"error": "消息不能为空"}), 400

    # ── 检查人工接管 ──────────────────────────────────────────────────────────
    with get_db() as _tc:
        active_takeover = _tc.execute(
            "SELECT id FROM takeover_sessions WHERE avatar_id=? AND receiver_id=? AND status='active'",
            (avatar_id, g.user_id),
        ).fetchone()
    if active_takeover:
        _save_message(avatar_id, g.user_id, "user", message)
        with get_db() as _tc:
            _tc.execute(
                "UPDATE takeover_sessions SET last_receiver_msg_at=datetime('now') WHERE id=?",
                (active_takeover["id"],),
            )
        return jsonify({"reply": None, "takeover": True})

    # ① 取近期对话历史
    history = _get_recent_history(avatar_id, g.user_id, limit=MAX_HISTORY)

    # ② 获取敏感话题提示（用原始 message 检索，不含图片描述）
    topic_hints = get_topic_hints(avatar_id, message)
    if topic_hints:
        print(f"[TOPIC_HINT] avatar={avatar_id[:8]} hint_len={len(topic_hints)}")

    # ③ 构建对话上下文
    rag_query = message
    try:
        system_prompt, retrieved_memories = get_chat_context(
            avatar_id=avatar_id,
            user_query=rag_query,
            recent_history=history,
            topic_hints=topic_hints,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    # ④ 构建 LLM 消息
    messages = history + [{"role": "user", "content": message}]
    print(
        f"[LLM] avatar={avatar_id[:8]} history={len(history)} "
        f"memories={len(retrieved_memories)} voice={want_voice}"
    )

    # ④ 调用 LLM
    try:
        reply = llm.chat(
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=2000,
            temperature=0.75,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": f"LLM 调用失败: {str(e)}"}), 500

    # ⑤ 存储对话历史
    _save_message(avatar_id, g.user_id, "user", message)
    _save_message(avatar_id, g.user_id, "assistant", reply)

    # ⑥ 构建响应
    response: dict = {
        "reply":                  reply,
        "retrieved_memory_count": len(retrieved_memories),
    }

    # ⑦ 可选：同步合成 TTS，返回 base64 MP3（失败不影响文字回复）
    if want_voice:
        try:
            with get_db() as conn:
                av = row_to_dict(conn.execute(
                    "SELECT voice_model_id, voice_language FROM avatars WHERE id=?",
                    (avatar_id,),
                ).fetchone())
            voice_id = (av.get("voice_model_id") if av else None) or None
            language = (av.get("voice_language") if av else None) or "zh"
            audio_bytes = tts_synthesize(reply[:500], voice_id=voice_id, language=language)
            response["audio_base64"] = base64.b64encode(audio_bytes).decode()
        except Exception as e:
            print(f"[TTS] 合成失败（不影响文字回复）: {e}")
            response["audio_error"] = str(e)

    return jsonify(response)


@chat_bp.get("/<avatar_id>/history")
@require_auth
def get_history(avatar_id):
    """获取对话历史"""
    if not _receiver_can_chat(avatar_id, g.user_id):
        return jsonify({"error": "无权限"}), 403

    default_limit = config.CHAT_DISPLAY_LIMIT if config.CHAT_DISPLAY_LIMIT > 0 else 50
    limit = int(request.args.get("limit", default_limit))
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
