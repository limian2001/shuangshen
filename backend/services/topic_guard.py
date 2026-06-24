from __future__ import annotations
"""
敏感话题拦截中间件 v0.2

两层拦截架构：
  第一层：安全关键词精确拦截（转账/借钱等金融欺诈词，零漏报）
  第二层：LLM 语义意图匹配（处理各种同义问法，一次调用同时完成匹配+生成回复）
"""
import json
import re

from backend.db.database import get_db, rows_to_list, row_to_dict
from backend.utils.auth import new_id


# 默认回避话题的兜底回复
DEFLECT_REPLIES = [
    "这个问题嘛，我得想想，下次再聊哈",
    "这个…现在不方便说，你先别问啦",
    "嗯，这个话题我们改天聊，好不好",
]

# 固定拦截的危险话题（无论是否预设，都拦截）—— 精确匹配，不走语义
ALWAYS_BLOCK_KEYWORDS = [
    "转账", "打款", "借钱", "借我", "汇款", "红包", "付款", "还钱",
    "账号密码", "银行卡", "验证码",
]
ALWAYS_BLOCK_REPLY = "这个我没办法帮你，你直接联系本人吧。"


class TopicGuardResult:
    def __init__(self, blocked: bool, reply: str = "", strategy: str = ""):
        self.blocked = blocked
        self.reply = reply
        self.strategy = strategy


def check_message(avatar_id: str, user_message: str) -> TopicGuardResult:
    """
    检测消息是否命中预设敏感话题或安全拦截词。

    Returns:
        TopicGuardResult(blocked=True, reply=...) 表示拦截
        TopicGuardResult(blocked=False) 表示放行给 LLM
    """
    # ── 第一层：安全词精确拦截（金融欺诈防护，必须零漏报）──
    for kw in ALWAYS_BLOCK_KEYWORDS:
        if kw in user_message:
            return TopicGuardResult(
                blocked=True,
                reply=ALWAYS_BLOCK_REPLY,
                strategy="safety_block",
            )

    # ── 第二层：自定义预设话题语义匹配 ──
    with get_db() as conn:
        topics = rows_to_list(conn.execute(
            "SELECT * FROM sensitive_topics WHERE avatar_id = ?", (avatar_id,)
        ).fetchall())

    if not topics:
        return TopicGuardResult(blocked=False)

    # 先尝试关键词快速匹配（命中则跳过 LLM，节省成本）
    for topic in topics:
        keywords = json.loads(topic.get("trigger_keywords", "[]"))
        if _matches_any(user_message, keywords):
            return _build_result(topic, user_message, matched_by="keyword")

    # 关键词未命中，调用 LLM 做语义意图匹配
    return _llm_intent_check(topics, user_message)


def _llm_intent_check(topics: list[dict], user_message: str) -> TopicGuardResult:
    """
    用 LLM 判断消息是否语义上命中某条预设话题。
    一次调用同时完成：意图匹配 + 生成语境化回复。
    """
    try:
        from backend.services.llm_provider import llm

        # 构建话题列表描述
        topics_desc = []
        for i, t in enumerate(topics):
            desc = t.get("topic_description") or "、".join(
                json.loads(t.get("trigger_keywords", "[]"))
            )
            preset = t.get("preset_content", "")
            strategy = t.get("strategy", "deflect")
            topics_desc.append(
                f"话题{i+1}：{desc} | 策略：{strategy}"
                + (f" | 预设答案：{preset}" if preset else "")
            )

        topics_text = "\n".join(topics_desc)

        prompt = f"""你是一个敏感话题识别助手。用户发了一条消息，请判断它是否语义上属于以下预设话题之一。

预设话题列表：
{topics_text}

用户消息：「{user_message}」

判断规则：
- 语义相关即算命中，不要求措辞完全一致
- 如果命中策略为"preset_answer"，根据预设答案内容，针对用户的具体问法生成一个自然口语化的回复（不要照抄预设，要根据问法调整表达）
- 如果命中策略为"deflect"，生成一个自然的回避回复
- 如果命中策略为"admit_unknown"，生成类似"这个我说不好，你直接问本人吧~"的回复
- 如果不属于任何预设话题，返回 matched: false

只输出 JSON，格式：
{{"matched": true, "topic_index": 1, "reply": "回复内容"}}
或
{{"matched": false}}"""

        raw = llm.chat(
            system_prompt="你是一个精确的话题分类器，只输出 JSON，不输出任何其他内容。",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.1,
        ).strip()

        # 提取 JSON（容错：LLM 可能带 markdown 代码块）
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            return TopicGuardResult(blocked=False)

        result = json.loads(json_match.group())
        if not result.get("matched"):
            return TopicGuardResult(blocked=False)

        idx = result.get("topic_index", 1) - 1
        idx = max(0, min(idx, len(topics) - 1))
        matched_topic = topics[idx]
        reply = result.get("reply") or _pick_deflect(user_message)
        return TopicGuardResult(
            blocked=True,
            reply=reply,
            strategy=matched_topic.get("strategy", "deflect") + "_semantic",
        )

    except Exception as e:
        # LLM 调用失败时放行（宁可放行，不阻断正常对话）
        print(f"[TOPIC_GUARD] LLM 语义匹配失败，放行: {e}")
        return TopicGuardResult(blocked=False)


