#!/usr/bin/env python3
"""
阿里云百炼 CosyVoice 声音复刻 + 合成 自测（不依赖 .env，可直接传参）

用法（服务器上，无需先改 .env）：
  python3 scripts/test_aliyun_voice.py <API_KEY> <WORKSPACE_ID> [音频URL]

不传音频 URL 时用阿里云官方公开样例音频测试。
测试完会自动删除临时音色，不占配额。
"""
import base64
import json
import sys
import urllib.request
import urllib.error

if len(sys.argv) < 3:
    print(__doc__)
    sys.exit(1)

KEY   = sys.argv[1]
WS    = sys.argv[2]
AUDIO = sys.argv[3] if len(sys.argv) > 3 else \
    "https://dashscope.oss-cn-beijing.aliyuncs.com/samples/audio/sensevoice/rich_text_example_1.wav"
MODEL = "cosyvoice-v3.5-flash"
BASE  = f"https://{WS}.cn-beijing.maas.aliyuncs.com"
H     = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def post(path, body, timeout=90):
    req = urllib.request.Request(BASE + path, data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers=H, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:600]


print(f"workspace = {WS}")
print(f"model     = {MODEL}")
print(f"样本音频  = {AUDIO}\n")

# ① 复刻音色（免费）
print("── ① 声音复刻 ──")
code, data = post("/api/v1/services/audio/tts/customization", {
    "model": "voice-enrollment",
    "input": {"action": "create_voice", "target_model": MODEL,
              "prefix": "yjtest", "url": AUDIO},
})
if code != 200 or not isinstance(data, dict):
    print(f"❌ 复刻失败 HTTP {code}: {data}")
    sys.exit(1)
voice_id = (data.get("output") or {}).get("voice_id", "")
if not voice_id:
    print(f"❌ 未返回 voice_id: {json.dumps(data, ensure_ascii=False)[:400]}")
    sys.exit(1)
print(f"✅ 复刻成功  voice_id = {voice_id}")
print(f"   （复刻本身免费；账号配额 1000 个）\n")

# ② 用复刻音色合成
print("── ② 语音合成 ──")
code, data = post("/api/v1/services/aigc/multimodal-generation/generation", {
    "model": MODEL,
    "input": {"text": "你好，这是声音复刻的合成测试，如果听到的是样本里的音色就说明成功了。",
              "voice": voice_id},
    "parameters": {"format": "mp3"},
})
audio_bytes = b""
if code == 200 and isinstance(data, dict):
    out = (data.get("output") or {})
    au  = out.get("audio") or {}
    print(f"   响应结构: output.keys={list(out.keys())} audio.keys={list(au.keys())}")
    if au.get("url"):
        with urllib.request.urlopen(au["url"], timeout=60) as r:
            audio_bytes = r.read()
        print("   音频来源: audio.url（下载）")
    elif au.get("data"):
        audio_bytes = base64.b64decode(au["data"])
        print("   音频来源: audio.data（base64）")
    if audio_bytes:
        with open("/tmp/aliyun_tts_test.mp3", "wb") as f:
            f.write(audio_bytes)
        print(f"✅ 合成成功  {len(audio_bytes)} 字节 → /tmp/aliyun_tts_test.mp3")
        print("   取出试听: docker cp shuangshen:/tmp/aliyun_tts_test.mp3 .  （容器内运行时）")
    else:
        print(f"❌ 未取到音频: {json.dumps(data, ensure_ascii=False)[:500]}")
else:
    print(f"❌ 合成失败 HTTP {code}: {data}")

# ③ 清理测试音色
print("\n── ③ 清理测试音色 ──")
code, data = post("/api/v1/services/audio/tts/customization", {
    "model": "voice-enrollment",
    "input": {"action": "delete_voice", "voice_id": voice_id},
}, timeout=30)
print(f"{'✅' if code == 200 else '⚠️'} 删除返回 HTTP {code}")

print("\n" + ("🎉 全部通过，可以更新 .env 并部署" if audio_bytes else "⚠️ 合成环节需排查，把上面输出发我"))
