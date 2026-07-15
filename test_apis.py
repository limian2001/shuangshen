#!/usr/bin/env python3
"""
接口连通性测试脚本
服务器上运行方式：
    cd /app/shuangshen
    python3 test_apis.py

依赖：pip install websockets --break-system-packages
"""

import sys
import os
import struct
import json

# 项目根目录
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"


# ──────────────────────────────────────────────────────────────────────
# 工具：生成 1 秒 16kHz 静音 WAV
# ──────────────────────────────────────────────────────────────────────
def _make_silence_wav(duration_sec: float = 1.0, sample_rate: int = 16000) -> bytes:
    num_samples = int(sample_rate * duration_sec)
    data_bytes = b"\x00\x00" * num_samples  # 16-bit PCM 静音
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data_bytes))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)          # chunk size
        + struct.pack("<H", 1)           # PCM
        + struct.pack("<H", 1)           # mono
        + struct.pack("<I", sample_rate)
        + struct.pack("<I", sample_rate * 2)  # byte rate
        + struct.pack("<H", 2)           # block align
        + struct.pack("<H", 16)          # bits per sample
        + b"data"
        + struct.pack("<I", len(data_bytes))
    )
    return header + data_bytes


# ──────────────────────────────────────────────────────────────────────
# 测试 1：TTS（火山引擎 seed-tts-2.0 WebSocket）
# ──────────────────────────────────────────────────────────────────────
def test_tts():
    print("\n" + "=" * 55)
    print("测试 1：TTS 火山引擎 seed-tts-2.0")
    print("=" * 55)

    app_id  = os.getenv("VOLC_APP_ID", "")
    api_key = os.getenv("VOLC_API_KEY", "")
    if not app_id or not api_key:
        print(f"{FAIL} VOLC_APP_ID / VOLC_API_KEY 未配置，跳过")
        return False

    print(f"  App ID : {app_id}")
    print(f"  API Key: {api_key[:8]}…")
    print("  正在连接 WebSocket 合成语音（约 3~8 秒）…")

    try:
        from backend.services.media import tts_synthesize
        audio = tts_synthesize("你好，这是一段测试语音，言己正在运行。", language="zh")
    except Exception as e:
        print(f"{FAIL} TTS 调用失败: {e}")
        return False

    if not audio or len(audio) < 1000:
        print(f"{FAIL} TTS 返回数据异常，大小: {len(audio) if audio else 0} bytes")
        return False

    out_path = "/tmp/test_tts_output.mp3"
    with open(out_path, "wb") as f:
        f.write(audio)

    print(f"{PASS} TTS 成功！音频大小: {len(audio):,} bytes")
    print(f"  已保存到 {out_path}（用 ffplay 或 scp 到本机播放验证）")
    print(f"  验证命令: ffplay -nodisp -autoexit {out_path}")
    return True


# ──────────────────────────────────────────────────────────────────────
# 测试 2：ASR（腾讯云 SentenceRecognition）
# ──────────────────────────────────────────────────────────────────────
def test_stt():
    print("\n" + "=" * 55)
    print("测试 2：STT 腾讯云 ASR")
    print("=" * 55)

    sid  = os.getenv("TENCENT_ASR_SECRET_ID", "")
    skey = os.getenv("TENCENT_ASR_SECRET_KEY", "")
    if not sid or not skey:
        print(f"{WARN} TENCENT_ASR_SECRET_ID / SECRET_KEY 未配置，跳过")
        print("  获取地址：https://console.cloud.tencent.com/cam/capi")
        return False

    print(f"  Secret ID: {sid[:8]}…")
    print("  使用 1 秒静音 WAV 测试 API 连通性…")

    wav_bytes = _make_silence_wav(duration_sec=1.0)
    try:
        from backend.services.media import stt_recognize
        text = stt_recognize(wav_bytes, language="zh")
        # 静音输入：返回空字符串是正常的，能走到这里说明鉴权和网络都通了
        print(f"{PASS} STT 连通成功！识别结果: '{text}'（静音正常为空字符串）")
        return True
    except Exception as e:
        err = str(e)
        if "AuthFailure" in err or "InvalidSecretKey" in err:
            print(f"{FAIL} 鉴权失败，请检查 SECRET_ID / SECRET_KEY: {err}")
        elif "ResourceNotFound" in err:
            print(f"{FAIL} 地域或服务未开通: {err}")
        else:
            print(f"{FAIL} STT 调用失败: {err}")
        return False


# ──────────────────────────────────────────────────────────────────────
# 测试 3：声音复刻接口连通性（mega_tts）
# ──────────────────────────────────────────────────────────────────────
def test_voice_clone():
    print("\n" + "=" * 55)
    print("测试 3：声音复刻 mega_tts（状态查询接口）")
    print("=" * 55)

    app_id  = os.getenv("VOLC_APP_ID", "")
    api_key = os.getenv("VOLC_API_KEY", "")
    if not app_id or not api_key:
        print(f"{FAIL} VOLC_APP_ID / VOLC_API_KEY 未配置，跳过")
        return False

    print("  用虚拟 speaker_id 查询状态（测试鉴权和连通）…")
    try:
        from backend.services.media import voice_clone_status
        result = voice_clone_status("test_dummy_speaker_000")
        # 返回任何结构都说明接口连通
        print(f"{PASS} 声音复刻接口连通，返回: {result}")
        return True
    except Exception as e:
        err = str(e)
        if "401" in err or "403" in err or "auth" in err.lower():
            print(f"{FAIL} 鉴权失败: {err}")
        elif "404" in err:
            # 404 说明接口存在，只是 speaker 不存在 — 也算通了
            print(f"{PASS} 接口连通（speaker 不存在 → 404，符合预期）")
            return True
        else:
            print(f"{FAIL} 接口异常: {err}")
        return False


# ──────────────────────────────────────────────────────────────────────
# 测试 4：DeepSeek LLM 连通
# ──────────────────────────────────────────────────────────────────────
def test_deepseek():
    print("\n" + "=" * 55)
    print("测试 4：DeepSeek LLM（对话生成）")
    print("=" * 55)

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        print(f"{FAIL} DEEPSEEK_API_KEY 未配置，跳过")
        return False

    print(f"  API Key: {api_key[:8]}…")
    try:
        from backend.services.llm_provider import llm
        reply = llm.chat(
            system_prompt="你是测试机器人，请用一句话回答。",
            messages=[{"role": "user", "content": "你好，测试连通"}],
            max_tokens=50,
            temperature=0.5,
        )
        print(f"{PASS} DeepSeek 连通！回复: {reply[:80]}")
        return True
    except Exception as e:
        print(f"{FAIL} DeepSeek 调用失败: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n言己接口连通性测试")
    print("=" * 55)

    results = {
        "TTS  （火山引擎）":  test_tts(),
        "STT  （腾讯ASR）":   test_stt(),
        "声音复刻（火山引擎）": test_voice_clone(),
        "LLM  （DeepSeek）":  test_deepseek(),
    }

    print("\n" + "=" * 55)
    print("汇总")
    print("=" * 55)
    all_ok = True
    for name, ok in results.items():
        status = PASS if ok else (FAIL if ok is False else WARN)
        print(f"  {status}  {name}")
        if ok is False:
            all_ok = False

    if all_ok:
        print("\n全部通过，可以部署。")
    else:
        print("\n有接口未通过，请按上方提示修复后重试。")
