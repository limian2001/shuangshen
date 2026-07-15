from __future__ import annotations
"""
替身路由：创建 / 查询 / 共享 / 绑定
"""
import json
import secrets
from flask import Blueprint, request, jsonify, g

from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.utils.auth import require_auth, new_id


# ─── 称呼字段辅助：兼容旧单字符串和新 JSON 数组 ───────────────────────────
def _parse_address(val) -> list[str]:
    """DB 值 → list，兼容旧字符串和新 JSON 数组"""
    if not val:
        return []
    if isinstance(val, list):
        return [v.strip() for v in val if str(v).strip()]
    s = str(val).strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
            return [str(v).strip() for v in arr if str(v).strip()]
        except Exception:
            pass
    return [s] if s else []


def _serialize_address(val) -> str:
    """前端传入值 → JSON 字符串存 DB"""
    items = _parse_address(val)
    return json.dumps(items[:5], ensure_ascii=False)


def _address_api(val) -> list[str]:
    """DB 值 → API 返回 list"""
    return _parse_address(val)


def _normalize_avatar(d: dict) -> dict:
    """把 avatar dict 里的称呼字段从 DB 格式转成 API list 格式"""
    d["address_to_other"] = _address_api(d.get("address_to_other"))
    d["address_from_other"] = _address_api(d.get("address_from_other"))
    return d

avatars_bp = Blueprint("avatars", __name__, url_prefix="/api/avatars")

VALID_RELATIONSHIPS = {
    "son", "daughter", "father", "mother",
    "boyfriend", "girlfriend",
    "elder_brother", "younger_brother",
    "elder_sister", "younger_sister",
    "friend", "bestie", "custom",
}


@avatars_bp.get("")
@require_auth
def list_avatars():
    """列出我创建的所有替身"""
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT id, name, relationship, counterpart_role, address_to_other, address_from_other,
                      custom_rel_name, status, share_code, created_at, updated_at
               FROM avatars WHERE creator_id = ? AND status != 'deleted'
               ORDER BY created_at DESC""",
            (g.user_id,),
        ).fetchall())
    return jsonify(rows)


@avatars_bp.post("")
@require_auth
def create_avatar():
    """创建替身"""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    relationship = data.get("relationship", "custom")
    identity_desc = (data.get("identity_desc") or "").strip()
    counterpart_role = (data.get("counterpart_role") or "").strip()
    address_to_other = _serialize_address(data.get("address_to_other"))
    address_from_other = _serialize_address(data.get("address_from_other"))
    custom_rel_name = (data.get("custom_rel_name") or "").strip()

    if not name:
        return jsonify({"error": "name 不能为空"}), 400
    if relationship not in VALID_RELATIONSHIPS:
        return jsonify({"error": f"relationship 须为 {VALID_RELATIONSHIPS} 之一"}), 400

    aid = new_id()
    share_code = secrets.token_urlsafe(8)

    with get_db() as conn:
        conn.execute(
            """INSERT INTO avatars
               (id, creator_id, name, relationship, identity_desc, share_code,
                counterpart_role, address_to_other, address_from_other, custom_rel_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (aid, g.user_id, name, relationship, identity_desc, share_code,
             counterpart_role, address_to_other, address_from_other, custom_rel_name),
        )

    return jsonify({
        "id": aid,
        "name": name,
        "relationship": relationship,
        "counterpart_role": counterpart_role,
        "address_to_other": _parse_address(address_to_other),
        "address_from_other": _parse_address(address_from_other),
        "custom_rel_name": custom_rel_name,
        "identity_desc": identity_desc,
        "share_code": share_code,
        "status": "active",
    }), 201


@avatars_bp.get("/<avatar_id>")
@require_auth
def get_avatar(avatar_id):
    """获取替身详情（创建者或绑定的接收者可访问）"""
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT * FROM avatars WHERE id = ?", (avatar_id,)
        ).fetchone())

    if not avatar:
        return jsonify({"error": "替身不存在"}), 404

    # 权限：创建者 or 接收者
    if not _can_access(avatar_id, g.user_id, avatar):
        return jsonify({"error": "无权访问"}), 403

    # 不暴露 persona_prompt 给接收者
    if avatar["creator_id"] != g.user_id:
        avatar.pop("persona_prompt", None)
        avatar.pop("share_code", None)

    return jsonify(_normalize_avatar(avatar))


