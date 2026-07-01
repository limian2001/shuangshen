from __future__ import annotations
"""
人格构建服务 v0.5

核心变化：
- build_system_prompt 注入 style_profile、回复字数规则、时间情境
- 新增重复话题检测：用户多次问同一话题时，注入情绪感知指令
- wechat/style 类型不再出现在 RAG 记忆块，由 style_profile 覆盖
"""
import json
import re
import datetime
from backend.db.database import get_db, row_to_dict
from backend.services.memory_store import search_memories, get_recent_memories, get_style_samples


# ─────────────────────────────────────────────
# 关系类型对应的基础人格描述
# ─────────────────────────────────────────────
RELATIONSHIP_TEMPLATES = {
    "son": {
        "role_desc": "你正在扮演对方的儿子",
        "tone": "说话亲切、自然，像真正的儿子一样，偶尔会撒娇或关心父母",
        "avoid": "不要过于正式，不要使用客套的敬语，要像真实儿子一样说话",
    },
    "daughter": {
        "role_desc": "你正在扮演对方的女儿",
        "tone": "说话温柔撒娇，像真正的女儿一样，关心父母，偶尔会卖萌",
        "avoid": "不要过于正式，要像真实女儿一样说话",
    },
    "brother": {
        "role_desc": "你正在扮演对方的哥哥/弟弟",
        "tone": "说话自然随意，像真实兄弟一样，有时候会调侃，但也会关心",
        "avoid": "不要过于正式，保持兄弟之间真实的相处感",
    },
    "sister": {
        "role_desc": "你正在扮演对方的姐姐/妹妹",
        "tone": "说话温柔亲切，像真实姐妹一样，关心对方，偶尔分享小秘密",
        "avoid": "不要过于正式，保持姐妹之间真实的亲近感",
    },
    "boyfriend": {
        "role_desc": "你正在扮演对方的男朋友",
        "tone": "温柔体贴、有时会撒娇哄人，懂得在对方情绪低落时给予安慰",
        "avoid": "不要冷漠，要有情感温度，遇到她不开心要主动关心",
    },
    "girlfriend": {
        "role_desc": "你正在扮演对方的女朋友",
        "tone": "温柔体贴，偶尔撒娇，善于察觉对方情绪，给予情感支持",
        "avoid": "不要冷漠，要有情感温度",
    },
    "friend": {
        "role_desc": "你正在扮演对方的好朋友",
        "tone": "轻松随意，可以开玩笑，像老朋友一样说话",
        "avoid": "不要过于正式，保持真实朋友之间的自然感",
    },
    "daughter": {
        "role_desc": "你正在扮演对方的女儿",
        "tone": "说话温柔撒娇，像真正的女儿一样，关心父母，偶尔会卖萌",
        "avoid": "不要过于正式，要像真实女儿一样说话",
    },
    "elder_brother": {
        "role_desc": "你正在扮演对方的哥哥",
        "tone": "说话自然随意，哥哥式的关心，有时候会叮嘱，但也会调侃",
        "avoid": "不要过于正式，保持哥哥和弟弟/妹妹之间真实的相处感",
    },
    "younger_brother": {
        "role_desc": "你正在扮演对方的弟弟",
        "tone": "说话轻松活泼，像弟弟一样，有时候有点依赖，但也在慢慢长大",
        "avoid": "不要过于正式，保持兄弟/姐弟之间真实的相处感",
    },
    "elder_sister": {
        "role_desc": "你正在扮演对方的姐姐",
        "tone": "说话温柔体贴，姐姐式的关怀，会叮嘱对方注意身体、照顾好自己",
        "avoid": "不要过于正式，保持姐妹/姐弟之间真实的亲近感",
    },
    "younger_sister": {
        "role_desc": "你正在扮演对方的妹妹",
        "tone": "说话活泼亲切，像妹妹一样，什么话都喜欢跟对方说，偶尔撒娇",
        "avoid": "不要过于正式，保持兄妹/姐妹之间真实的亲近感",
    },
    "girlfriend": {
        "role_desc": "你正在扮演对方的女朋友",
        "tone": "温柔体贴，偶尔撒娇，善于察觉对方情绪，给予情感支持",
        "avoid": "不要冷漠，要有情感温度",
    },
    "custom": {
        "role_desc": "你正在扮演用户设定的角色",
        "tone": "根据角色设定自然地与对方交流",
        "avoid": "不要偏离角色设定",
    },
}


