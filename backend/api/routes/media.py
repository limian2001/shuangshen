from __future__ import annotations
"""
媒体路由 — STT / TTS / 声音克隆

POST /api/media/stt              录音 → 文字 (multipart audio file)
POST /api/media/tts/<avatar_id>  文字 → MP3 音频 (JSON body)
POST /api/media/voice-sample/<avatar_id>  上传声音样本 (multipart)
GET  /api/media/voice-status/<avatar_id>  查询克隆状态
"""
from pathlib import Path

from flask import Blueprint, request, jsonify, g, Response

from backend.utils.auth import require_auth, new_id
from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.core.config import config
from backend.services.media import (
    stt_recognize,
    tts_synthesize,
    voice_clone_upload,
    voice_clone_status,
)

media_bp = Blueprint("media", __name__, url_prefix="/api/media")

VOICE_SAMPLES_DIR = config.UPLOAD_FOLDER / "voice_samples"
VOICE_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────
# STT
# ─────────────────────────────────────────────────────────────────────

@media_bp.post("/stt")
@require_auth
def stt_endpoint():
    """
    接收音频文件，返回识别文字。

    Form fields:
      - audio: 音频文件（webm / mp3 / wav / m4a / aac）
      - language: zh | yue | en  (默认 zh)
    """
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "缺少 audio 文件"}), 400

    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({"error": "音频文件为空"}), 400

    if len(audio_bytes) > 10 * 1024 * 1024:  # 10 MB
        return jsonify({"error": "音频文件过大（上限 10MB）"}), 400

    language = request.form.get("language", "zh").strip()
    if language not in ("zh", "yue", "en"):
        language = "zh"

    filename = audio_file.filename or ""

    try:
        text = stt_recognize(audio_bytes, language, filename)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"text": text, "language": language})


# ─────────────────────────────────────────────────────────────────────
# TTS
# ─────────────────────────────────────────────────────────────────────

@media_bp.post("/tts/<avatar_id>")
@require_auth
def tts_endpoint(avatar_id):
    """
    文字 → MP3 音频。

    Body JSON:
      - text: 要合成的文字
    """
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text 不能为空"}), 400
    if len(text) > 500:
        text = text[:500]

    # 拉取替身声音配置：优先账号级复刻声（user_voice_id），回退旧的 voice_model_id
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            """SELECT a.voice_model_id, a.voice_language, a.user_voice_id,
                      v.voice_id AS uv_voice_id, v.status AS uv_status
               FROM avatars a
               LEFT JOIN user_voices v ON a.user_voice_id = v.id
               WHERE a.id = ?""",
            (avatar_id,),
        ).fetchone())
    if not avatar:
        return jsonify({"error": "替身不存在"}), 404

    if avatar.get("uv_voice_id") and avatar.get("uv_status") == "ready":
        voice_id = avatar["uv_voice_id"]
        # 记录使用时间（供应商音色一年未用会被自动清理）
        with get_db() as conn:
            conn.execute("UPDATE user_voices SET last_used_at=datetime('now') WHERE id=?",
                         (avatar["user_voice_id"],))
    else:
        voice_id = avatar.get("voice_model_id") or None
    language = avatar.get("voice_language") or "zh"

    try:
        audio_bytes = tts_synthesize(text, voice_id=voice_id, language=language)
    except RuntimeError as e:
        # 克隆音色失败（如训练未完成）→ 自动降级为标准普通话音色
        # 注意打日志：降级是兜底，频繁触发说明克隆合成链路有问题
        if voice_id:
            from backend.services.llm_provider import alert
            alert("TTS降级", f"克隆音色合成失败，降级标准音色 voice_id={voice_id}: {e}")
            try:
                audio_bytes = tts_synthesize(text, voice_id=None, language=language)
            except RuntimeError as e2:
                return jsonify({"error": str(e2)}), 503
        else:
            return jsonify({"error": str(e)}), 503

    return Response(audio_bytes, mimetype="audio/mpeg")


# ─────────────────────────────────────────────────────────────────────
# 声音克隆：样本上传
# ─────────────────────────────────────────────────────────────────────

