"""
数据喂入路由 v0.3

两个入口：
  POST /api/ingest/:avatar_id/wechat   — 上传 .txt 文件
  POST /api/ingest/:avatar_id/paste    — 直接粘贴文本（最简单）

导入流程：
  1. 解析聊天记录（支持多格式）
  2. 提取风格样本存入 memories（原有逻辑）
  3. LLM 语义解析：从对话中提取结构化记忆（事实/观点/共同经历）
  4. 触发人格 Prompt 重建
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional, List
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth, new_id
from backend.core.config import config
from backend.db.database import get_db, row_to_dict, rows_to_list
from backend.services.wechat_parser import (
    parse_wechat_txt, parse_text,
    extract_style_samples, format_conversation_for_llm,
)
from backend.services.memory_store import add_memories_batch
from backend.services.persona_builder import rebuild_persona_prompt

ingest_bp = Blueprint("ingest", __name__, url_prefix="/api/ingest")

# 微信导出引导文本
WECHAT_EXPORT_GUIDE = (
    "微信聊天记录导出方法：\n"
    "① PC版微信：聊天窗口右上角「...」→「查找/备份聊天记录」，用第三方工具导出为 txt\n"
    "② 移动端：进入聊天 → 右上角「...」→「更多」→「邮件聊天记录」（有数量上限）\n"
    "③ 最简单：直接复制聊天内容，调用 /paste 接口粘贴文本"
)


def _check_creator(avatar_id: str) -> "Optional[dict]":
    with get_db() as conn:
        return row_to_dict(conn.execute(
            "SELECT * FROM avatars WHERE id = ? AND creator_id = ?",
            (avatar_id, g.user_id),
        ).fetchone())


@ingest_bp.post("/<avatar_id>/paste")
@require_auth
def paste_text(avatar_id):
    """
    直接粘贴聊天记录文本导入（无需文件，最简单的方式）。

    Body JSON:
      text:     聊天记录文本（必填）
      my_names: 识别「我」的名称，逗号分隔（可选）

    支持格式：
      - 微信 txt 导出格式
      - "名字: 内容" 简化格式
      - 任意文本段落
    """
    avatar = _check_creator(avatar_id)
    if not avatar:
        return jsonify({"error": "替身不存在或无权限"}), 403

    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text 不能为空"}), 400
    if len(text) > 500_000:
        return jsonify({"error": "文本过长，请分批导入（单次上限 50 万字）"}), 400

    my_names_raw = data.get("my_names", "")
    my_names = [n.strip() for n in my_names_raw.split(",") if n.strip()] or None

    return _do_import(avatar_id, text, my_names, source="paste")


@ingest_bp.post("/<avatar_id>/wechat")
@require_auth
def upload_wechat(avatar_id):
    """
    上传微信聊天记录 .txt 文件。

    Form-data:
      file:     .txt 文件
      my_names: 识别「我」的名称，逗号分隔（可选）
    """
    avatar = _check_creator(avatar_id)
    if not avatar:
        return jsonify({"error": "替身不存在或无权限"}), 403

    if "file" not in request.files:
        return jsonify({
            "error": "未上传文件",
            "guide": WECHAT_EXPORT_GUIDE,
        }), 400

    f = request.files["file"]
    if not f.filename.lower().endswith(".txt"):
        return jsonify({"error": "仅支持 .txt 格式"}), 400

    save_name = f"{new_id()}_{f.filename}"
    save_path = config.UPLOAD_FOLDER / save_name
    f.save(str(save_path))

    my_names_raw = request.form.get("my_names", "")
    my_names = [n.strip() for n in my_names_raw.split(",") if n.strip()] or None

    try:
        result = parse_wechat_txt(save_path, my_names)
        text = "\n".join(
            f"{m['sender']}: {m['content']}" for m in result["messages"]
        )
        response = _do_import(avatar_id, text, my_names, source="wechat_file",
                              pre_parsed=result)
    finally:
        save_path.unlink(missing_ok=True)

    return response


@ingest_bp.post("/<avatar_id>/detect")
@require_auth
def detect_senders(avatar_id):
    """
    解析聊天记录，返回发言人列表，不导入。
    供前端展示「请选择哪个是你」确认步骤。

    支持：
      JSON body {text}  — 粘贴文本
      form-data  {file} — 上传 .txt 文件
    """
    if not _check_creator(avatar_id):
        return jsonify({"error": "无权限"}), 403

    # ── 取文本 ──
    if request.is_json:
        text = (request.get_json() or {}).get("text", "").strip()
        if not text:
            return jsonify({"error": "text 不能为空"}), 400
    elif "file" in request.files:
        raw = request.files["file"].read()
        text = None
        for enc in ["utf-8", "utf-8-sig", "gbk", "gb18030"]:
            try:
                text = raw.decode(enc)
                break
            except Exception:
                pass
        if text is None:
            return jsonify({"error": "无法解码文件，请确认为 UTF-8 或 GBK 编码"}), 400
    else:
        return jsonify({"error": "需要提供 text 或 file"}), 400

    from collections import Counter
    parsed = parse_text(text, my_names=None)
    counts = Counter(m["sender"] for m in parsed["messages"])
    inferred = parsed.get("inferred_my_names", [])

    # 过滤掉空名和系统占位
    senders = [
        {"name": s, "count": counts[s], "is_me_guess": s in inferred}
        for s in sorted(counts, key=lambda x: -counts[x])
        if s and s not in ("unknown", "")
    ]

    return jsonify({
        "senders": senders,
        "total_messages": len(parsed["messages"]),
    })


@ingest_bp.get("/<avatar_id>/jobs")
@require_auth
def list_jobs(avatar_id):
    """查看导入任务历史"""
    if not _check_creator(avatar_id):
        return jsonify({"error": "无权限"}), 403

    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT id, filename, status, total_msgs, imported, error_msg, created_at, finished_at
               FROM ingest_jobs WHERE avatar_id = ? ORDER BY created_at DESC LIMIT 20""",
            (avatar_id,),
        ).fetchall())
    return jsonify(rows)


