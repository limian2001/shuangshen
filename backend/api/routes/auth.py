from __future__ import annotations
"""
认证路由：注册 / 登录 / 微信小程序登录

微信登录流程（两步）：
  步骤1 — POST /api/auth/wechat {code}
    → 用 wx.login() code 换 openid
    → 若 openid 已绑定用户 → 直接返回 JWT（静默登录）
    → 若未绑定 → 返回 {need_phone: true}，前端展示手机号授权按钮

  步骤2（仅新用户）— POST /api/auth/wechat_phone {wx_code, phone_code, display_name?}
    → 拿 openid + 手机号（含国家代码）
    → 查找 phone 已注册 → 绑定 openid → 返回 JWT
    → 否则建新用户（phone 为主标识）+ 绑定 openid → 返回 JWT
"""
import hashlib
import secrets
import requests as http_requests
from flask import Blueprint, request, jsonify

from backend.core.config import config
from backend.db.database import get_db, row_to_dict
from backend.utils.auth import generate_token, new_id

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


# ─────────────────────────────────────────────
# 密码工具
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# 微信 API 辅助
# ─────────────────────────────────────────────

def _wx_get_openid(code: str) -> tuple[str | None, str | None]:
    """用 wx.login() code 换 openid。返回 (openid, errmsg)"""
    try:
        resp = http_requests.get(
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
        return None, f"请求微信服务器失败: {e}"
    errcode = resp.get("errcode", 0)
    if errcode != 0:
        return None, f"微信验证失败({errcode}): {resp.get('errmsg', '')}"
    openid = resp.get("openid")
    if not openid:
        return None, "微信未返回 openid"
    return openid, None


def _wx_get_access_token() -> tuple[str | None, str | None]:
    """获取小程序 access_token。返回 (token, errmsg)"""
    try:
        resp = http_requests.get(
            "https://api.weixin.qq.com/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": config.WECHAT_APPID,
                "secret": config.WECHAT_SECRET,
            },
            timeout=10,
        ).json()
    except Exception as e:
        return None, f"获取 access_token 失败: {e}"
    if resp.get("errcode", 0) != 0:
        return None, f"access_token 错误({resp.get('errcode')}): {resp.get('errmsg', '')}"
    return resp.get("access_token"), None


def _wx_get_phone(phone_code: str) -> tuple[str | None, str | None]:
    """
    用 getPhoneNumber 返回的 code 换真实手机号（含国家代码，如 +8613800000000）。
    返回 (phone, errmsg)
    """
    access_token, err = _wx_get_access_token()
    if err:
        return None, err
    try:
        resp = http_requests.post(
            f"https://api.weixin.qq.com/wxa/business/getuserphonenumber?access_token={access_token}",
            json={"code": phone_code},
            timeout=10,
        ).json()
    except Exception as e:
        return None, f"获取手机号失败: {e}"
    errcode = resp.get("errcode", 0)
    if errcode != 0:
        return None, f"获取手机号失败({errcode}): {resp.get('errmsg', '')}"
    phone = (resp.get("phone_info") or {}).get("phoneNumber", "")
    if not phone:
        return None, "微信未返回手机号"
    # 确保含国家代码（微信通常已带 +，兜底补 +86）
    if not phone.startswith("+"):
        phone = "+86" + phone
    return phone, None


# ─────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────

