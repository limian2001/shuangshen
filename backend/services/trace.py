from __future__ import annotations
"""
对话链路 Trace — 全链路可见性采集器（v0.9）

设计原则：
- 请求级收集：数据挂在 flask.g 上，各模块（topic_guard / memory_store /
  llm_provider）调用 trace_event() 追加，请求结束时 chat.py 一次性落库
- 绝不影响主流程：所有操作 try/except 包裹，无请求上下文时静默跳过
- 保留策略：TRACE_RETENTION_DAYS 天 + TRACE_MAX_ROWS 条双上限，插入时修剪
  （只修剪 chat_traces 运维数据，替身本体数据永不清理）

trace JSON 四段结构：
  topic:     话题预设检测（命中/未命中、匹配方式）
  retrieval: 记忆检索（路径、query、选中记忆及得分、耗时）
  prompt:    System Prompt 全文 + 构成块统计
  llm:       模型、成败、降级/重试、耗时、token 用量、回复摘要
"""
import json
import time
import uuid

from backend.core.config import config


def trace_enabled() -> bool:
    return config.TRACE_ENABLED


def trace_event(section: str, data: dict):
    """向当前请求的 trace 追加/合并一段数据。无请求上下文时静默跳过。"""
    if not trace_enabled():
        return
    try:
        from flask import g, has_request_context
        if not has_request_context():
            return
        if not hasattr(g, "_chat_trace"):
            g._chat_trace = {"_start": time.time()}
        if section in g._chat_trace and isinstance(g._chat_trace[section], dict):
            g._chat_trace[section].update(data)
        else:
            g._chat_trace[section] = data
    except Exception:
        pass


def get_trace() -> dict | None:
    try:
        from flask import g, has_request_context
        if has_request_context() and hasattr(g, "_chat_trace"):
            return g._chat_trace
    except Exception:
        pass
    return None


def save_trace(message_id: str, avatar_id: str, receiver_id: str):
    """请求结束时落库。失败不影响主流程。"""
    if not trace_enabled():
        return
    try:
        tr = get_trace()
        if not tr:
            return
        payload = {k: v for k, v in tr.items() if not k.startswith("_")}
        payload["total_ms"] = round((time.time() - tr.get("_start", time.time())) * 1000)

        from backend.db.database import get_db
        with get_db() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO chat_traces
                   (id, message_id, avatar_id, receiver_id, trace)
                   VALUES (?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, message_id, avatar_id, receiver_id,
                 json.dumps(payload, ensure_ascii=False)),
            )
            # 双上限修剪（仅 trace 运维数据）
            conn.execute(
                f"DELETE FROM chat_traces WHERE created_at < datetime('now', '-{int(config.TRACE_RETENTION_DAYS)} days')"
            )
            conn.execute(
                """DELETE FROM chat_traces WHERE id IN (
                     SELECT id FROM chat_traces ORDER BY created_at DESC LIMIT -1 OFFSET ?
                   )""",
                (int(config.TRACE_MAX_ROWS),),
            )
    except Exception as e:
        print(f"[TRACE] 落库失败（不影响对话）: {e}")