# ─────────────────────────────────────────────
# 核心导入逻辑
# ─────────────────────────────────────────────

def _do_import(
    avatar_id: str,
    text: str,
    my_names: Optional[List[str]],
    source: str = "paste",
    pre_parsed: dict = None,
) -> object:
    """统一的导入处理逻辑"""
    from flask import current_app

    job_id = new_id()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO ingest_jobs (id, avatar_id, filename, status)
               VALUES (?, ?, ?, 'processing')""",
            (job_id, avatar_id, f"{source}_import"),
        )

    try:
        # ① 解析
        parsed = pre_parsed or parse_text(text, my_names)
        stats = parsed["stats"]
        my_messages = parsed["my_messages"]
        all_messages = parsed.get("all_messages", parsed["messages"])

        # ② 风格样本存入 memories（原有逻辑）
        style_samples = extract_style_samples(my_messages, max_samples=200)
        style_items = [
            {"content": t, "mem_type": "wechat", "source": source, "topic_tags": []}
            for t in style_samples
        ]
        style_count = add_memories_batch(avatar_id, style_items)

        # ③ LLM 语义解析：提取结构化记忆（核心新功能）
        semantic_count = 0
        semantic_error = None
        try:
            semantic_memories = _extract_semantic_memories(all_messages, avatar_id)
            if semantic_memories:
                semantic_count = add_memories_batch(avatar_id, semantic_memories)
        except Exception as e:
            semantic_error = str(e)
            print(f"[INGEST] LLM 语义解析失败（不影响导入）: {e}")

        # ④ 触发人格 Prompt 重建
        try:
            rebuild_persona_prompt(avatar_id)
            persona_rebuilt = True
        except Exception as e:
            persona_rebuilt = False
            print(f"[INGEST] 人格重建失败: {e}")

        # ⑤ 更新任务状态
        total_imported = style_count + semantic_count
        with get_db() as conn:
            conn.execute(
                """UPDATE ingest_jobs
                   SET status='done', total_msgs=?, imported=?, finished_at=datetime('now')
                   WHERE id=?""",
                (stats["total"], total_imported, job_id),
            )

        return jsonify({
            "job_id": job_id,
            "status": "done",
            "stats": {
                "total_messages": stats["total"],
                "my_messages": stats["mine"],
                "other_messages": stats["others"],
                "style_samples_imported": style_count,
                "semantic_memories_extracted": semantic_count,
                "total_imported": total_imported,
                "inferred_my_names": parsed.get("inferred_my_names", []),
                "date_range": stats["date_range"],
                "unique_senders": stats["unique_senders"],
            },
            "persona_rebuilt": persona_rebuilt,
            "semantic_error": semantic_error,
        })

    except Exception as e:
        with get_db() as conn:
            conn.execute(
                "UPDATE ingest_jobs SET status='failed', error_msg=? WHERE id=?",
                (str(e)[:500], job_id),
            )
        return jsonify({"error": f"导入失败: {str(e)}"}), 500


def _extract_semantic_memories(messages: list[dict], avatar_id: str) -> list[dict]:
    """
    用 LLM 从对话中提取结构化记忆。

    提取内容：
    - 事实信息（工作、居住地、习惯、经历等）
    - 个人观点和态度
    - 说话方式特征
    - 与对方的共同经历

    返回适合存入 memories 表的 items 列表。
    """
    from backend.services.llm_provider import llm

    # 取前 3000 字的对话用于分析（避免 token 超限）
    conversation_text = format_conversation_for_llm(messages, max_chars=3000)
    if not conversation_text:
        return []

    prompt = f"""以下是一段真实的聊天记录，其中「【我】」是我们要分析的人。
