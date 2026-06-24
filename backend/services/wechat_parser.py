from __future__ import annotations
"""
微信聊天记录解析器

支持格式：微信 PC 端导出的 .txt 文件
典型格式：
    2023-10-01 09:30:22 张三(wxid_abc)
    你好啊

    2023-10-01 09:31:00 我
    你好你好

解析策略：
- 提取「我」（创建者）说的话，用于风格学习
- 提取双方对话，用于记忆存储
"""
import re
from datetime import datetime
from pathlib import Path


# 匹配消息头部的几种常见格式
# 格式1: 2023-10-01 09:30:22 昵称(wxid_xxx)
# 格式2: 2023/10/01 09:30:22 昵称
# 格式3: 10月1日 09:30 昵称
HEADER_PATTERNS = [
    re.compile(r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(.+?)(?:\([\w@]+\))?$'),
    re.compile(r'^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+(.+?)(?:\([\w@]+\))?$'),
    re.compile(r'^(\d+月\d+日\s+\d{2}:\d{2})\s+(.+?)(?:\([\w@]+\))?$'),
]

# 跳过的系统消息
SKIP_PATTERNS = [
    re.compile(r'^\[.*?\]$'),        # [图片] [语音] [视频] 等
    re.compile(r'^<.*?>$'),          # XML 消息
    re.compile(r'你撤回了一条消息'),
    re.compile(r'对方撤回了一条消息'),
    re.compile(r'^\s*$'),
]


class WechatMessage:
    def __init__(self, timestamp: str, sender: str, content: str):
        self.timestamp = timestamp
        self.sender = sender
        self.content = content.strip()
        self.is_from_me = False  # 由解析器标记

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "sender": self.sender,
            "content": self.content,
            "is_from_me": self.is_from_me,
        }


def parse_wechat_txt(
    file_path,
    my_names=None,
):
    """
    解析微信聊天记录 txt 文件。

    Args:
        file_path: 文件路径
        my_names: 识别「我」的名称列表，如 ["我", "小王", "Wang"]
                  若为空，自动尝试推断（取消息数最少的一方）

    Returns:
        {
          "messages": [WechatMessage.to_dict(), ...],
          "my_messages": [...],     # 仅「我」说的话
          "stats": {total, mine, others, date_range}
        }
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    # 尝试多种编码
    content = None
    for enc in ["utf-8", "utf-8-sig", "gbk", "gb18030"]:
        try:
            content = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        raise ValueError("无法解码文件，请确认为微信导出的 txt 格式")

    messages = _parse_lines(content.splitlines())

    # 推断「我」的名称
    if not my_names:
        my_names = _infer_my_names(messages)

    # 标记 is_from_me
    my_names_lower = [n.lower().strip() for n in my_names]
    for msg in messages:
        if msg.sender.lower().strip() in my_names_lower:
            msg.is_from_me = True

    my_messages = [m for m in messages if m.is_from_me]
    timestamps = [m.timestamp for m in messages if m.timestamp]

    return {
        "messages": [m.to_dict() for m in messages],
        "my_messages": [m.to_dict() for m in my_messages],
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


def _parse_lines(lines: list[str]) -> list[WechatMessage]:
    """逐行解析，识别消息头 + 正文"""
    messages = []
    current_header = None
    current_sender = None
    current_time = None
    body_lines = []

    for line in lines:
        header = _try_parse_header(line)
        if header:
            # 保存上一条消息
            if current_sender is not None:
                content = "\n".join(body_lines).strip()
                if content and not _should_skip(content):
                    messages.append(WechatMessage(current_time, current_sender, content))
            current_time, current_sender = header
            body_lines = []
        elif current_sender is not None:
            body_lines.append(line)

    # 保存最后一条
    if current_sender and body_lines:
        content = "\n".join(body_lines).strip()
        if content and not _should_skip(content):
            messages.append(WechatMessage(current_time, current_sender, content))

    return messages


def _try_parse_header(line: str) -> tuple | None:
    """尝试识别消息头，返回 (timestamp_str, sender) 或 None"""
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


def _infer_my_names(messages: list[WechatMessage]) -> list[str]:
    """
    简单推断：在双人对话中，消息数量较少的一方更可能是「我」。
    若超过 2 人，默认返回 ["我"]。
    """
    from collections import Counter
    counts = Counter(m.sender for m in messages)
    senders = list(counts.keys())

    if "我" in senders:
        return ["我"]

    if len(senders) == 2:
        # 消息少的那方是「我」（通常）
        return [min(counts, key=counts.get)]

    return ["我"]


def extract_style_samples(my_messages: list[dict], max_samples: int = 100) -> list[str]:
    """
    从「我」的消息中提取风格样本（用于构建人格 Prompt）
    过滤太短的消息，优先选择有实质内容的
    """
    samples = []
    for msg in my_messages:
        content = msg.get("content", "").strip()
        if len(content) > 5 and not content.startswith("[") and not content.startswith("<"):
            samples.append(content)
        if len(samples) >= max_samples:
            break
    return samples
