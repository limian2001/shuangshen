"""
记忆/观点路由
"""
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth
from backend.services.memory_store import add_memory, list_memories
from backend.api.routes.avatars import _can_access
from backend.db.database import get_db, row_to_dict

memories_bp = Blueprint("memories", __name__, url_prefix="/api/memories")


@memories_bp.post("/<avatar_id>")
@require_auth
def add(avatar_id):
    """添加记忆/观点条目"""
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT * FROM avatars WHERE id = ? AND creator_id = ?",
            (avatar_id, g.user_id),
        ).fetchone())
    if not avatar:
        return jsonify({"error": "替身不存在或无权限（仅创建者可添加记忆）"}), 403

    data = request.get_json() or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content 不能为空"}), 400

    mem_type = data.get("mem_type", "opinion")
    if mem_type not in ("episode", "opinion", "style"):
        return jsonify({"error": "mem_type 须为 episode | opinion | style"}), 400

    topic_tags = data.get("topic_tags", [])
    # 用户手动添加的记忆 priority=1，RAG 检索时权重 ×1.5
    result = add_memory(avatar_id, content, mem_type, topic_tags, priority=1)
    return jsonify(result), 201


@memories_bp.get("/<avatar_id>")
@require_auth
def get_list(avatar_id):
    """获取记忆列表（创建者可见）"""
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT * FROM avatars WHERE id = ?", (avatar_id,)
        ).fetchone())
    if not avatar or avatar["creator_id"] != g.user_id:
        return jsonify({"error": "无权限"}), 403

    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    result = list_memories(avatar_id, page, per_page)
    return jsonify(result)