@avatars_bp.put("/<avatar_id>")
@require_auth
def update_avatar(avatar_id):
    """更新替身（仅创建者）"""
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT * FROM avatars WHERE id = ? AND creator_id = ?",
            (avatar_id, g.user_id),
        ).fetchone())

    if not avatar:
        return jsonify({"error": "替身不存在或无权限"}), 404

    data = request.get_json() or {}
    fields = []
    values = []
    for field in ("name", "status", "identity_desc", "relationship",
                  "counterpart_role", "address_to_other", "address_from_other", "custom_rel_name"):
        if field in data:
            if field == "relationship" and data[field] not in VALID_RELATIONSHIPS:
                return jsonify({"error": f"relationship 无效"}), 400
            val = data[field]
            if field in ("address_to_other", "address_from_other"):
                val = _serialize_address(val)
            fields.append(f"{field} = ?")
            values.append(val)

    if not fields:
        return jsonify({"error": "没有可更新的字段"}), 400

    fields.append("updated_at = datetime('now')")
    values.append(avatar_id)

    with get_db() as conn:
        conn.execute(
            f"UPDATE avatars SET {', '.join(fields)} WHERE id = ?", values
        )

    return jsonify({"ok": True})


@avatars_bp.delete("/<avatar_id>")
@require_auth
def delete_avatar(avatar_id):
    """硬删除替身及其全部数据（仅创建者）"""
    # 验证权限
    with get_db() as conn:
        avatar = conn.execute(
            "SELECT id FROM avatars WHERE id=? AND creator_id=?",
            (avatar_id, g.user_id),
        ).fetchone()
    if not avatar:
        return jsonify({"error": "替身不存在或无权限"}), 404

    # 清理 Chroma 向量（不影响主流程）
    try:
        from backend.services.chroma_store import delete_avatar_memories
        delete_avatar_memories(avatar_id)
    except Exception as e:
        print(f"[DELETE] Chroma 清理失败（继续删除）: {e}")

    # 硬删除所有关联数据 + 替身本体
    with get_db() as conn:
        conn.execute("DELETE FROM memories        WHERE avatar_id=?", (avatar_id,))
        conn.execute("DELETE FROM chat_messages   WHERE avatar_id=?", (avatar_id,))
        conn.execute("DELETE FROM raw_uploads     WHERE avatar_id=?", (avatar_id,))
        conn.execute("DELETE FROM ingest_jobs     WHERE avatar_id=?", (avatar_id,))
        conn.execute("DELETE FROM sensitive_topics WHERE avatar_id=?", (avatar_id,))
        conn.execute("DELETE FROM avatar_receivers WHERE avatar_id=?", (avatar_id,))
        conn.execute("DELETE FROM avatars          WHERE id=?",        (avatar_id,))

    print(f"[DELETE] avatar={avatar_id[:8]} 及全部数据已彻底删除")
    return jsonify({"ok": True})


@avatars_bp.post("/<avatar_id>/share")
@require_auth
def get_share_code(avatar_id):
    """获取/重置共享码"""
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT share_code FROM avatars WHERE id = ? AND creator_id = ?",
            (avatar_id, g.user_id),
        ).fetchone())

    if not avatar:
        return jsonify({"error": "替身不存在或无权限"}), 404

    return jsonify({
        "share_code": avatar["share_code"],
        "share_url": f"shuangshen://join/{avatar['share_code']}",
    })


@avatars_bp.post("/join/<share_code>")
@require_auth
def join_avatar(share_code):
    """接收者通过共享码绑定替身"""
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT * FROM avatars WHERE share_code = ? AND status = 'active'",
            (share_code,),
        ).fetchone())

    if not avatar:
        return jsonify({"error": "无效的共享码"}), 404

    if avatar["creator_id"] == g.user_id:
        return jsonify({"error": "不能绑定自己创建的替身"}), 400

    bind_id = new_id()
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO avatar_receivers (id, avatar_id, receiver_id) VALUES (?, ?, ?)",
                (bind_id, avatar["id"], g.user_id),
            )
    except Exception as e:
        if "UNIQUE" in str(e):
            return jsonify({"message": "已绑定", "avatar_id": avatar["id"]}), 200
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "message": "绑定成功",
        "avatar_id": avatar["id"],
        "avatar_name": avatar["name"],
    }), 201


@avatars_bp.get("/received")
@require_auth
def received_avatars():
    """接收者：查看绑定的替身列表"""
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT a.id, a.name, a.relationship, a.status,
                      a.voice_model_id, a.voice_language, ar.bound_at
               FROM avatars a
               JOIN avatar_receivers ar ON a.id = ar.avatar_id
               WHERE ar.receiver_id = ? AND a.status = 'active'""",
            (g.user_id,),
        ).fetchall())
    return jsonify(rows)


@avatars_bp.delete("/received/<avatar_id>")
@require_auth
def leave_avatar(avatar_id):
    """接收者解绑替身"""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM avatar_receivers WHERE avatar_id = ? AND receiver_id = ?",
            (avatar_id, g.user_id),
        )
    return jsonify({"ok": True})


def _can_access(avatar_id: str, user_id: str, avatar: dict) -> bool:
    """检查用户是否有权访问某替身（创建者 or 接收者）"""
    if avatar["creator_id"] == user_id:
        return True
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM avatar_receivers WHERE avatar_id = ? AND receiver_id = ?",
            (avatar_id, user_id),
        ).fetchone()
    return row is not None
