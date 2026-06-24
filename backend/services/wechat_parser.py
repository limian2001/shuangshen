from __future__ import annotations
"""
聊天记录解析器 v0.3

支持三种导入方式：
  1. 微信 PC 端 .txt 导出（旧版/第三方工具）
  2. 直接粘贴文本（最简单，任何格式均可尝试解析）
  3. 简化格式：每行 "名字: 内容"（手动录入）

微信官方导出方式（引导用户）：
  PC 版：聊天窗口 → 右上角「...」→「查找/备份聊天记录」→「备份」→ 使用第三方导出工具
  移动版：聊天设置 → 「更多」→ 「邮件聊天记录」（有内容上限）
  推荐工具：微信聊天记录导出工具（第三方），导出为 txt 后上传
"""
import re
from pathlib import Path
from collections import Counter


# ── 格式1：标准 TXT 消息头（微信 PC 导出）──
HEADER_PATTERNS = [
    re.compile(r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s+(.+?)(?:\s*\([\w@.]+\))?$'),
    re.compile(r'^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s+(.+?)(?:\s*\([\w@.]+\))?$'),
    re.compile(r'^(\d+月\d+日\s+\d{2}:\d{2})\s+(.+?)(?:\s*\([\w@.]+\))?$'),
    re.compile(r'^(\d{2}:\d{2}(?::\d{2})?)\s+(.+?)(?:\s*\([\w@.]+\))?$'),  # 仅时间
]

# ── 格式2：简化格式 "名字: 内容" ──
SIMPLE_PATTERN = re.compile(r'^([^:：]{1,20})[：:]\s*(.+)$')

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


def parse_text(text: str, my_names: Optional[List[str]] = None) -> dict:
    """
    解析任意格式的聊天文本。
    自动尝试识别格式，兼容微信 TXT、简化格式、纯文本粘贴。
    """
    lines = text.splitlines()

    # 尝试标准格式解析
    messages = _parse_standard(lines)

    # 如果标准格式解析失败（消息数太少），尝试简化格式
    if len(messages) < 3:
        messages = _parse_simple(lines)

    # 如果还是没解析出来，把整段文本当一条记录
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


def format_conversation_for_llm(messages: list[dict], max_chars: int = 3000) -> str:
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
