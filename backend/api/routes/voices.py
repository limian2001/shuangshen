from __future__ import annotations
"""
账号级声音复刻管理（v1.0）

设计要点：
- 声音归属 user，不再挂替身下 → 多个替身可共用一套复刻声，替身删除不影响声音
- 每账号最多 2 个：voice_kind=self（本人）1 个 + other（他人）1 个
- 复刻他人声音必须先确认授权（consent_at 存证）
- 扣费 VOICE_CLONE_COST 言己币，VOICE_CLONE_FREE=1 时限时免费
- 阿里云 CosyVoice 复刻接口要求公网音频 URL → 用带签名 token 的临时端点提供

GET    /api/voices                    我的声音列表
POST   /api/voices                    上传样本并复刻（multipart）
DELETE /api/voices/<voice_id>         删除声音（同步释放供应商配额）
POST   /api/voices/<voice_id>/apply-all   应用到我的所有替身
GET    /api/voices/sample/<token>     供应商回源取样本（一次性签名，无需登录）
"""
import hashlib
import hmac
import time
from pathlib import Path

from flask import Blueprint, request, jsonify, g, send_file

from backend.utils.auth import require_auth, new_id
from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.core.config import config
from backend.services.coins import spend_coins, get_balance

voices_bp = Blueprint("voices", __name__, url_prefix="/api/voices")

VOICE_DIR = config.UPLOAD_FOLDER / "user_voices"
VOICE_DIR.mkdir(parents=True, exist_ok=True)

# 音频要求（对齐 CosyVoice：10~60秒、≤10MB、wav/mp3/m4a）
MIN_BYTES = 20 * 1024          # 太小必然不足 10 秒
MAX_BYTES = 10 * 1024 * 1024
ALLOWED_EXT = (".wav", ".mp3", ".m4a", ".aac", ".webm")


# ─────────────────────────────────────────────
# 样本临时公开访问（供阿里云回源下载）
# ─────────────────────────────────────────────

def _sign(voice_id: str, exp: int) -> str:
    msg = f"{voice_id}:{exp}".encode()
    return hmac.new(config.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:32]


def _sample_url(voice_id: str) -> str:
    """生成 30 分钟有效的签名 URL"""
    base = (config.PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        raise RuntimeError("PUBLIC_BASE_URL 未配置（声音复刻需公网可访问的样本地址）")
    exp = int(time.time()) + 1800
    return f"{base}/api/voices/sample/{voice_id}?exp={exp}&sig={_sign(voice_id, exp)}"


@voices_bp.get("/sample/<voice_id>")
def get_sample(voice_id):
    """供应商回源取音频样本 —— 签名校验，不需要登录态"""
    try:
        exp = int(request.args.get("exp", 0))
    except ValueError:
        return jsonify({"error": "bad exp"}), 400
    sig = request.args.get("sig", "")
    if exp < time.time() or not hmac.compare_digest(sig, _sign(voice_id, exp)):
        return jsonify({"error": "链接已失效"}), 403

    with get_db() as conn:
        row = row_to_dict(conn.execute(
            "SELECT sample_path FROM user_voices WHERE id=?", (voice_id,)).fetchone())
    if not row or not row["sample_path"] or not Path(row["sample_path"]).exists():
        return jsonify({"error": "样本不存在"}), 404
    p = Path(row["sample_path"])
    mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4"}.get(
        p.suffix.lstrip("."), "audio/wav")
    return send_file(p, mimetype=mime)


# ─────────────────────────────────────────────
# 列表
# ─────────────────────────────────────────────