@media_bp.post("/voice-sample/<avatar_id>")
@require_auth
def upload_voice_sample(avatar_id):
    """
    上传声音样本（MP3 / WAV / M4A）。
    每个替身至多上传 5 个样本。
    上传完成后自动触发声音克隆训练（需要至少1个样本）。

    Form fields:
      - audio: 声音样本文件
      - language: zh | yue | en  (可选，默认读替身配置)
    """
    # 鉴权：仅替身创建者可操作
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT id, creator_id, voice_model_id FROM avatars WHERE id = ?",
            (avatar_id,),
        ).fetchone())
    if not avatar:
        return jsonify({"error": "替身不存在"}), 404
    if avatar["creator_id"] != g.user_id:
        return jsonify({"error": "无权限"}), 403

    # 上传数量检查（只统计成功的样本，失败的不占配额）
    with get_db() as conn:
        sample_count = conn.execute(
            "SELECT COUNT(*) FROM voice_samples WHERE avatar_id = ? AND status != 'failed'",
            (avatar_id,)
        ).fetchone()[0]
    if sample_count >= 5:
        return jsonify({"error": "每个替身最多上传 5 个声音样本"}), 400

    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "缺少 audio 文件"}), 400

    audio_bytes = audio_file.read()
    if len(audio_bytes) < 10000:
        return jsonify({"error": "音频太短，请上传至少10秒的清晰录音"}), 400
    if len(audio_bytes) > 50 * 1024 * 1024:
        return jsonify({"error": "音频文件过大（上限 50MB）"}), 400

    language = (request.form.get("language") or "zh").strip()

    # 保存本地
    sample_id = new_id()
    filename = f"{sample_id}_{audio_file.filename or 'sample.mp3'}"
    avatar_sample_dir = VOICE_SAMPLES_DIR / avatar_id
    avatar_sample_dir.mkdir(parents=True, exist_ok=True)
    file_path = avatar_sample_dir / filename
    file_path.write_bytes(audio_bytes)

    # 记录 DB
    with get_db() as conn:
        conn.execute(
            """INSERT INTO voice_samples (id, avatar_id, filename, file_path, status)
               VALUES (?, ?, ?, ?, 'uploading')""",
            (sample_id, avatar_id, filename, str(file_path)),
        )

    # ── 选定 speaker_id ──────────────────────────────────────────
    # 火山声音复刻的 speaker_id 必须是控制台购买音色获得的 S_ 开头 ID。
    # 优先沿用替身已分配的；否则从 VOLC_SPEAKER_IDS 池中取一个未被占用的。
    speaker_id = (avatar.get("voice_model_id") or "").strip()
    if not speaker_id.startswith("S_"):
        pool = [s.strip() for s in config.VOLC_SPEAKER_IDS.split(",") if s.strip()]
        if pool:
            with get_db() as conn:
                used = {
                    r["voice_model_id"]
                    for r in rows_to_list(conn.execute(
                        "SELECT voice_model_id FROM avatars "
                        "WHERE voice_model_id IS NOT NULL AND voice_model_id != '' AND id != ?",
                        (avatar_id,),
                    ).fetchall())
                }
            free = [s for s in pool if s not in used]
            if not free:
                return jsonify({"error": "音色名额已用完：控制台购买的 speaker_id 均已分配给其他替身"}), 400
            speaker_id = free[0]
        else:
            speaker_id = avatar_id[:64]   # 未配置池时沿用旧逻辑（部分账号支持）

    # 上传到火山引擎（upload 接口同时触发训练，返回 speaker_id）
    try:
        speaker_id = voice_clone_upload(speaker_id, audio_bytes, filename)
        with get_db() as conn:
            conn.execute(
                "UPDATE voice_samples SET clone_voice_id=?, status='training' WHERE id=?",
                (speaker_id, sample_id),
            )
            conn.execute(
                "UPDATE avatars SET voice_model_id=?, voice_language=? WHERE id=?",
                (speaker_id, language, avatar_id),
            )
        status_msg = "已提交声音克隆训练，请稍后查看状态"
    except RuntimeError as e:
        with get_db() as conn:
            conn.execute(
                "UPDATE voice_samples SET status='failed' WHERE id=?", (sample_id,)
            )
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"error": str(e)}), 503

    return jsonify({"sample_id": sample_id, "message": status_msg})


# ─────────────────────────────────────────────────────────────────────
# 声音克隆：状态查询
# ─────────────────────────────────────────────────────────────────────

@media_bp.get("/voice-status/<avatar_id>")
@require_auth
def get_voice_status(avatar_id):
    """查询替身声音克隆状态"""
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT creator_id, voice_model_id, voice_language FROM avatars WHERE id=?",
            (avatar_id,),
        ).fetchone())
        samples = rows_to_list(conn.execute(
            "SELECT id, filename, status, created_at FROM voice_samples WHERE avatar_id=? ORDER BY created_at DESC",
            (avatar_id,),
        ).fetchall())

    if not avatar:
        return jsonify({"error": "替身不存在"}), 404
    if avatar["creator_id"] != g.user_id:
        return jsonify({"error": "无权限"}), 403

    speaker_id = avatar.get("voice_model_id", "")
    clone_status = {"status": "not_started", "voice_id": ""}

    if speaker_id:
        try:
            clone_status = voice_clone_status(speaker_id)
            # 若训练成功，更新 voice_model_id 为最终 voice_id
            if clone_status["status"] == "success" and clone_status.get("voice_id"):
                with get_db() as conn:
                    conn.execute(
                        "UPDATE avatars SET voice_model_id=? WHERE id=?",
                        (clone_status["voice_id"], avatar_id),
                    )
                    conn.execute(
                        "UPDATE voice_samples SET status='ready' WHERE avatar_id=?",
                        (avatar_id,),
                    )
        except Exception:
            clone_status = {"status": "unknown", "voice_id": ""}

    return jsonify({
        "clone_status": clone_status,
        "voice_language": avatar.get("voice_language", "zh"),
        "samples": samples,
    })


# ─────────────────────────────────────────────────────────────────────
# 删除声音样本
# ─────────────────────────────────────────────────────────────────────

@media_bp.delete("/voice-sample/<avatar_id>/<sample_id>")
@require_auth
def delete_voice_sample(avatar_id, sample_id):
    """删除声音样本"""
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT creator_id FROM avatars WHERE id=?", (avatar_id,)
        ).fetchone())
    if not avatar or avatar["creator_id"] != g.user_id:
        return jsonify({"error": "无权限"}), 403

    with get_db() as conn:
        sample = row_to_dict(conn.execute(
            "SELECT file_path FROM voice_samples WHERE id=? AND avatar_id=?",
            (sample_id, avatar_id),
        ).fetchone())
        if sample:
            try:
                Path(sample["file_path"]).unlink(missing_ok=True)
            except Exception:
                pass
            conn.execute("DELETE FROM voice_samples WHERE id=?", (sample_id,))

    return jsonify({"ok": True})
