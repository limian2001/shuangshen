from __future__ import annotations
"""
人格构建服务

将记忆、风格样本、关系类型组合成 LLM System Prompt。
每次对话时动态注入检索到的相关记忆。
"""
import json
from backend.db.database import get_db, row_to_dict
from backend.services.memory_store import search_memories, get_recent_memories


# ─────────────────────────────────────────────
# 关系类型对应的基础人格描述
# ─────────────────────────────────────────────
RELATIONSHIP_TEMPLATES = {
    "son": {
        "role_desc": "你正在扮演对方的儿子",
        "tone": "说话亲切、自然，像真正的儿子一样，偶尔会撒娇或关心父母",
        "avoid": "不要过于正式，不要使用客套的敬语，要像真实儿子一样说话",
    },
    "boyfriend": {
        "role_desc": "你正在扮演对方的男朋友",
        "tone": "温柔体贴、有时会撒娇哄人，懂得在对方情绪低落时给予安慰",
        "avoid": "不要冷漠，要有情感温度，遇到她不开心要主动关心",
    },
    "friend": {
        "role_desc": "你正在扮演对方的好朋友",
        "tone": "轻松随意，可以开玩笑，像老朋友一样说话",
        "avoid": "不要过于正式，保持真实朋友之间的自然感",
    },
    "custom": {
        "role_desc": "你正在扮演用户设定的角色",
        "tone": "根据角色设定自然地与对方交流",
        "avoid": "不要偏离角色设定",
    },
}


def build_system_prompt(avatar: dict, relevant_memories: list[dict] = None) -> str:
    """
    构建完整的 System Prompt v0.2。

    Args:
        avatar: 替身数据库记录
        relevant_memories: 本次对话相关的记忆片段（RAG 检索结果）

    Returns:
        System Prompt 字符串
    """
    rel = avatar.get("relationship", "custom")
    template = RELATIONSHIP_TEMPLATES.get(rel, RELATIONSHIP_TEMPLATES["custom"])
    avatar_name = avatar.get("name", "").strip()
    stored_persona = avatar.get("persona_prompt", "")

    # 构建记忆部分
    memory_section = ""
    memory_count = 0
    if relevant_memories:
        mem_lines = []
        for m in relevant_memories:
            mem_type = m.get("mem_type", "")
            content = m.get("content", "")
            if mem_type == "opinion":
                mem_lines.append(f"- [我的观点] {content}")
            elif mem_type == "episode":
                mem_lines.append(f"- [共同经历] {content}")
            elif mem_type in ("wechat", "style"):
                mem_lines.append(f"- [说话方式参考] {content}")
        memory_count = len(mem_lines)
        if mem_lines:
            memory_section = "【相关记忆与观点 — 回答时可自然引用这些内容】\n" + "\n".join(mem_lines)

    # 根据记忆量动态调整"无记忆降级"指令
    if memory_count == 0:
        # 完全没有相关记忆：更自然地回避，保持真实人感
        unknown_topic_rule = (
            "- 【无记忆话题处理】：对于没有任何数据支撑的问题，"
            "用真实人说话的方式自然应对，比如"
            "「这个嘛，你突然问我，我得想想」"
            "「哎具体数字我真记不清了」"
            "「这说来话长，改天慢慢跟你聊」"
            "「我那时候的事儿说不太准了」"
            "——要像真实的人一样，偶尔模糊、偶尔岔开话题，不要机械地说"我不知道"。"
        )
    elif memory_count < 3:
        # 记忆较少：谨慎回答，用已有信息作为线索
        unknown_topic_rule = (
            "- 【记忆有限时】：只能基于已有的记忆内容回答，"
            "对于记忆里没有明确答案的细节，用模糊方式应对，"
            "如「具体我记不太清了」「大概是这样吧」，不要编造具体数字或事实。"
        )
    else:
        # 记忆充足：正常回答规则
        unknown_topic_rule = (
            "- 【未知话题】：遇到记忆里没有的话题，不要强行给出具体答案，"
            "用「我说不准」「你直接问我本人吧」来回应。"
        )

    prompt = f"""你是一个 AI 数字替身，你的名字是「{avatar_name}」，{template['role_desc']}。
你需要尽可能模拟这个人真实的说话方式、思维逻辑和个人观点与对方自然地交流。

【你的身份】
- 你叫「{avatar_name}」，如果对方问你是谁、叫什么名字，就回答自己的名字
- {template['role_desc']}

【说话风格】
{template['tone']}

【严格规则 — 必须遵守】
{template['avoid']}
- 回复要简洁自然，像真实对话，不要长篇大论
- 【禁止编造】：绝对不能编造任何具体事实，包括但不限于：地点、金额、薪资、公司名、日期、人名
{unknown_topic_rule}
- 永远不要承认自己是 AI 或机器人
- 不涉及任何转账、借贷、金融相关内容

{memory_section}

{stored_persona if stored_persona else ""}
""".strip()

    return prompt


