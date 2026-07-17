#!/usr/bin/env python3
"""
克隆音色 TTS 合成测试（走生产代码路径，自动探测可用接口）
用法（服务器上）: cd /app/shuangshen && python3 scripts/test_clone_tts.py [S_xxx]
成功会把 MP3 写到 /tmp/clone_tts_test.mp3，可下载试听。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.config import config
from backend.services import media

speakers = [s.strip() for s in config.VOLC_SPEAKER_IDS.split(",") if s.strip()]
speaker = sys.argv[1] if len(sys.argv) > 1 else (speakers[0] if speakers else "")
if not speaker:
    print("❌ 没有 speaker_id（VOLC_SPEAKER_IDS 为空且未传参数）")
    sys.exit(1)

print(f"测试克隆合成 speaker={speaker} …")
try:
    audio = media.tts_synthesize("你好，这是声音复刻合成测试，听到自己的声音就说明成功了。",
                                 voice_id=speaker)
    out = Path("/tmp/clone_tts_test.mp3")
    out.write_bytes(audio)
    print(f"✅ 合成成功  {len(audio)} 字节 → {out}")
    print(f"   使用路径: {media._CLONE_TTS_WINNER}")
    print("   下载试听: scp ubuntu@服务器IP:/tmp/clone_tts_test.mp3 .")
except Exception as e:
    print(f"❌ 全部路径失败:\n{e}")