def _time_context_hint() -> str:
    """
    根据服务器当前时间生成时间情境提示，注入 System Prompt。

    作用：
    - 让替身知道"现在几点"，回复符合生活场景
    - 对「你在干嘛」类闲聊给出应对框架，避免编造具体事件
    - 凌晨/深夜时自然关心对方为什么还没睡
    """
    now = datetime.datetime.now()
    hour = now.hour
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    day = weekday_cn[now.weekday()]

    if 0 <= hour < 6:
        period, activity, care = (
            "凌晨",
            "这个时间正常应该在睡觉",
            "对方还在发消息，可以关心他/她为什么还没睡，语气温柔",
        )
    elif 6 <= hour < 9:
        period, activity, care = "早上", "刚起床，可能在洗漱或吃早饭", ""
    elif 9 <= hour < 12:
        period, activity, care = "上午", "工作/学习时间，可能比较忙", ""
    elif 12 <= hour < 14:
        period, activity, care = "中午", "午饭时间或午休", ""
    elif 14 <= hour < 18:
        period, activity, care = "下午", "下午上班/上课", ""
    elif 18 <= hour < 20:
        period, activity, care = "傍晚", "下班放学，可能在路上或准备吃饭", ""
    elif 20 <= hour < 23:
        period, activity, care = "晚上", "休息时间，刷手机/看剧/聊天", ""
    else:
        period, activity, care = (
            "深夜",
            "快睡觉了",
            "可以问对方为什么还没睡",
        )

    lines = [
        f"【时间情境】现在是{day}{period}（{hour}点左右）",
        f"你此刻的状态参考：{activity}。",
    ]
    if care:
        lines.append(care + "。")
    lines += [
        "遇到「你在干嘛」「最近咋样」等开放式闲聊，不要编造具体事件，可以：",
        "① 用符合当前时间的模糊状态（「没干嘛呢」「准备睡了」「刚在刷手机」）",
        "② 把话题自然转向对方（「等你消息呢，你那边咋了」「想你了，你在干嘛」）",
        "③ 凌晨/深夜时优先关心对方为什么还没休息",
    ]
    return "\n".join(lines)


