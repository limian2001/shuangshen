from __future__ import annotations
"""
敏感话题处理模块 v0.3

架构调整：
  - 安全词（转账/借钱等）仍是硬拦截，直接返回，零漏报
  - 用户预设话题从"拦截器"改为"提示生成器"：
    命中后把预设信息作为 hint 注入 system prompt，由 LLM 决定最终回复
    这样 LLM 可以结合对话情绪、上下文灵活应对，而不是死板照搬预设答案
"""
import json
import re
from typing import Optional

from backend.db.database import get_db, rows_to_list
from backend.utils.auth import new_id


# 固定拦截的危险话题 —— 精确匹配，直接硬拦（金融欺诈/诈骗防护）
ALWAYS_BLOCK_KEYWORDS = [
    "转账", "打款", "借钱", "借我", "汇款", "红包", "付款", "还钱",
    "账号密码", "银行卡", "验证码",
]
ALWAYS_BLOCK_REPLY = "这个我没办法帮你，你直接联系本人吧。"


class SafetyBlockResult:
    """安全硬拦截结果"""
    def __init__(self, blocked: bool, reply: str = ""):
        self.blocked = blocked
        self.reply = reply


def is_safety_blocked(user_message: str) -> SafetyBlockResult:
    """
    第一道防线：安全关键词精确匹配。
    命中则立即硬拦，不走任何 LLM。
    """
    for kw in ALWAYS_BLOCK_KEYWORDS:
        if kw in user_message:
            return SafetyBlockResult(blocked=True, reply=ALWAYS_BLOCK_REPLY)
    return SafetyBlockResult(blocked=False)


def get_topic_hints(avatar_id: str, user_message: str) -> str:
    """
    检查消息是否命中预设话题，命中则返回提示字符串注入 system prompt。
    不再直接返回答案，而是把预设信息交给 LLM 灵活处理。

    Returns:
        命中时返回提示字符串，未命中返回空字符串
    """
    with get_db() as conn:
        # 查全局（avatar_id IS NULL）+ 当前替身专属
        topics = rows_to_list(conn.execute(
            """SELECT * FROM sensitive_topics
               WHERE avatar_id = ? OR avatar_id IS NULL
               ORDER BY avatar_id NULLS LAST""",
            (avatar_id,),
        ).fetchall())

    from backend.services.trace import trace_event

    if not topics:
        trace_event("topic", {"matched": None, "candidates": 0})
        return ""

    # 先关键词快速匹配
    for topic in topics:
        keywords = json.loads(topic.get("trigger_keywords", "[]"))
        if _matches_any(user_message, keywords):
            trace_event("topic", {
                "matched": topic.get("topic_description") or "、".join(keywords[:3]),
                "via": "keyword",
                "strategy": topic.get("strategy", ""),
                "candidates": len(topics),
            })
            return _build_hint(topic)

    # 再 LLM 语义匹配
    hint = _llm_semantic_hint(topics, user_message)
    trace_event("topic", {
        "matched": (hint[:60] if hint else None),
        "via": ("llm_semantic" if hint else None),
        "candidates": len(topics),
    })
    return hint


def _build_hint(topic: dict) -> str:
    """把命中的话题转成注入 system prompt 的提示字符串"""
    strategy = topic.get("strategy", "deflect")
    preset = topic.get("preset_content", "")
    desc = topic.get("topic_description") or "、".join(
        json.loads(topic.get("trigger_keywords", "[]"))
    )

    if strategy == "preset_answer" and preset:
        return (
            f"\n【预设信息参考】用户问到了「{desc}」相关的问题。"
            f"关于这个问题，有一个参考答案：「{preset}」。"
            "请结合当前对话语境和对方的情绪，自然地回应——"
            "可以用预设答案作为事实基础，但要根据对方的实际问法和情绪状态灵活表达，"
            "不要生硬照搬，更不要重复之前说过的完全相同的话。"
        )
    elif strategy == "deflect":
        return (
            f"\n【预设信息参考】用户问到了「{desc}」相关的问题，"
            "这是你不太方便详细说的话题，请自然地转移话题或给一个模糊的回应。"
        )
    else:  # admit_unknown
        return (
            f"\n【预设信息参考】用户问到了「{desc}」相关的问题，"
            "你对此不太了解，请诚实地说不太清楚，建议对方直接问本人。"
        )


