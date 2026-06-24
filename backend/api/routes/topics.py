from __future__ import annotations
"""
敏感话题预设路由
"""
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth
from backend.db.database import get_db, row_to_dict
from backend.services.topic_guard import add_topic, list_topics, delete_topic

topics_bp = Blueprint("topics", __name__, url_prefix="/api/avatars")


def _is_creator(avatar_id: str, user_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM avatars WHERE id = ? AND creator_id = ?",
            (avatar_id, user_id),
        ).fetchone()
    return row is not None


@topics_bp.post("/<avatar_id>/topics")
@require_auth
def add(avatar_id):
    if not _is_creator(avatar_id, g.user_id):
        return jsonify({"error": "无权限"}), 403

    data = request.get_json() or {}
    keywords = data.get("trigger_keywords", [])
    strategy = data.get("strategy", "deflect")
    preset = data.get("preset_content", "")
    description = data.get("topic_description", "")  # 可选，不传则自动生成

    if not keywords:
        return jsonify({"error": "trigger_keywords 不能为空"}), 400
    if strategy not in ("preset_answer", "deflect", "admit_unknown"):
        return jsonify({"error": "strategy 无效"}), 400

    result = add_topic(avatar_id, keywords, strategy, preset, description)
    return jsonify(result), 201


@topics_bp.get("/<avatar_id>/topics")
@require_auth
def get_list(avatar_id):
    if not _is_creator(avatar_id, g.user_id):
        return jsonify({"error": "无权限"}), 403
    return jsonify(list_topics(avatar_id))


@topics_bp.delete("/<avatar_id>/topics/<topic_id>")
@require_auth
def remove(avatar_id, topic_id):
    if not _is_creator(avatar_id, g.user_id):
        return jsonify({"error": "无权限"}), 403
    ok = delete_topic(topic_id, avatar_id)
    return jsonify({"ok": ok})