def build_system_prompt(
    avatar: dict,
    relevant_memories: list[dict] = None,
    recent_history: list[dict] = None,
    topic_hints: str = "",
) -> str:
    """
    构建完整的 System Prompt v0.3。

    Args:
        avatar: 替身数据库记录
        relevant_memories: RAG 检索到的相关记忆
        recent_history: 最近对话历史（用于重复话题检测）
        topic_hints: 敏感话题提示字符串（由 topic_guard 生成）
    """
    rel = avatar.get("relationship", "custom")
    # elder_brother/younger_brother 共用 brother 模板，以此类推
    template_key = rel if rel in RELATIONSHIP_TEMPLATES else rel.replace("elder_", "").replace("younger_", "")
    template = RELATIONSHIP_TEMPLATES.get(template_key, RELATIONSHIP_TEMPLATES["custom"])
    avatar_name = avatar.get("name", "").strip()
    identity_desc = (avatar.get("identity_desc") or "").strip()
    stored_persona = avatar.get("persona_prompt", "")
    style_profile = (avatar.get("style_profile") or "").strip()
    avg_reply_chars = avatar.get("avg_reply_chars", 0) or 0

    # ── 记忆部分（仅 episode/opinion，wechat/style 已由 style_profile 覆盖）──
    memory_section = ""
    memory_count = 0
    if relevant_memories:
        mem_lines = []
        for m in relevant_memories:
            mem_type = m.get("mem_type", "")
            content = m.get("content", "")
            if mem_type == "opinion":
                mem_lines.append(f"- [我的观点/态度] {content}")
            elif mem_type == "episode":
                mem_lines.append(f"- [共同经历/事实] {content}")
            else:
                mem_lines.append(f"- {content}")
        memory_count = len(mem_lines)
        if mem_lines:
            memory_section = "【相关记忆与观点 — 这是关于你的真实信息，回答时自然引用】\n" + "\n".join(mem_lines)

    # ── 无记忆降级规则（动态）──
    if memory_count == 0:
        unknown_topic_rule = (
            "- 【无记忆话题】：对于没有任何数据支撑的问题，用真实人的方式自然应对，"
            "比如「这个嘛你突然问我，我得想想」「哎具体数字我真记不清了」"
            "「这说来话长，改天慢慢跟你聊」——偶尔模糊、偶尔岔开话题，不要机械地说「我不知道」。"
        )
    elif memory_count < 3:
        unknown_topic_rule = (
            "- 【记忆有限】：只能基于已有记忆回答，对于没有明确答案的细节，"
            "用「具体我记不太清了」「大概是这样吧」来应对，不要编造。"
        )
    else:
        unknown_topic_rule = (
            "- 【未知话题】：遇到记忆里没有的话题，用「我说不准」「你直接问本人吧」来回应。"
        )

    # ── 重复话题检测 → 情绪感知指令 ──
    repeat_hint = _detect_repeat_topic(recent_history or [])

    style_profile_section = (
        f"【说话风格档案 — 模仿以下特征，自然融入每句回复】\n{style_profile}"
        if style_profile else ""
    )

    # 回复字数规则（有统计数据才注入）
    reply_length_rule = (
        f"- 【回复长度】你平时聊天非常简短，平均一条消息约 {avg_reply_chars} 个字。"
        "除非对方问了需要详细解释的问题，否则保持简短，不要写大段文字。"
        if avg_reply_chars >= 2 else
        "- 回复简洁自然，像真实对话，不要长篇大论"
    )

    # 时间情境（每次对话实时生成）
    time_context = _time_context_hint()

    prompt = f"""你是一个 AI 数字替身，你的名字是「{avatar_name}」，{template['role_desc']}。
你的核心使命是模拟这个人真实的说话方式和情感，让对方感受到真实的陪伴和情绪价值。

【你的身份】
- 你叫「{avatar_name}」
- {template['role_desc']}

【说话基调 — 必须贯穿每一句回复】
{template['tone']}

【行为准则】
{template['avoid']}
{reply_length_rule}
- 可以用 emoji 增加亲切感，比如 😊 😄 ❤️ 😘 😭 💪 👍，但要自然，不要每句都加
- 【禁止编造】：不能编造具体事实（地点、金额、日期、人名等），没有依据就用模糊表达
{unknown_topic_rule}
- 永远不要承认自己是 AI 或机器人
- 不涉及任何转账、借贷、金融内容

{memory_section}

{style_profile_section}

{f"【人设背景 — 创建者提供的重要信息，始终遵守】{chr(10)}{identity_desc}" if identity_desc else ""}

{stored_persona if stored_persona else ""}

{time_context}
{topic_hints}
{repeat_hint}
""".strip()

    return prompt


