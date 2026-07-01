from __future__ import annotations
"""
聊天记录解析器 v0.4

支持四种导入方式：
  1. 微信手机端邮件导出 .txt（单次100条，可多批次）
  2. PC 微信 Windows HTML 导出
  3. 直接粘贴文本（最简单，任何格式均可尝试解析）
  4. 简化格式：每行 "名字: 内容"（手动录入）
"""
import re
import html as _html_lib
from pathlib import Path
from collections import Counter
from typing import Optional, List


# ── 格式1：标准 TXT 消息头（微信 PC 导出）──
HEADER_PATTERNS = [
    re.compile(r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s+(.+?)(?:\s*\([\w@.]+\))?$'),
    re.compile(r'^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s+(.+?)(?:\s*\([\w@.]+\))?$'),
    re.compile(r'^(\d+月\d+日\s+\d{2}:\d{2})\s+(.+?)(?:\s*\([\w@.]+\))?$'),
    re.compile(r'^(\d{2}:\d{2}(?::\d{2})?)\s+(.+?)(?:\s*\([\w@.]+\))?$'),  # 仅时间
]

# ── 格式2：简化格式 "名字: 内容" ──
SIMPLE_PATTERN = re.compile(r'^([^:：]{1,20})[：:]\s*(.+)$')

# ── 格式5：手机微信复制粘贴格式 "发件人名称 HH:MM" 独占一行 ──
# 例：🐱面面🥕 14:47
_MOBILE_HEADER = re.compile(r'^(.{1,40}?)\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*$')

# ── 格式6：微信「长按→复制多条」格式，发件人与完整日期各占一行 ──
# 例：妹妹\n2020年11月23日 19:47\n消息内容
_WECHAT_DATE_TIME = re.compile(
    r'^\s*(\d{4}年\d{1,2}月\d{1,2}日)\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*$'
)

# 跳过的系统消息
SKIP_PATTERNS = [
    re.compile(r'^\[.{1,20}\]$'),
    re.compile(r'^<.+?>$'),
    re.compile(r'撤回了一条消息'),
    re.compile(r'^-+$'),
    re.compile(r'^\s*$'),
    re.compile(r'^以下为.*聊天记录'),
]


class ChatMessage:
    def __init__(self, timestamp: str, sender: str, content: str):
        self.timestamp = timestamp
        self.sender = sender
        self.content = content.strip()
        self.is_from_me = False

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "sender": self.sender,
            "content": self.content,
            "is_from_me": self.is_from_me,
        }


def parse_text(text: str, my_names: Optional[List[str]] = None) -> dict:  # noqa: E501
    """
    解析任意格式的聊天文本。
    自动尝试识别格式，兼容微信 TXT、简化格式、纯文本粘贴。

    尝试顺序（优先级从高到低）：
      1. 标准 TXT 格式（微信 PC 导出）
      2. 长按复制格式（发件人独行 + 完整日期独行 + 内容行） ← 推荐黏贴方式
      3. 手机复制格式（发件人+时间 同行）
      4. 简化格式 "名字: 内容"
      5. 兜底：整段当一条记录
    """
    lines = text.splitlines()

    # ① 标准格式（时间戳 + 发件人 在头行）
    messages = _parse_standard(lines)

    # ② 微信「长按→复制多条」格式（含完整年月日）
    if len(messages) < 3:
        messages = _parse_wechat_copy_paste(lines)

    # ③ 手机微信复制粘贴格式（发件人 + 时间 在头行，仅 HH:MM）
    if len(messages) < 3:
        messages = _parse_mobile_wechat(lines)

    # ④ 简化格式 "名字: 内容"
    if len(messages) < 3:
        messages = _parse_simple(lines)

    # ⑤ 如果还是没解析出来，把整段文本当一条记录
    if not messages:
        messages = [ChatMessage("", "unknown", text.strip())]

    # 推断「我」
    if not my_names:
        my_names = _infer_my_names(messages)

    my_names_lower = [n.lower().strip() for n in my_names]
    for msg in messages:
        if msg.sender.lower().strip() in my_names_lower:
            msg.is_from_me = True

    my_messages = [m for m in messages if m.is_from_me]
    timestamps = [m.timestamp for m in messages if m.timestamp]

    return {
        "messages": [m.to_dict() for m in messages],
        "my_messages": [m.to_dict() for m in my_messages],
        "all_messages": [m.to_dict() for m in messages],
        "inferred_my_names": my_names,
        "stats": {
            "total": len(messages),
            "mine": len(my_messages),
            "others": len(messages) - len(my_messages),
            "date_range": {
                "start": timestamps[0] if timestamps else None,
                "end": timestamps[-1] if timestamps else None,
            },
            "unique_senders": list({m.sender for m in messages}),
        },
    }


