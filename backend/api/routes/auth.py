from __future__ import annotations
"""
认证路由：注册 / 登录
"""
import hashlib
import secrets
from flask import Blueprint, request, jsonify

from backend.db.database import get_db, row_to_dict
from backend.utils.auth import generate_token, new_id

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def _hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """SHA-256 + salt 哈希（生产建议换 bcrypt）"""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return h, salt


def _verify_password(password: str, stored_hash: str) -> bool:
    salt, hashed = stored_hash.split(":", 1)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return h == hashed


@auth_bp.post("/register")
def register():
    data = request.get_json() or {}
    display_name = (data.get("display_name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password", "")

    if not display_name:
        return jsonify({"error": "display_name 不能为空"}), 400
    if not password or len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    if not phone and not email:
        return jsonify({"error": "手机号或邮箱至少填一个"}), 400

    h, salt = _hash_password(password)
    pwd_hash = f"{salt}:{h}"
    uid = new_id()

    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO users (id, phone, email, password_hash, display_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (uid, phone or None, email or None, pwd_hash, display_name),
            )
    except Exception as e:
        if "UNIQUE" in str(e):
            return jsonify({"error": "手机号或邮箱已注册"}), 409
        return jsonify({"error": str(e)}), 500

    token = generate_token(uid)
    return jsonify({"token": token, "user_id": uid, "display_name": display_name}), 201


@auth_bp.post("/login")
def login():
    data = request.get_json() or {}
    identifier = (data.get("phone") or data.get("email") or "").strip()
    password = data.get("password", "")

    if not identifier or not password:
        return jsonify({"error": "账号和密码不能为空"}), 400

    with get_db() as conn:
        user = row_to_dict(conn.execute(
            "SELECT * FROM users WHERE phone = ? OR email = ?",
            (identifier, identifier),
        ).fetchone())

    if not user or not _verify_password(password, user["password_hash"]):
        return jsonify({"error": "账号或密码错误"}), 401

    token = generate_token(user["id"])
    return jsonify({
        "token": token,
        "user_id": user["id"],
        "display_name": user["display_name"],
    })


@auth_bp.get("/me")
def me():
    from backend.utils.auth import require_auth
    from flask import g
    # 手动验证
    from backend.utils.auth import decode_token
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "未认证"}), 401
    try:
        payload = decode_token(auth[7:])
        uid = payload["sub"]
    except Exception:
        return jsonify({"error": "无效 Token"}), 401

    with get_db() as conn:
        user = row_to_dict(conn.execute(
            "SELECT id, phone, email, display_name, role, created_at FROM users WHERE id = ?",
            (uid,),
        ).fetchone())
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    return jsonify(user)