def _llm_semantic_hint(topics: list[dict], user_message: str) -> str:
    """LLM 语义匹配，返回提示字符串或空字符串"""
    try:
        from backend.services.llm_provider import llm

        topics_desc = []
        for i, t in enumerate(topics):
            desc = t.get("topic_description") or "、".join(
                json.loads(t.get("trigger_keywords", "[]"))
            )
            preset = t.get("preset_content", "")
            strategy = t.get("strategy", "deflect")
            topics_desc.append(
                f"话题{i+1}：{desc} | 策略：{strategy}"
                + (f" | 参考答案：{preset}" if preset else "")
            )

        topics_text = "\n".join(topics_desc)
        prompt = (
            "判断用户消息是否语义上属于以下预设话题之一（语义相关即算，不要求措辞一致）。\n\n"
            f"预设话题：\n{topics_text}\n\n"
            f"用户消息：「{user_message}」\n\n"
            '只输出 JSON：{"matched": true, "topic_index": 1} 或 {"matched": false}'
        )

        raw = llm.chat(
            system_prompt="你是话题分类器，只输出 JSON。",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=30,
            temperature=0.1,
        ).strip()

        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            return ""

        result = json.loads(json_match.group())
        if not result.get("matched"):
            return ""

        idx = max(0, min(result.get("topic_index", 1) - 1, len(topics) - 1))
        return _build_hint(topics[idx])

    except Exception as e:
        print(f"[TOPIC_GUARD] 语义匹配失败，不注入提示: {e}")
        return ""


def _matches_any(text: str, keywords: list[str]) -> bool:
    text_clean = text.lower().replace(" ", "").replace("　", "")
    for kw in keywords:
        kw_clean = kw.lower().strip().replace(" ", "")
        if kw_clean and kw_clean in text_clean:
            return True
    return False


# ─────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────

def add_topic(
    avatar_id: Optional[str],
    trigger_keywords: list[str],
    strategy: str,
    preset_content: str = "",
    topic_description: str = "",
) -> dict:
    """
    添加敏感话题预设。
    avatar_id=None 表示全局话题，适用于所有替身。
    """
    tid = new_id()
    kw_json = json.dumps(trigger_keywords, ensure_ascii=False)

    if not topic_description:
        topic_description = _generate_topic_description(trigger_keywords, preset_content)

    with get_db() as conn:
        conn.execute(
            """INSERT INTO sensitive_topics
               (id, avatar_id, trigger_keywords, topic_description, strategy, preset_content)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (tid, avatar_id, kw_json, topic_description, strategy, preset_content),
        )
    return {
        "id": tid,
        "avatar_id": avatar_id,
        "trigger_keywords": trigger_keywords,
        "topic_description": topic_description,
        "strategy": strategy,
        "preset_content": preset_content,
    }


def _generate_topic_description(keywords: list[str], preset_content: str) -> str:
    try:
        from backend.services.llm_provider import llm
        prompt = (
            f"用户为AI替身设置了一个敏感话题，关键词：{keywords}，"
            f"预设回答：「{preset_content}」。\n"
            "请用一句话（15字以内）描述这个话题的语义范围，覆盖所有同义问法。"
            "只输出描述本身。示例：关于薪资收入工资待遇的问题"
        )
        desc = llm.chat(
            system_prompt="你是话题意图描述生成器，输出简洁准确。",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=40,
            temperature=0.2,
        ).strip()
        return desc if desc else "、".join(keywords)
    except Exception:
        return "、".join(keywords)


def list_topics(avatar_id: Optional[str] = None) -> list[dict]:
    """
    查询话题列表。
    avatar_id=None 查全局话题；传入 avatar_id 查该替身专属话题。
    """
    with get_db() as conn:
        if avatar_id is None:
            rows = rows_to_list(conn.execute(
                "SELECT * FROM sensitive_topics WHERE avatar_id IS NULL ORDER BY created_at",
            ).fetchall())
        else:
            rows = rows_to_list(conn.execute(
                "SELECT * FROM sensitive_topics WHERE avatar_id = ? ORDER BY created_at",
                (avatar_id,),
            ).fetchall())
    for r in rows:
        r["trigger_keywords"] = json.loads(r.get("trigger_keywords", "[]"))
    return rows


def delete_topic(topic_id: str, avatar_id: Optional[str] = None) -> bool:
    with get_db() as conn:
        if avatar_id is None:
            cur = conn.execute(
                "DELETE FROM sensitive_topics WHERE id = ? AND avatar_id IS NULL",
                (topic_id,),
            )
        else:
            cur = conn.execute(
                "DELETE FROM sensitive_topics WHERE id = ? AND avatar_id = ?",
                (topic_id, avatar_id),
            )
    return cur.rowcount > 0
