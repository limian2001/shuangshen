from __future__ import annotations
"""
敏感话题路由 v0.3

两类话题：
  全局话题（/api/topics）：avatar_id=NULL，适用于所有替身，防诈骗/数据保护
  替身专属（已废弃，接口保留兼容）：不推荐新增，逻辑已合并到全局
"""
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth
from backend.db.database import get_db, row_to_dict
from backend.services.topic_guard import add_topic, list_topics, delete_topic

topics_bp = Blueprint("topics", __name__)


# ─────────────────────────────────────────────
# 全局话题接口（推荐使用）
# ─────────────────────────────────────────────

@topics_bp.get("/api/topics")
@require_auth
def global_list():
    """查看全局敏感话题"""
    return jsonify(list_topics(avatar_id=None))


@topics_bp.post("/api/topics")
@require_auth
def global_add():
    """新增全局敏感话题（适用于所有替身）"""
    data = request.get_json() or {}
    keywords = data.get("trigger_keywords", [])
    strategy = data.get("strategy", "deflect")
    preset = data.get("preset_content", "")
    description = data.get("topic_description", "")

    if not keywords:
        return jsonify({"error": "trigger_keywords 不能为空"}), 400
    if strategy not in ("preset_answer", "deflect", "admit_unknown"):
        return jsonify({"error": "strategy 须为 preset_answer | deflect | admit_unknown"}), 400

    result = add_topic(None, keywords, strategy, preset, description)
    return jsonify(result), 201


@topics_bp.delete("/api/topics/<topic_id>")
@require_auth
def global_remove(topic_id):
    """删除全局敏感话题"""
    ok = delete_topic(topic_id, avatar_id=None)
    return jsonify({"ok": ok})


# ─────────────────────────────────────────────
# 替身专属话题接口（保留兼容旧数据，不推荐新增）
# ─────────────────────────────────────────────

def _is_creator(avatar_id: str, user_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM avatars WHERE id = ? AND creator_id = ?",
            (avatar_id, user_id),
        ).fetchone()
    return row is not None


@topics_bp.post("/api/avatars/<avatar_id>/topics")
@require_auth
def avatar_add(avatar_id):
    """[兼容] 新增替身专属敏感话题（建议改用全局接口）"""
    if not _is_creator(avatar_id, g.user_id):
        return jsonify({"error": "无权限"}), 403

    data = request.get_json() or {}
    keywords = data.get("trigger_keywords", [])
    strategy = data.get("strategy", "deflect")
    preset = data.get("preset_content", "")
    description = data.get("topic_description", "")

    if not keywords:
        return jsonify({"error": "trigger_keywords 不能为空"}), 400
    if strategy not in ("preset_answer", "deflect", "admit_unknown"):
        return jsonify({"error": "strategy 无效"}), 400

    result = add_topic(avatar_id, keywords, strategy, preset, description)
    return jsonify(result), 201


@topics_bp.get("/api/avatars/<avatar_id>/topics")
@require_auth
def avatar_list(avatar_id):
    """[兼容] 查看替身专属敏感话题"""
    if not _is_creator(avatar_id, g.user_id):
        return jsonify({"error": "无权限"}), 403
    return jsonify(list_topics(avatar_id))


@topics_bp.delete("/api/avatars/<avatar_id>/topics/<topic_id>")
@require_auth
def avatar_remove(avatar_id, topic_id):
    """[兼容] 删除替身专属敏感话题"""
    if not _is_creator(avatar_id, g.user_id):
        return jsonify({"error": "无权限"}), 403
    ok = delete_topic(topic_id, avatar_id)
    return jsonify({"ok": ok})
