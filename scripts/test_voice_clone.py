#!/usr/bin/env python3
"""
声音复刻 V3 鉴权测试（只读，不消耗训练次数）
用法（服务器/容器内）: python3 scripts/test_voice_clone.py
调用 /api/v3/tts/get_voice 查询第一个 speaker_id 的训练状态。
"""
import json
import sys
import uuid
import urllib.request
import urllib.error
from pathlib import Path

env_file = Path(__file__).resolve().parent.parent / ".env"
env = {}
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
else:
    import os
    env = dict(os.environ)   # 容器内用环境变量

API_KEY  = env.get("VOLC_API_KEY", "")
SPEAKERS = [s.strip() for s in env.get("VOLC_SPEAKER_IDS", "").split(",") if s.strip()]

if not API_KEY or not SPEAKERS:
    print("❌ 缺少 VOLC_API_KEY / VOLC_SPEAKER_IDS")
    sys.exit(1)

STATUS_MAP = {0: "NotFound", 1: "Training", 2: "Success", 3: "Failed", 4: "Active"}
speaker = sys.argv[1] if len(sys.argv) > 1 else SPEAKERS[0]

req = urllib.request.Request(
    "https://openspeech.bytedance.com/api/v3/tts/get_voice",
    data=json.dumps({"speaker_id": speaker}).encode(),
    headers={
        "Content-Type": "application/json",
        "X-Api-Key": API_KEY,
        "X-Api-Request-Id": uuid.uuid4().hex,
    },
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    s = data.get("status")
    print(f"✅ V3 鉴权成功  speaker={speaker}  status={s}({STATUS_MAP.get(s,'?')})")
    print(f"   剩余训练次数: {data.get('available_training_times', '?')}")
    if data.get("demo_audio"):
        print(f"   试听: {data['demo_audio'][:80]}…")
except urllib.error.HTTPError as e:
    print(f"❌ HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
