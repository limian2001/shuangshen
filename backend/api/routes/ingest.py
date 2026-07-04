"""
数据喂入路由 v0.5

两个入口：
  POST /api/ingest/:avatar_id/wechat   — 上传 .txt / .html 文件（多文件）
  POST /api/ingest/:avatar_id/paste    — 直接粘贴文本（最简单）

导入流程 v0.5：
  1. 解析聊天记录（多格式）
  2. 原始文本保存到 raw_uploads（SHA256 去重，永久存档）
  3. 风格样本存入 memories（wechat 类型，供 persona_prompt 重建用）
  4. LLM 生成风格档案（≤300字）→ 存 avatars.style_profile
  5. LLM 语义解析 → episode/opinion → 存 memories（priority=0）
  6. rebuild_persona_prompt → 存 avatars.persona_prompt
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
    parse_wechat_txt, parse_text, parse_wechat_html,
    extract_style_samples, format_conversation_for_llm, calculate_reply_stats,
)
from backend.services.memory_store import add_memories_batch, sync_to_chroma
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

    return _do_import(
        avatar_id, text, my_names, source="paste",
        raw_content=text, raw_filename="paste_import", raw_file_type="paste",
    )


@ingest_bp.post("/<avatar_id>/wechat")
@require_auth
def upload_wechat(avatar_id):
    """
    上传微信聊天记录文件（支持多文件批量上传）。

    Form-data:
      file:     一个或多个 .txt / .html 文件（file 字段重复发送）
      my_names: 识别「我」的名称，逗号分隔（可选）
    """
    avatar = _check_creator(avatar_id)
    if not avatar:
        return jsonify({"error": "替身不存在或无权限"}), 403

    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "未上传文件", "guide": WECHAT_EXPORT_GUIDE}), 400

    my_names_raw = request.form.get("my_names", "")
    my_names = [n.strip() for n in my_names_raw.split(",") if n.strip()] or None

    parsed_results: List[dict] = []
    errors: List[str] = []

    for f in files:
        fname = f.filename.lower()
        raw = f.read()

        text = _decode_bytes(raw)
        if text is None:
            errors.append(f"{f.filename}: 无法解码（请使用 UTF-8 或 GBK）")
            continue

        # 保存每个文件原始内容（SHA256 去重）
        ftype = "html" if (fname.endswith(".html") or fname.endswith(".htm")) else "txt"
        _save_raw_upload(avatar_id, g.user_id, f.filename, ftype, text)

        try:
            if fname.endswith(".html") or fname.endswith(".htm"):
                result = parse_wechat_html(text, my_names)
            else:
                result = parse_text(text, my_names)
            parsed_results.append(result)
        except Exception as e:
            errors.append(f"{f.filename}: 解析失败 — {str(e)[:80]}")

    if not parsed_results:
        return jsonify({"error": "所有文件均解析失败", "details": errors}), 400

    # 多结果合并 + 去重
    merged = _merge_parsed(parsed_results, my_names)

    # 原始文件已逐个保存，_do_import 不需要再次保存
    return _do_import(
        avatar_id, "", my_names,
        source="wechat_file",
        pre_parsed=merged,
        file_errors=errors,
        raw_content=None,
    )


@ingest_bp.post("/<avatar_id>/detect")
@require_auth
def detect_senders(avatar_id):
    """
    解析聊天记录，返回发言人列表，不导入。
    供前端展示「请选择哪个是你」确认步骤。

    支持：
      JSON body {text}   — 粘贴文本
      form-data {file}   — 单个或多个 .txt / .html 文件（file 字段重复发送）
    """
    if not _check_creator(avatar_id):
        return jsonify({"error": "无权限"}), 403

    all_messages: List[dict] = []
    inferred: List[str] = []

    if request.is_json:
        text = (request.get_json() or {}).get("text", "").strip()
        if not text:
            return jsonify({"error": "text 不能为空"}), 400
        parsed = parse_text(text, my_names=None)
        all_messages = parsed["messages"]
        inferred = parsed.get("inferred_my_names", [])

    elif request.files.getlist("file"):
        files = request.files.getlist("file")
        for f in files:
            raw = f.read()
            fname = f.filename.lower()
            if fname.endswith(".html") or fname.endswith(".htm"):
                text = _decode_bytes(raw)
                if text is None:
                    continue
                parsed = parse_wechat_html(text, my_names=None)
            else:
                text = _decode_bytes(raw)
                if text is None:
                    continue
                parsed = parse_text(text, my_names=None)
            all_messages.extend(parsed["messages"])
            for n in parsed.get("inferred_my_names", []):
                if n not in inferred:
                    inferred.append(n)
        if not all_messages:
            return jsonify({"error": "文件无法解码或未识别到消息"}), 400
    else:
        return jsonify({"error": "需要提供 text 或 file"}), 400

    from collections import Counter
    counts = Counter(m["sender"] for m in all_messages)

    senders = [
        {"name": s, "count": counts[s], "is_me_guess": s in inferred}
        for s in sorted(counts, key=lambda x: -counts[x])
        if s and s not in ("unknown", "")
    ]

    return jsonify({
        "senders": senders,
        "total_messages": len(all_messages),
    })


@ingest_bp.post("/<avatar_id>/reparse")
@require_auth
def reparse_raw(avatar_id):
    """
    从 raw_uploads 重新解析该替身的所有原始记录。

    使用场景：
    - 首次导入后未正确设置发言人 → 带 my_names 重跑
    - 算法升级后想刷新 style_profile / avg_reply_chars
    - 无需重新上传文件

    Body JSON（可选）:
      my_names: 识别「我」的名称，逗号分隔
    """
    avatar = _check_creator(avatar_id)
    if not avatar:
        return jsonify({"error": "无权限"}), 403

    with get_db() as conn:
        raws = rows_to_list(conn.execute(
            """SELECT filename, file_type, content FROM raw_uploads
               WHERE avatar_id = ? ORDER BY created_at""",
            (avatar_id,)
        ).fetchall())

    if not raws:
        return jsonify({"error": "没有原始记录，请先上传聊天文件"}), 404

    data = request.get_json(silent=True) or {}
    my_names_raw = data.get("my_names", "")
    my_names = [n.strip() for n in my_names_raw.split(",") if n.strip()] or None

    parsed_results: List[dict] = []
    errors: List[str] = []
    for raw in raws:
        content = raw.get("content") or ""
        fname = raw.get("filename") or ""
        if not content:
            continue
        try:
            if raw.get("file_type") == "html":
                result = parse_wechat_html(content, my_names)
            else:
                result = parse_text(content, my_names)
            parsed_results.append(result)
        except Exception as e:
            errors.append(f"{fname}: {str(e)[:80]}")

    if not parsed_results:
        return jsonify({"error": "所有记录重新解析均失败", "details": errors}), 400

    merged = _merge_parsed(parsed_results, my_names)

    # raw_uploads 已存在，不重复保存；计算字段（style_profile/avg_reply_chars）会被覆盖更新
    resp = _do_import(
        avatar_id, "", my_names,
        source="reparse",
        pre_parsed=merged,
        file_errors=errors,
        raw_content=None,
    )

    # 重新解析后将 SQLite 里所有 episode/opinion 记忆同步到 Chroma
    # （_do_import 内部的 add_memories_batch 会跳过已存在内容，所以必须这里单独 sync）
    try:
        sync_result = sync_to_chroma(avatar_id)
        print(f"[REPARSE] Chroma 同步完成: {sync_result}")
    except Exception as e:
        print(f"[REPARSE] Chroma 同步失败（不影响其他结果）: {e}")

    # 更新 processed_at 标记
    with get_db() as conn:
        conn.execute(
            "UPDATE raw_uploads SET processed_at=datetime('now'), process_version='v0.6' WHERE avatar_id=?",
            (avatar_id,)
        )
    return resp


@ingest_bp.get("/<avatar_id>/raw_count")
@require_auth
def raw_count(avatar_id):
    """返回已存储的原始记录数量（供前端判断是否显示重新解析按钮）"""
    if not _check_creator(avatar_id):
        return jsonify({"error": "无权限"}), 403
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt, SUM(char_count) as total_chars FROM raw_uploads WHERE avatar_id=?",
            (avatar_id,)
        ).fetchone()
    return jsonify({"count": row["cnt"] or 0, "total_chars": row["total_chars"] or 0})


@ingest_bp.get("/<avatar_id>/jobs")
@require_auth
def list_jobs(avatar_id):
    """查看导入任务历史"""
    if not _check_creator(avatar_id):
        return jsonify({"error": "无权限"}), 403

    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT id, filename, status, total_msgs, imported,
                      date_start, date_end, error_msg, created_at, finished_at
               FROM ingest_jobs WHERE avatar_id = ? ORDER BY created_at DESC LIMIT 20""",
            (avatar_id,),
        ).fetchall())
    return jsonify(rows)


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _decode_bytes(raw: bytes) -> Optional[str]:
    """尝试多种编码解码字节流，返回字符串或 None"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return None


def _save_raw_upload(
    avatar_id: str,
    creator_id: str,
    filename: str,
    file_type: str,
    content: str,
) -> Optional[str]:
    """
    保存原始上传内容到 raw_uploads 表。
    同一替身下内容相同（content_hash 相同）则跳过，返回 None。
    """
    import hashlib
    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    try:
        with get_db() as conn:
            exists = conn.execute(
                "SELECT id FROM raw_uploads WHERE avatar_id = ? AND content_hash = ?",
                (avatar_id, content_hash),
            ).fetchone()
            if exists:
                return None  # 重复内容，跳过
            uid = new_id()
            conn.execute(
                """INSERT INTO raw_uploads
                   (id, avatar_id, creator_id, filename, file_type, content,
                    char_count, content_hash, process_version, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'v0.5', datetime('now'))""",
                (uid, avatar_id, creator_id, filename, file_type, content, len(content), content_hash),
            )
            return uid
    except Exception as e:
        print(f"[INGEST] 原始数据存储失败（不影响导入）: {e}")
        return None


def _generate_style_profile(style_samples: List[str], avatar: dict) -> str:
    """
    从风格样本中提取说话习惯，生成 ≤STYLE_PROFILE_MAX_CHARS 字的风格档案。
    用于每次对话注入 System Prompt，不参与 RAG。
    """
    from backend.services.llm_provider import llm
    if not style_samples:
        return ""
    max_chars = config.STYLE_PROFILE_MAX_CHARS
    samples_text = "\n".join(f"- {s}" for s in style_samples[:30])
    prompt = f"""以下是一个人的真实聊天样本，请提取他/她的说话风格特征，供 AI 替身模拟使用：

