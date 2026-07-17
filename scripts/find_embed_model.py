#!/usr/bin/env python3
"""
CloudBase embedding 模型发现：枚举模型列表 + 试调候选模型名
用法（容器内）:
  docker cp scripts/find_embed_model.py shuangshen:/app/find_embed_model.py
  docker exec shuangshen python3 /app/find_embed_model.py
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
ENV_BASE = BASE.rsplit("/v1/ai/", 1)[0]
H = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}


def try_get(url):
    try:
        req = urllib.request.Request(url, headers=H)
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200, r.read().decode()[:1500]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:200]
    except Exception as e:
        return -1, str(e)


def try_embed(url, model):
    try:
        req = urllib.request.Request(url, data=json.dumps({"model": model, "input": ["测试"]}).encode(),
                                     headers=H, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        dim = len(data["data"][0]["embedding"])
        return f"✅ 成功！维度={dim}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        code = ""
        try:
            code = json.loads(body).get("code", "")
        except Exception:
            pass
        return f"{e.code} {code}"
    except Exception as e:
        return str(e)[:80]


print("═══ ① 模型列表枚举 ═══")
for url in [f"{BASE}/models", f"{BASE}/v1/models",
            f"{ENV_BASE}/v1/ai/hunyuan/models", f"{ENV_BASE}/v1/ai/hunyuan/v1/models",
            f"{ENV_BASE}/v1/ai/deepseek/v1/models"]:
    code, body = try_get(url)
    tag = "✅" if code == 200 else "❌"
    print(f"{tag} [{code}] {url}")
    if code == 200:
        try:
            ids = [m.get("id") for m in json.loads(body).get("data", [])]
            print(f"     模型: {ids}")
        except Exception:
            print(f"     {body[:300]}")

print("\n═══ ② embedding 候选模型试调 ═══")
models = ["hunyuan-embedding", "hunyuan-embedding-v2", "text-embedding-3",
          "text-embedding-v1", "bge-m3", "hy-embedding"]
urls = [f"{BASE}/embeddings",
        f"{ENV_BASE}/v1/ai/hunyuan/v1/embeddings",
        f"{ENV_BASE}/v1/ai/hunyuan/embeddings"]
for url in urls:
    print(f"\n▶ {url}")
    for m in models:
        r = try_embed(url, m)
        print(f"   {m:26s} → {r}")
        if r.startswith("✅"):
            print(f"\n🎉 找到可用组合：URL={url}  model={m}")
            print(f"   → 修改 .env: CLOUDBASE_EMBED_MODEL={m}")
            sys.exit(0)

print("\n未找到可用 embedding 模型。请到 CloudBase 控制台查看资源点套餐支持的模型列表。")
