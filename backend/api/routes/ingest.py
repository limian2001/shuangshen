"""
数据喂入路由 — 微信聊天记录上传与解析
"""
import json
import os
from pathlib import Path
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth, new_id
from backend.core.config import config
from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.services.wechat_parser import parse_wechat_txt, extract_style_samples
from backend.services.memory_store import add_memories_batch
from backend.services.persona_builder import rebuild_persona_prompt

ingest_bp = Blueprint("ingest", __name__, url_prefix="/api/ingest")


@ingest_bp.post("/<avatar_id>/wechat")
@require_auth
def upload_wechat(avatar_id):
    """
    上传微信聊天记录 txt 文件并解析。

    Form-data:
        file: 微信导出的 .txt 文件
        my_names: （可选）识别「我」的名称，逗号分隔，如 "小王,Wang"

    Returns:
        job_id: 异步任务 ID（当前为同步处理）
        stats: 解析统计
    """
    # 验证权限
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT * FROM avatars WHERE id = ? AND creator_id = ?",
            (avatar_id, g.user_id),
        ).fetchone())
    if not avatar:
        return jsonify({"error": "替身不存在或无权限"}), 403

    # 接收文件
    if "file" not in request.files:
        return jsonify({"error": "未上传文件，请使用 multipart/form-data 上传 file 字段"}), 400

    f = request.files["file"]
    if not f.filename.endswith(".txt"):
        return jsonify({"error": "仅支持 .txt 格式（微信导出的聊天记录）"}), 400

    # 保存到 uploads 目录
    save_name = f"{new_id()}_{f.filename}"
    save_path = config.UPLOAD_FOLDER / save_name
    f.save(str(save_path))

    # 解析「我」的名称
    my_names_raw = request.form.get("my_names", "")
    my_names = [n.strip() for n in my_names_raw.split(",") if n.strip()] or None

    # 创建任务记录
    job_id = new_id()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO ingest_jobs (id, avatar_id, filename, status)
               VALUES (?, ?, ?, 'processing')""",
            (job_id, avatar_id, f.filename),
        )

    # 同步解析（v0.2 可改为 Celery 异步任务）
    try:
        result = parse_wechat_txt(save_path, my_names)
        stats = result["stats"]

        # 将「我」说的话转为记忆条目
        my_msgs = result["my_messages"]
        style_samples = extract_style_samples(my_msgs, max_samples=200)

        # 构建批量导入数据
        items = [
            {
                "content": text,
                "mem_type": "wechat",
                "source": "wechat_import",
                "topic_tags": [],
            }
            for text in style_samples
        ]

        imported = add_memories_batch(avatar_id, items)

        # 触发人格 Prompt 重建
        try:
            rebuild_persona_prompt(avatar_id)
            persona_rebuilt = True
        except Exception as e:
            persona_rebuilt = False
            persona_error = str(e)

        # 更新任务状态
        with get_db() as conn:
            conn.execute(
                """UPDATE ingest_jobs
                   SET status='done', total_msgs=?, imported=?, finished_at=datetime('now')
                   WHERE id=?""",
                (stats["total"], imported, job_id),
            )

        # 清理上传文件
        save_path.unlink(missing_ok=True)

        return jsonify({
            "job_id": job_id,
            "status": "done",
            "stats": {
                "total_messages": stats["total"],
                "my_messages": stats["mine"],
                "other_messages": stats["others"],
                "imported_to_memory": imported,
                "inferred_my_names": result.get("inferred_my_names", []),
                "date_range": stats["date_range"],
                "unique_senders": stats["unique_senders"],
            },
            "persona_rebuilt": persona_rebuilt,
        })

    except Exception as e:
        with get_db() as conn:
            conn.execute(
                "UPDATE ingest_jobs SET status='failed', error_msg=? WHERE id=?",
                (str(e)[:500], job_id),
            )
        save_path.unlink(missing_ok=True)
        return jsonify({"error": f"解析失败: {str(e)}"}), 500


@ingest_bp.get("/<avatar_id>/jobs")
@require_auth
def list_jobs(avatar_id):
    """查看导入任务历史"""
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT id FROM avatars WHERE id = ? AND creator_id = ?",
            (avatar_id, g.user_id),
        ).fetchone())
    if not avatar:
        return jsonify({"error": "无权限"}), 403

    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT id, filename, status, total_msgs, imported, error_msg, created_at, finished_at
               FROM ingest_jobs WHERE avatar_id = ? ORDER BY created_at DESC LIMIT 20""",
            (avatar_id,),
        ).fetchall())
    return jsonify(rows)