【聊天样本（部分）】
{samples_text}

请简洁总结（直接输出，不要序号和标题，不超过 {max_chars} 字）：
• 称呼对方的习惯（如「宝宝」「老公」「妈」「你小子」）
• 常用语气词、口头禅（如「哈哈」「嗯嗯」「好滴」「啊对」）
• 消息长度习惯：典型的一条消息大约几个字（例如「平均5-15字」「通常5字内，偶尔长句」），这是最重要的特征之一，必须给出具体字数区间
• 说话节奏（简短/连发多条/习惯用句尾语气词等）
• 表情符号使用习惯（多/少/偏好哪类）
• 其他明显语言特征

只输出特征描述，不超过 {max_chars} 字。"""
    try:
        result = llm.chat(
            system_prompt="你是语言风格分析专家，从真实聊天样本中提取说话特征，输出简洁准确。",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=450,
            temperature=0.2,
        ).strip()
        return result[:max_chars]
    except Exception as e:
        print(f"[INGEST] 风格档案生成失败（不影响导入）: {e}")
        return ""


def _select_conversation_samples(all_messages: List[dict], avatar: dict) -> List[dict]:
    """
    从聊天记录里用 LLM 挑选 10-15 组有代表性的真实来回，供 few-shot 注入 System Prompt。

    返回 [{"q": "对方说的话", "a": "他的回复"}, ...] 形式的 JSON 列表。
    注重覆盖：闲聊/情绪倾诉/玩笑/日常询问/关心 等不同场景。
    """
    from backend.services.llm_provider import llm

    # 把消息整理成来回对（连续的 not_me → me 为一对）
    pairs: List[dict] = []
    i = 0
    msgs = all_messages
    while i < len(msgs) - 1:
        if not msgs[i].get("is_from_me") and msgs[i + 1].get("is_from_me"):
            q = msgs[i].get("content", "").strip()
            a = msgs[i + 1].get("content", "").strip()
            if 2 < len(q) < 80 and 2 < len(a) < 100 and not q.startswith("[") and not a.startswith("["):
                pairs.append({"q": q, "a": a})
        i += 1

    if len(pairs) < 3:
        return []

    # 最多给 LLM 看 80 对，让它从中挑有代表性的
    import random
    sample_pool = random.sample(pairs, min(80, len(pairs)))
    pool_text = "\n".join(f"{idx+1}. 对方：{p['q']}\n   他：{p['a']}" for idx, p in enumerate(sample_pool))

    rel = avatar.get("relationship", "friend")
    prompt = f"""以下是两个人的真实聊天片段（关系：{rel}），其中「他」是我们要模拟的人。