请从这段对话中提取关于「我」的结构化信息，用于构建AI数字替身的记忆库。

聊天记录：
{conversation_text}

请提取以下几类信息（每类最多5条，只提取有明确依据的，不要推断或编造）：

1. 事实信息：工作、城市、生活习惯、家庭情况等客观事实
2. 个人观点：对某些事情的看法、态度、喜好厌恶
3. 说话风格：常用词语、表达习惯、语气特点
4. 共同经历：与对方提到的共同回忆、约定、事件

输出 JSON 格式：
{{
  "facts": ["事实1", "事实2"],
  "opinions": ["观点1", "观点2"],
  "style": ["风格特点1", "风格特点2"],
  "episodes": ["经历1", "经历2"]
}}

如果某类没有明确信息，返回空数组。只输出 JSON，不要其他文字。"""

    raw = llm.chat(
        system_prompt="你是一个信息提取专家，从对话中提取关于某人的结构化信息。只输出 JSON。",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=600,
        temperature=0.2,
    ).strip()

    # 提取 JSON
    import re
    json_match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not json_match:
        return []

    data = json.loads(json_match.group())

    items = []
    for content in data.get("facts", []):
        if content.strip():
            items.append({"content": content.strip(), "mem_type": "episode",
                          "source": "llm_extracted", "topic_tags": ["事实"]})
    for content in data.get("opinions", []):
        if content.strip():
            items.append({"content": content.strip(), "mem_type": "opinion",
                          "source": "llm_extracted", "topic_tags": ["观点"]})
    for content in data.get("style", []):
        if content.strip():
            items.append({"content": content.strip(), "mem_type": "style",
                          "source": "llm_extracted", "topic_tags": ["风格"]})
    for content in data.get("episodes", []):
        if content.strip():
            items.append({"content": content.strip(), "mem_type": "episode",
                          "source": "llm_extracted", "topic_tags": ["经历"]})

    return items
