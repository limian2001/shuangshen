from __future__ import annotations
"""
认证路由：注册 / 登录 / 微信小程序登录
"""
import hashlib
import secrets
import requests as http_requests
from flask import Blueprint, request, jsonify

from backend.core.config import config
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


@auth_bp.post("/wechat")
def wechat_login():
    """
    微信小程序登录
    前端流程：wx.login() → 拿到 code → POST /api/auth/wechat {code, display_name?}
    后端流程：code → 调微信 API → openid → 查或建用户 → 返回 JWT
    """
    data = request.get_json() or {}
    code = (data.get("code") or "").strip()
    display_name = (data.get("display_name") or "微信用户").strip()

    if not code:
        return jsonify({"error": "缺少 code 参数"}), 400

    if not config.WECHAT_APPID or not config.WECHAT_SECRET:
        return jsonify({"error": "服务器未配置微信 AppID / Secret，请联系管理员"}), 500

    # ① 用 code 换 openid（服务器端调用，code 一次性有效）
    try:
        wx_resp = http_requests.get(
            "https://api.weixin.qq.com/sns/jscode2session",
            params={
                "appid": config.WECHAT_APPID,
                "secret": config.WECHAT_SECRET,
                "js_code": code,
                "grant_type": "authorization_code",
            },
            timeout=10,
        ).json()
    except Exception as e:
        return jsonify({"error": f"请求微信服务器失败: {e}"}), 502

    errcode = wx_resp.get("errcode", 0)
    if errcode != 0:
        return jsonify({"error": f"微信验证失败({errcode}): {wx_resp.get('errmsg', '')}"}), 401

    openid = wx_resp.get("openid")
    if not openid:
        return jsonify({"error": "微信未返回 openid"}), 401

    # ② 查找已有绑定；没有则创建新用户 + 绑定
    is_new = False
    with get_db() as conn:
        identity = row_to_dict(conn.execute(
            "SELECT * FROM user_identities WHERE platform = 'wechat' AND external_id = ?",
            (openid,),
        ).fetchone())

        if identity:
            user = row_to_dict(conn.execute(
                "SELECT id, display_name FROM users WHERE id = ?",
                (identity["user_id"],),
            ).fetchone())
        else:
            # 新用户：password_hash 用哨兵值（微信用户无密码）
            uid = new_id()
            conn.execute(
                "INSERT INTO users (id, password_hash, display_name) VALUES (?, 'wechat_auth', ?)",
                (uid, display_name),
            )
            conn.execute(
                "INSERT INTO user_identities (id, user_id, platform, external_id) VALUES (?, ?, 'wechat', ?)",
                (new_id(), uid, openid),
            )
            user = {"id": uid, "display_name": display_name}
            is_new = True

    token = generate_token(user["id"])
    return jsonify({
        "token": token,
        "user_id": user["id"],
        "display_name": user["display_name"],
        "is_new": is_new,   # 前端可用于判断是否需要引导填写昵称
    }), 200


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
