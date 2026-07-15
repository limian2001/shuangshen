from __future__ import annotations
"""
媒体路由 — STT / Vision / TTS / 声音克隆

POST /api/media/stt              录音 → 文字 (multipart audio file)
POST /api/media/vision           图片 → 描述文字 (multipart image)
POST /api/media/tts/<avatar_id>  文字 → MP3 音频 (JSON body)
POST /api/media/voice-sample/<avatar_id>  上传声音样本 (multipart)
GET  /api/media/voice-status/<avatar_id>  查询克隆状态
"""
import os
import uuid
from pathlib import Path

from flask import Blueprint, request, jsonify, g, Response

from backend.utils.auth import require_auth, new_id
from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.core.config import config
from backend.services.media import (
    stt_recognize,
    vision_describe,
    tts_synthesize,
    voice_clone_upload,
    voice_clone_train,
    voice_clone_status,
    make_thumbnail,
    detect_media_type,
)

media_bp = Blueprint("media", __name__, url_prefix="/api/media")

VOICE_SAMPLES_DIR = config.UPLOAD_FOLDER / "voice_samples"
THUMB_DIR         = config.UPLOAD_FOLDER / "image_thumbs"
VOICE_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)


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

    try:
        text = stt_recognize(audio_bytes, language)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"text": text, "language": language})


# ─────────────────────────────────────────────────────────────────────
# Vision
# ─────────────────────────────────────────────────────────────────────

@media_bp.post("/vision")
@require_auth
def vision_endpoint():
    """
    接收图片，返回 AI 描述（用于注入对话上下文）。

    Form fields:
      - image: 图片文件（JPEG / PNG / WEBP / GIF）
      - avatar_id: (可选) 关联替身 ID，用于存储图片描述到 image_cache
    """
    img_file = request.files.get("image")
    if not img_file:
        return jsonify({"error": "缺少 image 文件"}), 400

    img_bytes = img_file.read()
    if not img_bytes:
        return jsonify({"error": "图片为空"}), 400

    if len(img_bytes) > 20 * 1024 * 1024:  # 20 MB
        return jsonify({"error": "图片过大（上限 20MB）"}), 400

    media_type = detect_media_type(img_bytes)
    try:
        description = vision_describe(img_bytes, media_type)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    avatar_id = request.form.get("avatar_id", "").strip()

    # 存缩略图 + 描述到 image_cache
    thumb_id = None
    if avatar_id:
        thumb_id = _save_image_cache(avatar_id, img_bytes, description)

    return jsonify({
        "description": description,
        "thumb_id": thumb_id,
    })


def _save_image_cache(avatar_id: str, img_bytes: bytes, description: str) -> str:
    """保存缩略图 + description 到 image_cache 表，超上限时淘汰低分图片。"""
    thumb_bytes = make_thumbnail(img_bytes)
    img_id = new_id()
    thumb_path = THUMB_DIR / f"{img_id}.jpg"
    thumb_path.write_bytes(thumb_bytes)

    with get_db() as conn:
        # 淘汰：若超 IMAGE_MAX_PER_AVATAR，删除最低 relevance_score 的记录
        count = conn.execute(
            "SELECT COUNT(*) FROM image_cache WHERE avatar_id = ?", (avatar_id,)
        ).fetchone()[0]
        if count >= config.IMAGE_MAX_PER_AVATAR:
            old = conn.execute(
                "SELECT id, thumb_path FROM image_cache WHERE avatar_id = ? "
                "ORDER BY relevance_score ASC, last_accessed ASC LIMIT ?",
                (avatar_id, max(1, count - config.IMAGE_MAX_PER_AVATAR + 1)),
            ).fetchall()
            for row in old:
                try:
                    Path(row["thumb_path"]).unlink(missing_ok=True)
                except Exception:
                    pass
                conn.execute("DELETE FROM image_cache WHERE id = ?", (row["id"],))

        conn.execute(
            """INSERT INTO image_cache (id, avatar_id, thumb_path, description)
               VALUES (?, ?, ?, ?)""",
            (img_id, avatar_id, str(thumb_path), description),
        )
    return img_id


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

    # 拉取替身声音配置
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT voice_model_id, voice_language FROM avatars WHERE id = ?",
            (avatar_id,),
        ).fetchone())
    if not avatar:
        return jsonify({"error": "替身不存在"}), 404

    voice_id = avatar.get("voice_model_id") or None
    language = avatar.get("voice_language") or "zh"

    try:
        audio_bytes = tts_synthesize(text, voice_id=voice_id, language=language)
    except RuntimeError as e:
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

    # 上传数量检查
    with get_db() as conn:
        sample_count = conn.execute(
            "SELECT COUNT(*) FROM voice_samples WHERE avatar_id = ?", (avatar_id,)
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

    # 上传到火山引擎 + 触发训练
    try:
        audio_id = voice_clone_upload(avatar_id, audio_bytes, filename)
        with get_db() as conn:
            conn.execute(
                "UPDATE voice_samples SET clone_voice_id=?, status='uploaded' WHERE id=?",
                (audio_id, sample_id),
            )

        # 拉所有已上传的 audio_id 提交训练
        with get_db() as conn:
            audio_ids = [
                r["clone_voice_id"]
                for r in rows_to_list(conn.execute(
                    "SELECT clone_voice_id FROM voice_samples WHERE avatar_id=? AND status='uploaded'",
                    (avatar_id,),
                ).fetchall())
                if r["clone_voice_id"]
            ]
        if audio_ids:
            speaker_id = voice_clone_train(avatar_id, audio_ids, language)
            with get_db() as conn:
                conn.execute(
                    "UPDATE voice_samples SET status='training' WHERE avatar_id=?",
                    (avatar_id,),
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