@voices_bp.get("")
@require_auth
def list_voices():
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT id, voice_kind, name, status, provider, instruction, consent_at, created_at,
                      (SELECT COUNT(*) FROM avatars a
                        WHERE a.user_voice_id = v.id AND a.status != 'deleted') AS bound_avatars
               FROM user_voices v
               WHERE user_id = ? ORDER BY created_at""",
            (g.user_id,)).fetchall())
    return jsonify({
        "voices": rows,
        "max": config.VOICE_MAX_PER_USER,
        "cost": config.VOICE_CLONE_COST,
        "free": config.VOICE_CLONE_FREE,
        "balance": get_balance(g.user_id),
    })


# ─────────────────────────────────────────────
# 创建（上传样本 → 复刻）
# ─────────────────────────────────────────────

@voices_bp.post("")
@require_auth
def create_voice():
    """
    Form fields:
      - audio:    音频文件（录音或上传）
      - kind:     self | other
      - name:     展示名
      - consent:  kind=other 时必须为 "1"（授权确认存证）
    """
    kind = (request.form.get("kind") or "self").strip()
    if kind not in ("self", "other"):
        return jsonify({"error": "kind 只能是 self 或 other"}), 400
    name = (request.form.get("name") or ("我的声音" if kind == "self" else "他人声音")).strip()[:20]
    instruction = (request.form.get("instruction") or "").strip()[:50]   # 方言/语气指令

    # 复刻他人声音必须确认授权
    if kind == "other" and request.form.get("consent") != "1":
        return jsonify({"error": "复刻他人声音需先确认授权"}), 400

    # 同类型只能有 1 个
    with get_db() as conn:
        exists = conn.execute(
            "SELECT id FROM user_voices WHERE user_id=? AND voice_kind=?",
            (g.user_id, kind)).fetchone()
    if exists:
        return jsonify({"error": f"已存在{'本人' if kind=='self' else '他人'}声音，请先删除再重新复刻"}), 400

    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "缺少音频文件"}), 400
    ext = Path(audio_file.filename or "sample.wav").suffix.lower() or ".wav"
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"不支持的格式 {ext}，请上传 wav / mp3 / m4a"}), 400
    audio_bytes = audio_file.read()
    if len(audio_bytes) < MIN_BYTES:
        return jsonify({"error": "音频太短，请录制或上传 10 秒以上的清晰人声"}), 400
    if len(audio_bytes) > MAX_BYTES:
        return jsonify({"error": "音频文件过大（上限 10MB），请裁剪后重传"}), 400

    # 扣费（限时免费时跳过）
    charged = 0
    if not config.VOICE_CLONE_FREE:
        ok, bal = spend_coins(g.user_id, config.VOICE_CLONE_COST, "voice_clone")
        if not ok:
            return jsonify({"error": f"言己币不足（需 {config.VOICE_CLONE_COST}，当前 {bal}）",
                            "need_coins": True}), 402
        charged = config.VOICE_CLONE_COST

    # 落库 + 存样本
    voice_id = new_id()
    path = VOICE_DIR / f"{voice_id}{ext}"
    path.write_bytes(audio_bytes)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO user_voices (id, user_id, voice_kind, name, provider,
                                        status, sample_path, instruction, consent_at)
               VALUES (?, ?, ?, ?, ?, 'training', ?, ?, ?)""",
            (voice_id, g.user_id, kind, name, config.TTS_PROVIDER, str(path), instruction,
             None if kind == "self" else _now()),   # 他人声音记录授权确认时间（存证）
        )

    # 调用供应商复刻
    try:
        from backend.services.media import aliyun_voice_clone
        provider_voice_id = aliyun_voice_clone(_sample_url(voice_id), prefix="yanji")
        with get_db() as conn:
            conn.execute(
                "UPDATE user_voices SET voice_id=?, status='ready' WHERE id=?",
                (provider_voice_id, voice_id))
    except Exception as e:
        with get_db() as conn:
            conn.execute("UPDATE user_voices SET status='failed' WHERE id=?", (voice_id,))
        # 复刻失败退款
        if charged:
            from backend.services.coins import award_coins
            award_coins(g.user_id, charged, "voice_clone_refund", voice_id)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        from backend.services.llm_provider import alert
        alert("声音复刻失败", f"user={g.user_id[:8]} kind={kind}: {e}")
        return jsonify({"error": f"声音复刻失败：{e}"}), 503

    return jsonify({"id": voice_id, "status": "ready", "name": name,
                    "message": "声音复刻成功"})


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────────────────────────
# 删除 / 应用到所有替身
# ─────────────────────────────────────────────