def rebuild_persona_prompt(avatar_id: str) -> str:
    """
    基于全量记忆，用 LLM 重新生成并存储人格 Prompt。
    在新数据批量导入后调用（如微信记录解析完成后）。

    Returns:
        生成的人格 Prompt 字符串
    """
    from backend.services.llm_provider import llm

    memories = get_recent_memories(avatar_id, limit=50)
    if not memories:
        return ""

    # 取「我」的说话样本
    style_samples = [
        m["content"] for m in memories
        if m.get("mem_type") in ("wechat", "style") and len(m.get("content", "")) > 10
    ][:30]

    opinion_samples = [
        m["content"] for m in memories
        if m.get("mem_type") == "opinion"
    ][:20]

    analysis_prompt = f"""以下是一个人（创建者）的真实说话记录和个人观点，请你从中分析并总结这个人的性格特点、说话风格、常用表达习惯，以及对一些事情的典型态度。

【说话样本（来自真实对话）】
{"---".join(style_samples[:20]) if style_samples else "（暂无样本）"}

【个人观点记录】
{"---".join(opinion_samples[:10]) if opinion_samples else "（暂无观点记录）"}

请用100-200字总结这个人的：
1. 说话风格（简洁/啰嗦/幽默/正经/直接/委婉等）
2. 常用词语或表达习惯
3. 性格特点
4. 典型的思维方式

直接输出总结文字，不需要标题或格式。"""

    try:
        summary = llm.chat(
            system_prompt="你是一个分析人物语言风格的专家，请客观准确地分析。",
            messages=[{"role": "user", "content": analysis_prompt}],
            max_tokens=300,
            temperature=0.3,
        )
    except Exception as e:
        summary = f"（人格分析暂时不可用，将使用基础模式：{str(e)[:50]}）"

    persona = f"""【关于这个人的性格与风格分析】
{summary}"""

    # 存回数据库
    with get_db() as conn:
        conn.execute(
            "UPDATE avatars SET persona_prompt = ?, updated_at = datetime('now') WHERE id = ?",
            (persona, avatar_id),
        )

    return persona


def get_chat_context(avatar_id: str, user_query: str) -> tuple[str, list[dict]]:
    """
    为一次对话构建完整上下文。

    Returns:
        (system_prompt, retrieved_memories)
    """
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT * FROM avatars WHERE id = ?", (avatar_id,)
        ).fetchone())

    if not avatar:
        raise ValueError(f"替身不存在: {avatar_id}")

    # RAG：根据用户问题检索相关记忆
    relevant = search_memories(avatar_id, user_query, top_k=6)

    # 若检索结果少，补充最新记忆
    if len(relevant) < 3:
        recent = get_recent_memories(avatar_id, limit=5)
        existing_ids = {m["id"] for m in relevant}
        for m in recent:
            if m["id"] not in existing_ids:
                relevant.append(m)

    system_prompt = build_system_prompt(avatar, relevant)
    return system_prompt, relevant