def _build_result(topic: dict, user_message: str, matched_by: str = "keyword") -> TopicGuardResult:
    """根据已命中的话题构建拦截结果"""
    strategy = topic.get("strategy", "deflect")
    if strategy == "preset_answer":
        reply = topic.get("preset_content") or DEFLECT_REPLIES[0]
    elif strategy == "deflect":
        reply = _pick_deflect(user_message)
    else:  # admit_unknown
        reply = "这个我说不好，你直接问本人吧~"
    return TopicGuardResult(blocked=True, reply=reply, strategy=f"{strategy}_{matched_by}")


def _matches_any(text: str, keywords: list[str]) -> bool:
    """检测文本是否包含任意关键词（忽略大小写和全角空格）"""
    text_clean = text.lower().replace(" ", "").replace("　", "")
    for kw in keywords:
        kw_clean = kw.lower().strip().replace(" ", "")
        if kw_clean and kw_clean in text_clean:
            return True
    return False


def _pick_deflect(message: str) -> str:
    """根据消息内容选择合适的回避回复"""
    idx = hash(message[:10]) % len(DEFLECT_REPLIES)
    return DEFLECT_REPLIES[idx]


# ─────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────

def add_topic(
    avatar_id: str,
    trigger_keywords: list[str],
    strategy: str,
    preset_content: str = "",
    topic_description: str = "",
) -> dict:
    """添加敏感话题预设，自动生成语义描述供 LLM 匹配使用"""
    tid = new_id()
    kw_json = json.dumps(trigger_keywords, ensure_ascii=False)

    # 若未手动传入描述，调用 LLM 自动生成
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
        "trigger_keywords": trigger_keywords,
        "topic_description": topic_description,
        "strategy": strategy,
        "preset_content": preset_content,
    }


def _generate_topic_description(keywords: list[str], preset_content: str) -> str:
    """
    调用 LLM 把关键词 + 预设答案转成一句话语义描述。
    例：keywords=["月薪","工资"] preset="我月薪一万"
      → "关于薪资收入工资待遇的问题"
    失败时降级为关键词拼接。
    """
    try:
        from backend.services.llm_provider import llm
        prompt = (
            f"用户为AI替身设置了一个敏感话题，关键词：{keywords}，"
            f"预设回答：「{preset_content}」。\n"
            "请用一句话（15字以内）描述这个话题的语义范围，"
            "要覆盖所有同义问法。只输出描述本身，不要加任何前缀或标点以外的文字。\n"
            "示例输出：关于薪资收入工资待遇的问题"
        )
        desc = llm.chat(
            system_prompt="你是一个话题意图描述生成器，输出简洁准确。",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=40,
            temperature=0.2,
        ).strip()
        return desc if desc else "、".join(keywords)
    except Exception:
        return "、".join(keywords)


def list_topics(avatar_id: str) -> list[dict]:
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            "SELECT * FROM sensitive_topics WHERE avatar_id = ? ORDER BY created_at",
            (avatar_id,),
        ).fetchall())
    for r in rows:
        r["trigger_keywords"] = json.loads(r.get("trigger_keywords", "[]"))
    return rows


def delete_topic(topic_id: str, avatar_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM sensitive_topics WHERE id = ? AND avatar_id = ?",
            (topic_id, avatar_id),
        )
    return cur.rowcount > 0