@auth_bp.post("/register")
def register():
    """Web 端注册（暂未对外开放）"""
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
    """Web 端登录（暂未对外开放）"""
    data = request.get_json() or {}
    identifier = (data.get("phone") or data.get("email") or "").strip()
    password = data.get("password", "")

    if not identifier or not password:
        return jsonify({"error": "账号和密码不能为空"}), 400

    # 兼容手机号有无 +86 前缀（管理员手动创建账号时可能格式不统一）
    candidates = [identifier]
    if identifier.startswith("+86"):
        candidates.append(identifier[3:])   # +8613xxx → 13xxx
    elif identifier.lstrip("0").isdigit():
        candidates.append("+86" + identifier)  # 13xxx → +8613xxx

    with get_db() as conn:
        user = None
        for cand in candidates:
            user = row_to_dict(conn.execute(
                "SELECT * FROM users WHERE phone = ? OR email = ?",
                (cand, cand),
            ).fetchone())
            if user:
                break

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
    微信小程序静默登录（步骤1）。
    前端：wx.login() → code → POST /api/auth/wechat {code}
    - openid 已绑定 → 返回 JWT（返回用户直接进入）
    - openid 未绑定 → 返回 {need_phone: true}（新用户，前端展示手机授权按钮）
    """
    data = request.get_json() or {}
    code = (data.get("code") or "").strip()

    if not code:
        return jsonify({"error": "缺少 code 参数"}), 400
    if not config.WECHAT_APPID or not config.WECHAT_SECRET:
        return jsonify({"error": "服务器未配置微信 AppID / Secret"}), 500

    openid, err = _wx_get_openid(code)
    if err:
        return jsonify({"error": err}), 502

    with get_db() as conn:
        identity = row_to_dict(conn.execute(
            "SELECT user_id FROM user_identities WHERE platform = 'wechat' AND external_id = ?",
            (openid,),
        ).fetchone())

    if not identity:
        # 新用户，需要走手机号授权流程
        return jsonify({"need_phone": True}), 200

    with get_db() as conn:
        user = row_to_dict(conn.execute(
            "SELECT id, display_name FROM users WHERE id = ?",
            (identity["user_id"],),
        ).fetchone())

    token = generate_token(user["id"])
    return jsonify({
        "token": token,
        "user_id": user["id"],
        "display_name": user["display_name"],
        "is_new": False,
    })


@auth_bp.post("/wechat_phone")
def wechat_phone_login():
    """
    微信小程序手机号注册/登录（步骤2，仅新用户）。
    前端：getPhoneNumber 按钮 → phone_code + wx_code → POST /api/auth/wechat_phone
    - 手机号已注册 → 绑定 openid → 返回 JWT
    - 手机号未注册 → 建新用户（phone 为主标识）+ 绑定 openid → 返回 JWT
    """
    data = request.get_json() or {}
    wx_code    = (data.get("wx_code")    or "").strip()
    phone_code = (data.get("phone_code") or "").strip()
    display_name = (data.get("display_name") or "").strip() or "言己用户"

    if not wx_code or not phone_code:
        return jsonify({"error": "缺少 wx_code 或 phone_code"}), 400
    if not config.WECHAT_APPID or not config.WECHAT_SECRET:
        return jsonify({"error": "服务器未配置微信 AppID / Secret"}), 500

    # 并行获取 openid 和手机号
    openid, err = _wx_get_openid(wx_code)
    if err:
        return jsonify({"error": err}), 502

    phone, err = _wx_get_phone(phone_code)
    if err:
        return jsonify({"error": err}), 502

    with get_db() as conn:
        # 先查手机号是否已存在
        existing_user = row_to_dict(conn.execute(
            "SELECT id, display_name FROM users WHERE phone = ?",
            (phone,),
        ).fetchone())

        if existing_user:
            # 手机号已注册，绑定 openid（如果还没绑过）
            uid = existing_user["id"]
            already = conn.execute(
                "SELECT 1 FROM user_identities WHERE platform = 'wechat' AND external_id = ?",
                (openid,),
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO user_identities (id, user_id, platform, external_id) VALUES (?, ?, 'wechat', ?)",
                    (new_id(), uid, openid),
                )
            user = existing_user
            is_new = False
        else:
            # 全新用户：以手机号为主标识建账号
            uid = new_id()
            conn.execute(
                "INSERT INTO users (id, phone, password_hash, display_name) VALUES (?, ?, 'wechat_auth', ?)",
                (uid, phone, display_name),
            )
            conn.execute(
                "INSERT INTO user_identities (id, user_id, platform, external_id) VALUES (?, ?, 'wechat', ?)",
                (new_id(), uid, openid),
            )
            user = {"id": uid, "display_name": display_name}
            is_new = True

    token = generate_token(uid)
    return jsonify({
        "token": token,
        "user_id": uid,
        "display_name": user["display_name"],
        "is_new": is_new,
    })


@auth_bp.get("/me")
def me():
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