@voices_bp.delete("/<voice_id>")
@require_auth
def delete_voice(voice_id):
    with get_db() as conn:
        row = row_to_dict(conn.execute(
            "SELECT sample_path, voice_id, provider FROM user_voices WHERE id=? AND user_id=?",
            (voice_id, g.user_id)).fetchone())
        if not row:
            return jsonify({"error": "声音不存在"}), 404
        # 解绑所有替身
        conn.execute("UPDATE avatars SET user_voice_id=NULL WHERE user_voice_id=?", (voice_id,))
        conn.execute("DELETE FROM user_voices WHERE id=?", (voice_id,))

    # 释放供应商配额 + 删本地样本
    if row.get("voice_id") and row.get("provider") == "aliyun":
        try:
            from backend.services.media import aliyun_voice_delete
            aliyun_voice_delete(row["voice_id"])
        except Exception:
            pass
    try:
        if row.get("sample_path"):
            Path(row["sample_path"]).unlink(missing_ok=True)
    except Exception:
        pass
    return jsonify({"ok": True})


@voices_bp.post("/<voice_id>/preview")
@require_auth
def preview_voice(voice_id):
    """
    试听：用该复刻音色合成一段文字，直接返回 MP3。
    不依赖任何替身，复刻完立刻能听效果。
    Body: {"text": "..."}
    """
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "请输入试听文字"}), 400

    with get_db() as conn:
        v = row_to_dict(conn.execute(
            "SELECT voice_id, status, instruction FROM user_voices WHERE id=? AND user_id=?",
            (voice_id, g.user_id)).fetchone())
    if not v:
        return jsonify({"error": "声音不存在"}), 404
    if v.get("status") != "ready" or not v.get("voice_id"):
        return jsonify({"error": "声音尚未就绪"}), 400

    from flask import Response
    from backend.services.media import tts_synthesize
    try:
        audio = tts_synthesize(text[:200], voice_id=v["voice_id"],
                               instruction=v.get("instruction") or "")
    except RuntimeError as e:
        return jsonify({"error": f"合成失败：{e}"}), 503

    # 试听也算使用，刷新 last_used_at（供应商音色一年未用会被清理）
    with get_db() as conn:
        conn.execute("UPDATE user_voices SET last_used_at=datetime('now') WHERE id=?", (voice_id,))
    return Response(audio, mimetype="audio/mpeg")


@voices_bp.post("/<voice_id>/apply-all")
@require_auth
def apply_all(voice_id):
    """把该声音应用到我创建的所有替身"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM user_voices WHERE id=? AND user_id=? AND status='ready'",
            (voice_id, g.user_id)).fetchone()
        if not row:
            return jsonify({"error": "声音不存在或未就绪"}), 404
        cur = conn.execute(
            "UPDATE avatars SET user_voice_id=? WHERE creator_id=? AND status!='deleted'",
            (voice_id, g.user_id))
        count = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return jsonify({"ok": True, "applied": count})


# ─────────────────────────────────────────────
# 替身绑定声音（替身管理页-声音）
# ─────────────────────────────────────────────

@voices_bp.put("/avatar/<avatar_id>")
@require_auth
def set_avatar_voice(avatar_id):
    """
    Body: {"voice_id": "xxx"} 绑定；{"voice_id": null} 关闭
    关闭后该替身不显示小喇叭。
    """
    data = request.get_json() or {}
    vid = data.get("voice_id") or None

    with get_db() as conn:
        av = conn.execute(
            "SELECT id FROM avatars WHERE id=? AND creator_id=?",
            (avatar_id, g.user_id)).fetchone()
        if not av:
            return jsonify({"error": "替身不存在或无权限"}), 404
        if vid:
            ok = conn.execute(
                "SELECT id FROM user_voices WHERE id=? AND user_id=? AND status='ready'",
                (vid, g.user_id)).fetchone()
            if not ok:
                return jsonify({"error": "声音不存在或未就绪"}), 400
        conn.execute("UPDATE avatars SET user_voice_id=? WHERE id=?", (vid, avatar_id))
    return jsonify({"ok": True, "voice_id": vid})
