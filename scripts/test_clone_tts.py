#!/usr/bin/env python3
"""
克隆音色 TTS 合成测试
用法（服务器上）: cd /app/shuangshen && python3 scripts/test_clone_tts.py [S_xxx]
不传参数则用 VOLC_SPEAKER_IDS 里第一个已训练成功的 speaker。
成功会把 MP3 写到 /tmp/clone_tts_test.mp3，可下载试听。
"""
import json
import sys
import urllib.request
import urllib.error
import base64
import uuid
from pathlib import Path

env_file = Path(__file__).resolve().parent.parent / ".env"
env = {}
for line in env_file.read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()

APP_ID = env.get("VOLC_APP_ID", "")
TOKEN  = env.get("VOLC_ACCESS_TOKEN") or env.get("VOLC_API_KEY", "")
SPEAKERS = [s.strip() for s in env.get("VOLC_SPEAKER_IDS", "").split(",") if s.strip()]
speaker = sys.argv[1] if len(sys.argv) > 1 else (SPEAKERS[0] if SPEAKERS else "")

if not speaker:
    print("❌ 没有 speaker_id"); sys.exit(1)

req_id = uuid.uuid4().hex
payload = json.dumps({
    "app":  {"appid": APP_ID, "token": TOKEN, "cluster": "volcano_icl"},
    "user": {"uid": req_id[:16]},
    "audio": {"voice_type": speaker, "encoding": "mp3", "speed_ratio": 1.0},
    "request": {"reqid": req_id, "text": "你好，这是声音复刻合成测试。", "operation": "query"},
}, ensure_ascii=False).encode()

req = urllib.request.Request(
    "https://openspeech.bytedance.com/api/v1/tts",
    data=payload,
    headers={"Authorization": f"Bearer;{TOKEN}", "Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    code = data.get("code", 0)
    if code == 3000:
        mp3 = base64.b64decode(data.get("data", ""))
        out = Path("/tmp/clone_tts_test.mp3")
        out.write_bytes(mp3)
        print(f"✅ 合成成功 speaker={speaker}  mp3={len(mp3)}字节 → {out}")
        print("   下载试听: scp ubuntu@服务器:/tmp/clone_tts_test.mp3 .")
    else:
        print(f"❌ code={code}: {data.get('message','')}")
except urllib.error.HTTPError as e:
    print(f"❌ HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
