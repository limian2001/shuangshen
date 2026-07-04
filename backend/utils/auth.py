from __future__ import annotations
"""
JWT 认证工具
"""
import jwt
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import request, jsonify, g

from backend.core.config import config


def generate_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=config.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])


def require_auth(f):
    """路由装饰器：要求 JWT 认证，注入 g.user_id"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "未认证，请登录"}), 401
        token = auth[7:]
        try:
            payload = decode_token(token)
            g.user_id = payload["sub"]
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token 已过期，请重新登录"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "无效 Token"}), 401
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """路由装饰器：要求管理员身份（is_admin=1），注入 g.user_id"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "未认证，请登录"}), 401
        token = auth[7:]
        try:
            payload = decode_token(token)
            user_id = payload["sub"]
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token 已过期，请重新登录"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "无效 Token"}), 401

        # 查询管理员标志
        from backend.db.database import get_db, row_to_dict
        with get_db() as conn:
            user = row_to_dict(conn.execute(
                "SELECT id, is_admin, status FROM users WHERE id = ?", (user_id,)
            ).fetchone())
        if not user or not user.get("is_admin"):
            return jsonify({"error": "权限不足"}), 403
        g.user_id = user_id
        return f(*args, **kwargs)
    return decorated


def new_id() -> str:
    """生成 UUID"""
    return str(uuid.uuid4())
