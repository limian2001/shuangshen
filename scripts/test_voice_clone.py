#!/usr/bin/env python3
"""
声音复刻鉴权测试（只读，不消耗复刻次数）
用法（服务器上）: cd /app/shuangshen && python3 scripts/test_voice_clone.py
逐个 Resource-Id 查询第一个 speaker_id 的训练状态，
任意一个返回 200 即说明鉴权和授权都正常。
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 手动加载 .env（不依赖 flask 启动流程）
env_file = Path(__file__).resolve().parent.parent / ".env"
env = {}
for line in env_file.read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()

APP_ID   = env.get("VOLC_APP_ID", "")
TOKEN    = env.get("VOLC_ACCESS_TOKEN") or env.get("VOLC_API_KEY", "")
SPEAKERS = [s.strip() for s in env.get("VOLC_SPEAKER_IDS", "").split(",") if s.strip()]

if not APP_ID or not TOKEN or not SPEAKERS:
    print("❌ .env 缺少 VOLC_APP_ID / VOLC_ACCESS_TOKEN / VOLC_SPEAKER_IDS")
    sys.exit(1)

print(f"appid={APP_ID}  token={TOKEN[:6]}***  speakers={len(SPEAKERS)}个")
payload = json.dumps({"appid": APP_ID, "speaker_id": SPEAKERS[0]}).encode()

STATUS_MAP = {0: "NotFound", 1: "Training", 2: "Success", 3: "Failed", 4: "Active"}
ok = False
for rid in ["volc.megatts.voiceclone", "seed-icl-1.0", "seed-icl-2.0"]:
    req = urllib.request.Request(
        "https://openspeech.bytedance.com/api/v1/mega_tts/status",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer;{TOKEN}",
            "Resource-Id": rid,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        s = data.get("status")
        print(f"✅ [{rid}] 鉴权成功  speaker={SPEAKERS[0]}  status={s}({STATUS_MAP.get(s,'?')})")
        print(f"   → 建议把 VOLC_CLONE_RESOURCE_ID={rid} 写入 .env 固定使用")
        ok = True
        break
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:150]
        print(f"❌ [{rid}] HTTP {e.code}: {body}")
    except Exception as e:
        print(f"❌ [{rid}] {type(e).__name__}: {e}")

if ok:
    print("\n全部就绪：可以在小程序里上传声音样本了。")
else:
    print("\n所有 Resource-Id 均失败：请检查控制台声音复刻服务状态，把上面的错误发我。")
