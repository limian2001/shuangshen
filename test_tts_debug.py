#!/usr/bin/env python3
"""
TTS 连接诊断脚本
在 Docker 容器内运行：docker exec shuangshen python3 /app/test_tts_debug.py
"""
import asyncio
import sys

print(f"Python: {sys.version}")

try:
    import websockets
    print(f"websockets: {websockets.__version__}")
except ImportError:
    print("ERROR: websockets 未安装")
    sys.exit(1)


async def test(label, url, headers):
    print(f"\n--- {label} ---")
    print(f"URL: {url}")
    print(f"Headers: {dict(headers)}")
    try:
        async with websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=10,
        ) as ws:
            print("✅ 连接成功！")
            return True
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")
        return False


async def main():
    v3_url  = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
    # 正确 auth：X-Api-Key（官方示例确认）
    import os
    v3_headers = {
        "X-Api-Key":         os.getenv("VOLC_API_KEY", ""),   # 从环境变量读，不硬编码
        "X-Api-Resource-Id": "seed-tts-2.0",
        "X-Api-Connect-Id":  "dbg-conn-001",
    }
    await test("v3 seed-tts-2.0 (X-Api-Key)", v3_url, v3_headers)

    # 老版 v1，BigTTS 凭证（Bearer auth）
    await test(
        "v1 BigTTS (Bearer auth)",
        "wss://openspeech.bytedance.com/api/v1/tts/ws_binary",
        {"Authorization": "Bearer;QNXzTTotKSY1xsbUgk48sncLOp1buvVR"},
    )

    print("\n=== 诊断完成 ===")


asyncio.run(main())
