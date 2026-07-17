#!/usr/bin/env python3
"""
CloudBase AI 网关测试（走生产代码路径）
用法（容器内）:
  docker cp scripts/test_cloudbase.py shuangshen:/app/test_cloudbase.py
  docker exec shuangshen python3 /app/test_cloudbase.py
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.core.config import config
from backend.services.llm_provider import llm

BASE = config.CLOUDBASE_BASE_URL.rstrip("/")
KEY  = config.CLOUDBASE_API_KEY
H = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}

# ① 对话（生产路径 llm.chat，带完整诊断）
print(f"── 对话测试 model={config.CLOUDBASE_MODEL} ──")
try:
    payload = json.dumps({
        "model": config.CLOUDBASE_MODEL,
        "messages": [{"role": "system", "content": "你是测试助手"},
                     {"role": "user", "content": "请回复：测试成功"}],
        "max_tokens": 100,
    }).encode()
    req = urllib.request.Request(f"{BASE}/chat/completions", data=payload, headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    msg = data["choices"][0]["message"]
    fin = data["choices"][0].get("finish_reason")
    print(f"✅ content={msg.get('content')!r}")
    if msg.get("reasoning_content"):
        print(f"   (思维链模型，reasoning={msg['reasoning_content'][:60]!r}…)")
    print(f"   finish_reason={fin}  usage={data.get('usage')}")
except Exception as e:
    print(f"❌ 对话失败: {e}")

# ② 生产路径 llm.chat
try:
    reply = llm.chat(system_prompt="你是测试助手", messages=[{"role": "user", "content": "回复：OK"}], max_tokens=50)
    print(f"✅ llm.chat OK  reply={reply!r}")
except Exception as e:
    print(f"❌ llm.chat 失败: {e}")

# ③ Embedding（生产路径 llm.embed，自动走 hunyuan provider）
print(f"\n── Embedding 测试 provider={config.CLOUDBASE_EMBED_PROVIDER} model={config.CLOUDBASE_EMBED_MODEL} ──")
vec = llm.embed("向量化测试文本")
if vec:
    print(f"✅ llm.embed OK  维度={len(vec)}")
else:
    print("❌ llm.embed 返回 None（上方应有 ALERT 日志说明原因）")
