#!/usr/bin/env python3
"""
CloudBase AI 网关测试：模型列表 + 对话 + Embedding
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

BASE = config.CLOUDBASE_BASE_URL.rstrip("/")
KEY  = config.CLOUDBASE_API_KEY
if not BASE or not KEY:
    print("❌ CLOUDBASE_BASE_URL / CLOUDBASE_API_KEY 未配置")
    sys.exit(1)

H = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get(path):
    req = urllib.request.Request(BASE + path, headers=H)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ① 模型列表
try:
    data = get("/models")
    ids = [m.get("id") for m in data.get("data", [])]
    print(f"✅ 模型列表({len(ids)}个): {', '.join(ids[:15])}{' …' if len(ids) > 15 else ''}")
except Exception as e:
    print(f"⚠️ 模型列表接口不可用（不影响使用）: {e}")

# ② 对话
try:
    data = post("/chat/completions", {
        "model": config.CLOUDBASE_MODEL,
        "messages": [{"role": "user", "content": "只回复两个字：成功"}],
        "max_tokens": 10,
    })
    reply = data["choices"][0]["message"]["content"]
    print(f"✅ 对话 OK  model={config.CLOUDBASE_MODEL}  reply={reply!r}")
except urllib.error.HTTPError as e:
    print(f"❌ 对话失败 HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
except Exception as e:
    print(f"❌ 对话失败: {e}")

# ③ Embedding
try:
    data = post("/embeddings", {
        "model": config.CLOUDBASE_EMBED_MODEL,
        "input": ["向量化测试文本"],
    })
    vec = data["data"][0]["embedding"]
    print(f"✅ Embedding OK  model={config.CLOUDBASE_EMBED_MODEL}  维度={len(vec)}")
except urllib.error.HTTPError as e:
    print(f"❌ Embedding 失败 HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
    print("   → 若模型名不对，请查控制台支持的 embedding 模型名，改 .env 的 CLOUDBASE_EMBED_MODEL")
except Exception as e:
    print(f"❌ Embedding 失败: {e}")