{pool_text}

请从中挑选 10-12 组最能体现「他」说话风格的对话，要求：
- 覆盖不同场景：闲聊/情绪倾诉/玩笑调侃/日常关心/随口一问 等
- 优先选「他」回复最有个人特色、最真实的片段
- 避免重复相似内容
- 只选原文，不要修改

输出 JSON 数组，格式：
[{{"q": "对方原话", "a": "他的原话"}}, ...]

只输出 JSON，不要其他文字。"""

    try:
        raw = llm.chat(
            system_prompt="你是对话样本筛选专家，从真实聊天记录中挑选最有代表性的片段。只输出 JSON。",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.2,
        ).strip()
        import re
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            return []
        samples = json.loads(json_match.group())
        # 验证格式
        valid = [s for s in samples if isinstance(s, dict) and "q" in s and "a" in s]
        return valid[:15]
    except Exception as e:
        print(f"[INGEST] 对话样本选取失败（不影响导入）: {e}")
        return []


def _generate_style_features(style_samples: List[str], avatar: dict) -> dict:
    """
    从真实聊天样本中提取四类深层风格特征，供 System Prompt 结构化注入。

    返回 dict：
    {
      "emotional_expressions": {"happy": [...], "unhappy": [...], "comforting": [...], "sad": [...]},
      "deflection_patterns": "描述转移/拒绝话题的方式",
      "followup_habit": "描述追问/反问习惯",
      "signature_phrases": ["口头禅1", "口头禅2", ...]
    }
    强调在这两个人的具体对话关系里提取，而非通用人格描述。
    """
    from backend.services.llm_provider import llm

    if not style_samples:
        return {}

    rel = avatar.get("relationship", "friend")
    samples_text = "\n".join(f"- {s}" for s in style_samples[:40])

    prompt = f"""以下是两个人真实聊天记录中「他/她」发出的消息（关系：{rel}）。