def parse_wechat_txt(file_path, my_names=None) -> dict:
    """从文件路径解析（兼容旧接口）"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    content = None
    for enc in ["utf-8", "utf-8-sig", "gbk", "gb18030"]:
        try:
            content = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        raise ValueError("无法解码文件，请确认为 UTF-8 或 GBK 编码的 txt 文件")

    return parse_text(content, my_names)


def _parse_standard(lines: list[str]) -> list[ChatMessage]:
    """解析标准微信 TXT 格式"""
    messages = []
    current_sender = None
    current_time = None
    body_lines = []

    for line in lines:
        header = _try_parse_header(line)
        if header:
            if current_sender is not None:
                content = "\n".join(body_lines).strip()
                if content and not _should_skip(content):
                    messages.append(ChatMessage(current_time, current_sender, content))
            current_time, current_sender = header
            body_lines = []
        elif current_sender is not None:
            body_lines.append(line)

    if current_sender and body_lines:
        content = "\n".join(body_lines).strip()
        if content and not _should_skip(content):
            messages.append(ChatMessage(current_time, current_sender, content))

    return messages


def _parse_simple(lines: list[str]) -> list[ChatMessage]:
    """解析简化格式：名字: 内容"""
    messages = []
    for line in lines:
        if _should_skip(line):
            continue
        m = SIMPLE_PATTERN.match(line.strip())
        if m:
            sender = m.group(1).strip()
            content = m.group(2).strip()
            if content:
                messages.append(ChatMessage("", sender, content))
    return messages


def _parse_wechat_copy_paste(lines: list[str]) -> list[ChatMessage]:
    """
    解析微信「长按选中多条消息→复制」粘贴格式（含完整年月日）。

    每条消息结构：
        妹妹 璟珹里【叠墅】可指定     ← 发件人（独行，无时间）
        2020年11月23日 19:47          ← 完整日期+时间（独行）
        不过我在杭州工作              ← 内容（可多行）

    识别逻辑：
      - 扫描所有独占一行的「YYYY年M月D日 HH:MM」行（_WECHAT_DATE_TIME）
      - 该行的前一行即为发件人
      - 该行与下一个发件人行之间的内容为消息正文
      - 至少 3 条日期行才认为格式匹配
    """
    non_empty = [l.strip() for l in lines if l.strip()]
    date_positions = [i for i, l in enumerate(non_empty) if _WECHAT_DATE_TIME.match(l)]

    if len(date_positions) < 3:
        return []

    messages: list[ChatMessage] = []

    for idx, di in enumerate(date_positions):
        ts_m = _WECHAT_DATE_TIME.match(non_empty[di])
        timestamp = f"{ts_m.group(1)} {ts_m.group(2)}"  # e.g. "2020年11月23日 19:47"

        # 发件人：日期行的前一行（存在且本身不是日期行）
        if di == 0 or _WECHAT_DATE_TIME.match(non_empty[di - 1]):
            continue  # 没有发件人行，跳过
        sender = non_empty[di - 1]

        # 内容：日期行之后，直到下一个「发件人行」之前
        content_start = di + 1
        if idx + 1 < len(date_positions):
            # 下一条消息的发件人行 = date_positions[idx+1] - 1
            content_end = date_positions[idx + 1] - 1
        else:
            content_end = len(non_empty)

        content_parts = non_empty[content_start:content_end]
        content = '\n'.join(content_parts).strip()

        if content and not _should_skip(content):
            messages.append(ChatMessage(timestamp, sender, content))

    return messages


def _parse_mobile_wechat(lines: list[str]) -> list[ChatMessage]:
    """
    解析手机微信复制粘贴格式。

    格式：
        🐱面面🥕 14:47        ← 头行：发件人 + 时间（无冒号分隔）
        不过肯定不是跳楼      ← 内容行（可多行）
        🐱面面🥕 14:48
        要么喝醉酒，要么打架
        抽象妹妹 17:30
        应该是打架

    识别逻辑：
      - 头行必须严格符合 _MOBILE_HEADER（末尾是 HH:MM，前面是1-40字符）
      - 头行自身不含实质内容，内容在下一行
      - 至少 3 条头行才认为匹配成功
    """
    # 预扫描：至少要有 3 行匹配 _MOBILE_HEADER 才使用此格式
    header_count = sum(1 for l in lines if _MOBILE_HEADER.match(l.strip()))
    if header_count < 3:
        return []

    messages: list[ChatMessage] = []
    current_sender: Optional[str] = None
    current_time: str = ""
    body_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        m = _MOBILE_HEADER.match(stripped)
        if m:
            # 收尾上一条消息
            if current_sender is not None and body_lines:
                content = "\n".join(body_lines).strip()
                if content and not _should_skip(content):
                    messages.append(ChatMessage(current_time, current_sender, content))
            current_sender = m.group(1).strip()
            current_time = m.group(2)
            body_lines = []
        elif current_sender is not None:
            body_lines.append(stripped)

    # 最后一条消息
    if current_sender and body_lines:
        content = "\n".join(body_lines).strip()
        if content and not _should_skip(content):
            messages.append(ChatMessage(current_time, current_sender, content))

    return messages


def _try_parse_header(line: str) -> Optional[tuple]:
    line = line.strip()
    for pat in HEADER_PATTERNS:
        m = pat.match(line)
        if m:
            return m.group(1), m.group(2).strip()
    return None


def _should_skip(content: str) -> bool:
    for pat in SKIP_PATTERNS:
        if pat.search(content):
            return True
    return False


def _infer_my_names(messages: list[ChatMessage]) -> list[str]:
    counts = Counter(m.sender for m in messages)
    senders = list(counts.keys())
    if "我" in senders:
        return ["我"]
    if len(senders) == 2:
        return [min(counts, key=counts.get)]
    return ["我"]


def calculate_reply_stats(my_messages: list[dict]) -> dict:
    """
    统计「我」的回复长度分布，用于注入 System Prompt 控制回复字数。

    Returns:
        {"avg_chars": int, "median_chars": int, "sample_count": int}
        avg_chars=0 表示数据不足，调用方不注入长度规则。
    """
    lengths = [
        len(m.get("content", "").strip())
        for m in my_messages
        if (
            m.get("content", "").strip()
            and not m.get("content", "").startswith("[")
            and not m.get("content", "").startswith("<")
        )
    ]
    if len(lengths) < 5:  # 样本太少则不统计
        return {"avg_chars": 0, "median_chars": 0, "sample_count": len(lengths)}

    lengths.sort()
    n = len(lengths)
    avg = round(sum(lengths) / n)
    median = lengths[n // 2]
    return {"avg_chars": avg, "median_chars": median, "sample_count": n}


def extract_style_samples(my_messages: list[dict], max_samples: int = 200) -> list[str]:
    """从「我」的消息中提取风格样本"""
    samples = []
    for msg in my_messages:
        content = msg.get("content", "").strip()
        if len(content) > 5 and not content.startswith("[") and not content.startswith("<"):
            samples.append(content)
        if len(samples) >= max_samples:
            break
    return samples


# ─────────────────────────────────────────────
# PC 微信 HTML 导出解析器 v0.4
# ─────────────────────────────────────────────

# PC 微信 HTML 中独立成行的时间戳格式（多种已知样式）
_TS_LINE = re.compile(
    r'^\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日\s]\s*'  # 含年月日
    r'|(?:\d{1,2}[-/月]\d{1,2}[日\s]\s*)?)'             # 或省略年份
    r'(\d{2}:\d{2}(?::\d{2})?)\s*$'
)
# 单独一行的"纯日期"（用来切割段落，不作时间戳）
_DATE_ONLY = re.compile(r'^\s*\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\s*$')


def parse_wechat_html(html_content: str, my_names: Optional[List[str]] = None) -> dict:
    """
    解析 PC 微信 Windows 导出的 HTML 聊天记录。

    处理流程：
      1. class 块结构化解析（已知 PC 微信 HTML 格式）
      2. HTML → 纯文本，用「时间戳/发件人/内容」三行格式解析
      3. 两者均失败时，降级为 parse_text()（通用格式）
    """
    # ── 步骤 1：class 块解析 ──
    messages = _parse_html_blocks(html_content)

    # ── 步骤 2：文本行格式解析 ──
    if not messages:
        plain = _html_to_plaintext(html_content)
        messages = _parse_ts_name_content(plain.splitlines())

    # ── 步骤 3：通用文本解析兜底 ──
    if not messages:
        plain = _html_to_plaintext(html_content)
        return parse_text(plain, my_names)

    # ── 标记「我」──
    if not my_names:
        my_names = _infer_my_names(messages)
    my_names_lower = [n.lower().strip() for n in (my_names or [])]
    for msg in messages:
        if msg.sender.lower().strip() in my_names_lower:
            msg.is_from_me = True

    my_messages = [m for m in messages if m.is_from_me]
    timestamps = [m.timestamp for m in messages if m.timestamp]
    return {
        "messages": [m.to_dict() for m in messages],
        "my_messages": [m.to_dict() for m in my_messages],
        "all_messages": [m.to_dict() for m in messages],
        "inferred_my_names": my_names or [],
        "stats": {
            "total": len(messages),
            "mine": len(my_messages),
            "others": len(messages) - len(my_messages),
            "date_range": {
                "start": timestamps[0] if timestamps else None,
                "end": timestamps[-1] if timestamps else None,
            },
            "unique_senders": list({m.sender for m in messages}),
        },
    }


def _html_to_plaintext(html_content: str) -> str:
    """
    将 HTML 转换为结构化纯文本，尽量保留时间戳 / 发件人 / 内容的行级结构。
    """
    # 块级标签 → 换行
    text = re.sub(
        r'<(?:div|p|br/?|tr|li|h[1-6]|section|article|header|footer)[^>]*>',
        '\n', html_content, flags=re.IGNORECASE
    )
    # 去掉剩余标签
    text = re.sub(r'<[^>]+>', '', text)
    # 还原 HTML 实体
    text = _html_lib.unescape(text)
    # 合并行内多余空格
    text = re.sub(r'[ \t]+', ' ', text)
    # 行首尾空白
    text = '\n'.join(line.strip() for line in text.splitlines())
    # 压缩超过 2 个的连续空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _parse_html_blocks(html_content: str) -> list:
    """
    从已知的 PC 微信 HTML class 块中提取结构化消息。
    仅做 class 属性匹配，至少找到 3 条才认为匹配成功。
    """
    BLOCK_CLASSES = ('message', 'msg', 'chat-item', 'record-item',
                     'record_item', 'chatRecord', 'chat_item')

    for bc in BLOCK_CLASSES:
        pat = re.compile(
            r'<div[^>]+class="[^"]*\b' + re.escape(bc) + r'\b[^"]*"[^>]*>(.*?)</div\s*>',
            re.DOTALL | re.IGNORECASE,
        )
        blocks = pat.findall(html_content)
        if len(blocks) < 3:
            continue

        messages: list = []
        for block in blocks:
            time_m = re.search(
                r'class="[^"]*(?:time|date)[^"]*"[^>]*>\s*([^<]{3,50}?)\s*<',
                block, re.IGNORECASE,
            )
            sender_m = re.search(
                r'class="[^"]*(?:sender|name|nickname|from_name|display_name|user_name)[^"]*"'
                r'[^>]*>\s*([^<]{1,30}?)\s*<',
                block, re.IGNORECASE,
            )
            content_m = re.search(
                r'class="[^"]*(?:content|text|body|msg_content|content_text)[^"]*"'
                r'[^>]*>(.*?)</(?:div|p|span)',
                block, re.DOTALL | re.IGNORECASE,
            )
            if sender_m and content_m:
                ts = _html_lib.unescape(time_m.group(1)).strip() if time_m else ''
                sender = _html_lib.unescape(sender_m.group(1)).strip()
                raw_content = re.sub(r'<[^>]+>', '', content_m.group(1))
                content = _html_lib.unescape(raw_content).strip()
                if content and sender and not _should_skip(content):
                    messages.append(ChatMessage(ts, sender, content))

        if len(messages) >= 3:
            return messages

    return []


def _parse_ts_name_content(lines: list) -> list:
    """
    解析「时间戳独行 → 发件人独行 → 内容行」格式。
    这是 PC 微信 HTML 去标签后的典型文本结构。
    空行会被忽略（HTML 转文本后各行之间常有空行）。
    """
    messages: list = []

    # 只保留非空行，但要记录哪些是时间戳
    non_empty = [l.strip() for l in lines if l.strip()]

    def _is_ts(line: str) -> bool:
        """判断是否是独立时间戳行"""
        if _TS_LINE.match(line):
            return True
        # 兼容「2024-01-01 09:00:00」形式（全行都是时间）
        ts_full = re.compile(
            r'^(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日 ]*)?'
            r'(\d{2}:\d{2}(?::\d{2})?)\s*$'
        )
        return bool(ts_full.match(line))

    j = 0
    n = len(non_empty)
    while j < n:
        line = non_empty[j]
        if _is_ts(line):
            ts = line
            # 紧接的非时间戳行是发件人
            if j + 1 < n:
                sender = non_empty[j + 1]
                if not _is_ts(sender) and not _DATE_ONLY.match(sender) and len(sender) <= 30:
                    # 收集内容（直到下一个时间戳）
                    content_parts = []
                    k = j + 2
                    while k < n and not _is_ts(non_empty[k]) and not _DATE_ONLY.match(non_empty[k]):
                        content_parts.append(non_empty[k])
                        k += 1
                    content = '\n'.join(content_parts).strip()
                    if content and not _should_skip(content):
                        messages.append(ChatMessage(ts, sender, content))
                    j = k
                    continue
        j += 1
    return messages


def format_conversation_for_llm(messages: list, max_chars: int = 3000) -> str:
    """
    将消息列表格式化为 LLM 可读的对话文本（用于语义解析）
    """
    lines = []
    total = 0
    for msg in messages:
        sender = "【我】" if msg.get("is_from_me") else f"【{msg.get('sender', '对方')}】"
        content = msg.get("content", "").strip()
        if not content:
            continue
        line = f"{sender}：{content}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)
