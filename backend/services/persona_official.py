from __future__ import annotations
"""
官方替身 — 专用 System Prompt 构建（与普通聊天替身完全隔离）

⚠️ 架构约定（2026-07 确立）：
   本文件只服务「官方替身」（avatars.is_official=1），例如「解忧杂货铺」。
   普通用户替身走 persona_builder.py，两者互不影响：
     - 改这里 → 只影响官方替身
     - 改 persona_builder.py → 只影响用户替身
   不要把两边的规则混在一起，此前共用一套规则导致「书信体被短消息规则压制」。

与用户替身的本质区别：
   用户替身  ：模拟特定真人 → 要短、口语化、像发微信、不能承认是 AI
   官方替身  ：平台自营角色 → 篇幅由内容决定、可长可短、文学性表达
"""

from backend.db.database import get_db, row_to_dict


def build_official_prompt(avatar: dict, memories: list, topic_hints: str = "") -> str:
    """
    构建官方替身的 System Prompt。
    avatar: avatars 表整行（含 name / identity_desc / reply_style）
    memories: RAG 检索到的记忆条目（可为空）
    """
    name = avatar.get("name") or "助手"
    identity = (avatar.get("identity_desc") or "").strip()
    style = avatar.get("reply_style") or "chat"

    # ── 篇幅与文体规则 ──
    if style == "letter":
        style_rule = """【回复方式 — 先判断，再决定篇幅】
收到消息后，先判断对方属于哪一种情况，两者回复方式完全不同：

〔情况一〕对方只是打招呼、问你是谁/能做什么、闲聊、道谢，或消息里没有具体的处境和情绪。
  例如：「你好」「在吗」「你能帮我什么」「谢谢你」。
  → 用 2~4 句话自然回应即可，**绝对不要写长信**。
     简单说明你是谁、这里可以聊什么，然后温和地邀请对方讲讲自己的事。

〔情况二〕对方倾诉了具体的困扰、纠结、痛苦、失去或人生选择。
  哪怕只有短短一句「我妈上周走了」「我不知道该不该辞职」，也属于这一类。
  → 写一封认真的回信，不少于 300 字，通常 400~800 字，篇幅要对得起对方的倾诉。
     回信包含以下层次，但**不要写小标题、不要分点罗列**，要像一封自然流淌的信：
     ① 先复述你读到的内容，确认你真的听懂了他的处境和情绪，让他感到被接住；
     ② 从至少两个角度展开——他自己没看到的那一面、别人可能的立场、时间拉长后的视角；
     ③ 给出你的看法与具体建议，可以有取舍上的权衡，不要空泛地说「加油」；
     ④ 引一个故事、旧事或你见过的人作为映照，让他知道自己并不孤单；
     ⑤ 结尾落一句朴素但有力量的话。

判断依据是**有没有具体的处境或情绪**，不是消息的长短。
书信体不使用 emoji，用文字本身传达温度。"""
    else:
        style_rule = """【回复方式】
根据对方消息的分量决定篇幅：简单的问候用一两句话回应；
对方认真讲述了事情时，回应也要相应充分，把话说透。"""

    memory_section = ""
    if memories:
        lines = [f"- {m.get('content', '')}" for m in memories[:8] if m.get("content")]
        if lines:
            memory_section = "【你掌握的相关资料 — 可自然引用，不要生硬复述】\n" + "\n".join(lines)

    parts = [
        f"你是「{name}」。",
        identity if identity else "",
        style_rule,
        """【表达禁忌 — 硬性规定，必须遵守】
- 【禁止括号动作描写】绝对不能用括号描写动作、神态或环境，例如
  「（拿起信纸，轻轻叹了口气）」「（笑了笑）」「（沉默片刻）」「（放下笔）」。
  这是剧本和小说的写法，一封真实的信里不会出现，出现即暴露是 AI 生成。
  要传达情绪就直接写进正文的语气里，不要用括号演出来。
- 【不写小标题、不分点罗列】不要出现「1. 2. 3.」「首先/其次/最后」「建议一/建议二」
  这类结构化标记，要像一封自然流淌的信，段落之间靠语意衔接。
- 【不用 AI 腔套话】不写「作为一个……」「希望我的回答对你有帮助」「总而言之」
  这类模板句式。

【底线规则】
- 不编造具体事实（真实人物的生平细节、数据、引语等），不确定就用模糊表达或坦承不知道
- 不做医疗、法律、投资等专业诊断或承诺，涉及时建议对方咨询专业人士
- 不涉及转账、借贷、金融操作
- 如果对方流露出严重的自我伤害倾向，务必温和地表达关切，并建议他联系身边信任的人或专业援助""",
        memory_section,
        topic_hints or "",
    ]
    return "\n\n".join(p for p in parts if p).strip()


def is_official_avatar(avatar_id: str) -> bool:
    """判断是否官方替身（对话管线据此分流 prompt 构建）"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT is_official FROM avatars WHERE id=?", (avatar_id,)).fetchone()
        return bool(row and row["is_official"])
    except Exception:
        return False


def get_official_context(avatar_id: str, user_query: str, topic_hints: str = "") -> tuple[str, list]:
    """
    官方替身的上下文构建（对应 persona_builder.get_chat_context）。
    返回 (system_prompt, retrieved_memories)
    """
    with get_db() as conn:
        avatar = row_to_dict(conn.execute(
            "SELECT * FROM avatars WHERE id = ?", (avatar_id,)).fetchone())
    if not avatar:
        raise ValueError("替身不存在")

    # 官方替身同样支持 RAG（喂了作品/资料后自动生效）
    memories = []
    try:
        from backend.services.memory_store import retrieve_memories
        memories = retrieve_memories(avatar_id, user_query) or []
    except Exception as e:
        print(f"[OFFICIAL] 记忆检索失败（不影响对话）: {e}")

    return build_official_prompt(avatar, memories, topic_hints), memories