请分析在这段具体的对话关系里，这个人的深层说话特征。

【他/她的消息样本】
{samples_text}

请提取以下四类特征，输出 JSON（只输出 JSON，不要其他文字）：

{{
  "emotional_expressions": {{
    "happy": ["他高兴/开心时常说的词或句式，列3-5个例子"],
    "unhappy": ["他不满/吐槽时常说的词或句式"],
    "comforting": ["他安慰对方时常说的话"],
    "sad": ["他失落/难过时常说的话，如果样本中没有则返回空数组"]
  }},
  "deflection_patterns": "当他不想聊某话题或想结束某个方向时，他通常怎么做？（举具体例子）",
  "followup_habit": "他聊天时会主动追问/反问吗？频率如何？常用什么句式？（具体描述）",
  "signature_phrases": ["他最有个人特色的口头禅或惯用句式，列3-6个"]
}}

注意：严格基于样本中出现的内容，不要推断或编造。如果某类证据不足，直接返回空字符串或空数组。"""

    try:
        raw = llm.chat(
            system_prompt="你是语言特征分析专家，从真实聊天样本中精确提取说话模式。只输出 JSON。",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.2,
        ).strip()
        import re
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            return {}
        return json.loads(json_match.group())
    except Exception as e:
        print(f"[INGEST] 风格特征提取失败（不影响导入）: {e}")
        return {}


def _extract_reaction_patterns(
    all_messages: List[dict],
    avatar_id: str,
    avatar: dict,
    date_range: Optional[dict] = None,
) -> List[dict]:
    """
    从对话中提取情境反应模式（reaction 类型记忆）。

    分析在这段具体关系里，当对方处于某种情境时，「他」的典型反应是什么。
    每条 reaction 格式：「场景描述 → 他的典型反应（含具体用词）」
    存为 mem_type=reaction，进入 SQLite + Chroma。
    """
    from backend.services.llm_provider import llm

    max_chars = config.EXTRACT_MAX_CHARS or 4000
    conversation_text = format_conversation_for_llm(all_messages, max_chars=max_chars)
    if not conversation_text:
        return []

    rel = avatar.get("relationship", "friend")
    prompt = f"""以下是两个人的真实聊天记录（关系：{rel}），其中「【我】」是被分析的人。

