"""
DeepSeek API Key 连通性测试
运行方式：python3 scripts/test_deepseek.py
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

# 读取 .env 中的 key（也可直接硬编码测试）
BASE_DIR = Path(__file__).resolve().parent.parent
env_file = BASE_DIR / ".env"
api_key = ""
for line in env_file.read_text().splitlines():
    if line.startswith("DEEPSEEK_API_KEY="):
        api_key = line.split("=", 1)[1].strip()
        break

if not api_key:
    print("❌ 未找到 DEEPSEEK_API_KEY，请检查 .env 文件")
    sys.exit(1)

print(f"🔑 使用 Key: {api_key[:12]}...")

payload = json.dumps({
    "model": "deepseek-chat",
    "messages": [
        {"role": "system", "content": "你是一个助手"},
        {"role": "user", "content": "用一句话介绍你自己"}
    ],
    "max_tokens": 100,
    "temperature": 0.7,
}).encode()

req = urllib.request.Request(
    "https://api.deepseek.com/v1/chat/completions",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    reply = data["choices"][0]["message"]["content"]
    print(f"✅ API Key 可用！\n模型回复：{reply}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"❌ HTTP {e.code}: {body}")
    sys.exit(1)
except Exception as e:
    print(f"❌ 连接失败: {e}")
    sys.exit(1)