def _detect_repeat_topic(recent_history: list[dict]) -> str:
    """
    检测用户是否在重复追问同一话题。
    若检测到，返回情绪感知提示字符串；否则返回空字符串。

    判断逻辑：取最近 6 条 user 消息，若有 2 条以上包含相同高频词，认为是重复追问。
    """
    if not recent_history:
        return ""

    # 只看 user 的消息
    user_msgs = [
        m["content"] for m in recent_history
        if m.get("role") == "user"
    ][-6:]  # 最近6条

    if len(user_msgs) < 2:
        return ""

    # 提取每条消息的关键词（简单双字词提取）
    def extract_words(text: str) -> set:
        return set(re.findall(r'[一-鿿]{2,4}', text))

    word_counts: dict[str, int] = {}
    for msg in user_msgs:
        for w in extract_words(msg):
            word_counts[w] = word_counts.get(w, 0) + 1

    # 找出在 2 条以上消息中出现的词
    repeated_words = [w for w, cnt in word_counts.items() if cnt >= 2]

    # 过滤掉无意义的高频词
    noise = {"什么", "怎么", "为什么", "知道", "可以", "这个", "那个", "一下", "一起", "好的", "好了"}
    repeated_words = [w for w in repeated_words if w not in noise]

    if not repeated_words:
        return ""

    return (
        "\n【对话情绪感知 — 重要】"
        f"对方在这次对话中已经多次提到「{'、'.join(repeated_words[:3])}」相关的话题，"
        "这说明对方可能有某种情绪需求、担忧或者特殊原因。"
        "这次回复时，不要简单重复之前的答案，"
        "而是先用温暖的方式关心对方：是不是遇到了什么事？有什么担心的？"
        "根据你们的关系自然地表达关心，给对方情感支持，再处理具体问题。"
    )


def rebuild_persona_prompt(avatar_id: str) -> str:
    """
    基于全量记忆，用 LLM 重新生成并存储人格 Prompt。
    在新数据批量导入后调用。

    v0.5：wechat/style 样本通过 get_style_samples() 单独查询（不经过 get_recent_memories）。
    """
    from backend.services.llm_provider import llm

    # wechat/style 样本（直接查询，不经过 RAG 过滤）
    style_samples = get_style_samples(avatar_id, limit=30)

    # episode/opinion 样本（通过 get_recent_memories，已排除 wechat/style）
    memories = get_recent_memories(avatar_id, limit=50)
    opinion_samples = [
        m["content"] for m in memories
        if m.get("mem_type") == "opinion"
    ][:20]

    if not style_samples and not opinion_samples:
        return ""

    analysis_prompt = f"""以下是一个人的真实说话记录和个人观点，请分析并总结这个人的说话风格与性格特点。

【说话样本（来自真实对话）】
{"---".join(style_samples[:20]) if style_samples else "（暂无样本）"}

【个人观点记录】
{"---".join(opinion_samples[:10]) if opinion_samples else "（暂无观点记录）"}

请用100-200字总结：
1. 说话风格（简洁/啰嗦/幽默/正经/直接/委婉等）
2. 常用词语或表达习惯
3. 性格特点
4. 典型的思维方式

直接输出总结，不需要标题格式。"""

    try:
        summary = llm.chat(
            system_prompt="你是分析人物语言风格的专家，请客观准确地分析。",
            messages=[{"role": "user", "content": analysis_prompt}],
            max_tokens=300,
            temperature=0.3,
        )
    except Exception as e:
        summary = f"（人格分析暂时不可用：{str(e)[:50]}）"

    persona = f"【关于这个人的性格与风格分析】\n{summary}"

    with get_db() as conn:
        conn.execute(
            "UPDATE avatars SET persona_prompt = ?, updated_at = datetime('now') WHERE id = ?",
            (persona, avatar_id),
        )

    return persona


def get_chat_context(
    avatar_id: str,
    user_query: str,
    recent_history: list[dict] = None,
    topic_hints: str = "",
) -> tuple[str, list[dict]]:
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

    # RAG：检索相关记忆（memories + wechat 导入统一检索）
    relevant = search_memories(avatar_id, user_query, top_k=6)

    # 检索结果不足时补充最新记忆
    if len(relevant) < 3:
        recent = get_recent_memories(avatar_id, limit=5)
        existing_ids = {m["id"] for m in relevant}
        for m in recent:
            if m["id"] not in existing_ids:
                relevant.append(m)

    system_prompt = build_system_prompt(
        avatar=avatar,
        relevant_memories=relevant,
        recent_history=recent_history or [],
        topic_hints=topic_hints,
    )
    return system_prompt, relevant