{conversation_text}

请分析在这段具体的对话关系里，当对方处于不同情境时，「【我】」的典型反应模式。

提取 5-8 个情境反应，格式：「场景描述 → 他的典型反应（含具体说法）」

关注这几类场景（有样本才写，没有就跳过）：
- 对方诉苦/抱怨时
- 对方分享好消息时
- 对方问「你在干嘛」「最近咋样」等闲聊时
- 对方生气/情绪不好时
- 对方提出某个请求时
- 话题冷场/不知道说什么时
- 对方开玩笑/调侃时
- 聊到某类特定话题（如工作/钱/感情）时

输出 JSON 数组：
[
  {{"scene": "对方诉苦或抱怨时", "reaction": "通常先用「嗯嗯」「哎」接情绪，再问「咋了」，很少直接给建议"}},
  ...
]

只输出 JSON，不要其他文字。严格基于聊天记录，不要编造。"""

    try:
        raw = llm.chat(
            system_prompt="你是人际互动模式分析专家，从真实对话中提取一个人在特定情境下的反应规律。只输出 JSON。",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            temperature=0.2,
        ).strip()
        import re
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            return []
        patterns = json.loads(json_match.group())

        chat_date = (date_range or {}).get("start") or None
        items = []
        for p in patterns:
            if not isinstance(p, dict):
                continue
            scene = (p.get("scene") or "").strip()
            reaction = (p.get("reaction") or "").strip()
            if scene and reaction:
                items.append({
                    "content": f"【{scene}】{reaction}",
                    "mem_type": "reaction",
                    "source": "llm_extracted",
                    "topic_tags": ["反应模式"],
                    "priority": 0,
                    "chat_date": chat_date,
                })
        return items
    except Exception as e:
        print(f"[INGEST] 反应模式提取失败（不影响导入）: {e}")
        return []


def _merge_parsed(results: List[dict], my_names: Optional[List[str]] = None) -> dict:
    """
    合并多个 parse_text / parse_wechat_html 的返回结果。
    按 (timestamp, sender, content) 三元组对消息去重（支持批次间重叠）。
    """
    all_msgs: List[dict] = []
    inferred: List[str] = list(my_names or [])

    for r in results:
        all_msgs.extend(r.get("all_messages", r.get("messages", [])))
        for n in r.get("inferred_my_names", []):
            if n not in inferred:
                inferred.append(n)

    # 消息级去重
    seen: set = set()
    deduped: List[dict] = []
    dup_count = 0
    for msg in all_msgs:
        key = (
            msg.get("timestamp", ""),
            msg.get("sender", ""),
            msg.get("content", ""),
        )
        if key in seen:
            dup_count += 1
        else:
            seen.add(key)
            deduped.append(msg)

    my_messages = [m for m in deduped if m.get("is_from_me")]
    timestamps = [m.get("timestamp", "") for m in deduped if m.get("timestamp")]

    return {
        "messages": deduped,
        "my_messages": my_messages,
        "all_messages": deduped,
        "inferred_my_names": inferred,
        "_msg_duplicates": dup_count,
        "stats": {
            "total": len(deduped),
            "mine": len(my_messages),
            "others": len(deduped) - len(my_messages),
            "date_range": {
                "start": timestamps[0] if timestamps else None,
                "end": timestamps[-1] if timestamps else None,
            },
            "unique_senders": list({m.get("sender", "") for m in deduped}),
        },
    }


# ─────────────────────────────────────────────
# 核心导入逻辑
# ─────────────────────────────────────────────

def _do_import(
    avatar_id: str,
    text: str,
    my_names: Optional[List[str]],
    source: str = "paste",
    pre_parsed: dict = None,
    file_errors: Optional[List[str]] = None,
    raw_content: Optional[str] = None,       # 原始文本（用于 raw_uploads 存储）
    raw_filename: str = "",
    raw_file_type: str = "paste",
) -> object:
    """统一的导入处理逻辑 v0.10"""
    from flask import g

    job_id = new_id()
    creator_id = getattr(g, "user_id", "")
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

        # ② 保存原始文本到 raw_uploads（幂等，重复内容自动跳过）
        raw_text = raw_content or text
        if raw_text and creator_id:
            _save_raw_upload(avatar_id, creator_id, raw_filename, raw_file_type, raw_text)

        # ③ 回复字数统计 → 存 avatars.avg_reply_chars（v0.5）
        reply_stats = calculate_reply_stats(my_messages)
        if reply_stats["avg_chars"] > 0 and reply_stats["sample_count"] >= 10:
            with get_db() as conn:
                conn.execute(
                    "UPDATE avatars SET avg_reply_chars=? WHERE id=?",
                    (reply_stats["avg_chars"], avatar_id),
                )

        # ④ 风格样本存入 memories（wechat 类型，供 persona_prompt 重建用，不参与 RAG）
        #    直接迭代 my_messages 以保留原始聊天时间戳（chat_date）
        style_items = []
        _seen_content: set = set()
        for _msg in my_messages:
            _c = _msg.get("content", "").strip()
            if (len(_c) > 5
                    and not _c.startswith("[")
                    and not _c.startswith("<")
                    and _c not in _seen_content
                    and len(style_items) < 200):
                _seen_content.add(_c)
                style_items.append({
                    "content": _c,
                    "mem_type": "wechat",
                    "source": source,
                    "topic_tags": [],
                    "priority": 0,
                    "chat_date": _msg.get("timestamp") or None,
                })
        style_samples = [item["content"] for item in style_items]  # 仅供风格档案生成用
        style_r = add_memories_batch(avatar_id, style_items)
        style_count = style_r["added"]
        style_skipped = style_r["skipped"]

        # ④ LLM 生成三类风格数据 → 存 avatars（v0.10）
        style_profile_updated = False
        with get_db() as conn:
            avatar_row = conn.execute(
                "SELECT * FROM avatars WHERE id = ?", (avatar_id,)
            ).fetchone()
        avatar_data = row_to_dict(avatar_row) if avatar_row else {}

        try:
            new_profile = _generate_style_profile(style_samples, avatar_data)
            new_features = _generate_style_features(style_samples, avatar_data)
            new_samples = _select_conversation_samples(all_messages, avatar_data)
            update_fields: dict = {}
            if new_profile:
                update_fields["style_profile"] = new_profile
            if new_features:
                update_fields["style_features"] = json.dumps(new_features, ensure_ascii=False)
            if new_samples:
                update_fields["conversation_samples"] = json.dumps(new_samples, ensure_ascii=False)
            if update_fields:
                set_clause = ", ".join(f"{k}=?" for k in update_fields)
                with get_db() as conn:
                    conn.execute(
                        f"UPDATE avatars SET {set_clause}, updated_at=datetime('now') WHERE id=?",
                        (*update_fields.values(), avatar_id),
                    )
                style_profile_updated = True
        except Exception as e:
            print(f"[INGEST] 风格数据生成失败（不影响导入）: {e}")

        # ⑤ LLM 语义解析 → episode/opinion → memories（priority=0）
        semantic_count = 0
        semantic_skipped = 0
        semantic_error = None
        try:
            semantic_memories = _extract_semantic_memories(
                all_messages, avatar_id, date_range=stats["date_range"]
            )
            if semantic_memories:
                sem_r = add_memories_batch(avatar_id, semantic_memories)
                semantic_count = sem_r["added"]
                semantic_skipped = sem_r["skipped"]
        except Exception as e:
            semantic_error = str(e)
            print(f"[INGEST] LLM 语义解析失败（不影响导入）: {e}")

        # ⑤b LLM 提取 reaction 反应模式记忆（v0.10）
        reaction_count = 0
        try:
            reaction_memories = _extract_reaction_patterns(
                all_messages, avatar_id, avatar_data, date_range=stats["date_range"]
            )
            if reaction_memories:
                reac_r = add_memories_batch(avatar_id, reaction_memories)
                reaction_count = reac_r["added"]
        except Exception as e:
            print(f"[INGEST] 反应模式提取失败（不影响导入）: {e}")

        # ⑥ 触发人格 Prompt 重建
        try:
            rebuild_persona_prompt(avatar_id)
            persona_rebuilt = True
        except Exception as e:
            persona_rebuilt = False
            print(f"[INGEST] 人格重建失败: {e}")

        total_imported = style_count + semantic_count
        msg_dup = parsed.get("_msg_duplicates", 0)
        date_start = stats["date_range"].get("start") or ""
        date_end = stats["date_range"].get("end") or ""
        with get_db() as conn:
            conn.execute(
                """UPDATE ingest_jobs
                   SET status='done', total_msgs=?, imported=?,
                       date_start=?, date_end=?,
                       finished_at=datetime('now')
                   WHERE id=?""",
                (stats["total"], total_imported, date_start, date_end, job_id),
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
                "duplicates_skipped": style_skipped + semantic_skipped + msg_dup,
                "inferred_my_names": parsed.get("inferred_my_names", []),
                "date_range": stats["date_range"],
                "unique_senders": stats["unique_senders"],
            },
            "persona_rebuilt": persona_rebuilt,
            "style_profile_updated": style_profile_updated,
            "semantic_error": semantic_error,
            "file_errors": file_errors or [],
        })

    except Exception as e:
        with get_db() as conn:
            conn.execute(
                "UPDATE ingest_jobs SET status='failed', error_msg=? WHERE id=?",
                (str(e)[:500], job_id),
            )
        return jsonify({"error": f"导入失败: {str(e)}"}), 500


def _extract_semantic_memories(
    messages: List[dict],
    avatar_id: str,
    date_range: Optional[dict] = None,
) -> List[dict]:
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

    # 处理深度配置：EXTRACT_MAX_CHARS=None 则不限，否则截断输入（v0.5）
    max_chars = config.EXTRACT_MAX_CHARS or 3000
    conversation_text = format_conversation_for_llm(messages, max_chars=max_chars)
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

    # 用对话的起始日期作为语义记忆的 chat_date（代表这批记忆来自哪段对话）
    chat_date = (date_range or {}).get("start") or None

    items = []
    for content in data.get("facts", []):
        if content.strip():
            items.append({"content": content.strip(), "mem_type": "episode",
                          "source": "llm_extracted", "topic_tags": ["事实"],
                          "chat_date": chat_date})
    for content in data.get("opinions", []):
        if content.strip():
            items.append({"content": content.strip(), "mem_type": "opinion",
                          "source": "llm_extracted", "topic_tags": ["观点"],
                          "chat_date": chat_date})
    for content in data.get("style", []):
        if content.strip():
            items.append({"content": content.strip(), "mem_type": "style",
                          "source": "llm_extracted", "topic_tags": ["风格"],
                          "chat_date": chat_date})
    for content in data.get("episodes", []):
        if content.strip():
            items.append({"content": content.strip(), "mem_type": "episode",
                          "source": "llm_extracted", "topic_tags": ["经历"],
                          "chat_date": chat_date})

    return items
