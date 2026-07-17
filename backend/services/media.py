from __future__ import annotations
"""
媒体服务 — 语音识别(STT)、图像理解(Vision)、语音合成/声音克隆(TTS)

STT  : 腾讯云 SentenceRecognition (TC3-HMAC-SHA256)
Vision: Anthropic Claude (claude-3-5-sonnet，已有 API Key)
TTS  : 火山引擎 TTS + 声音复刻 (CosyVoice-style)
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.core.config import config


# ──────────────────────────────────────────────────────────────────────
# STT — 腾讯云 SentenceRecognition
# ──────────────────────────────────────────────────────────────────────

def stt_recognize(audio_bytes: bytes, language: str = "zh", filename: str = "") -> str:
    """
    语音转文字。
    language: "zh" | "yue" | "en"
    filename:  用于推断音频格式（webm/mp4/wav/mp3/ogg）
    Returns 识别文本；失败抛 RuntimeError。
    """
    sid = config.TENCENT_ASR_SECRET_ID
    skey = config.TENCENT_ASR_SECRET_KEY
    if not sid or not skey:
        raise RuntimeError(
            "腾讯云 ASR 未配置，请在 .env 中填入 TENCENT_ASR_SECRET_ID / TENCENT_ASR_SECRET_KEY"
        )

    engine_map = {"zh": "16k_zh", "yue": "16k_yue", "en": "16k_en"}
    engine = engine_map.get(language, "16k_zh")

    # SentenceRecognition 的 VoiceFormat 是字符串：
    # wav / pcm / ogg-opus / speex / silk / mp3 / m4a / aac / amr
    fn = (filename or "").lower()
    if fn.endswith(".wav"):
        voice_fmt = "wav"
    elif fn.endswith(".mp3"):
        voice_fmt = "mp3"
    elif fn.endswith(".ogg"):
        voice_fmt = "ogg-opus"
    elif fn.endswith(".m4a"):
        voice_fmt = "m4a"
    elif fn.endswith(".aac") or fn.endswith(".mp4"):
        voice_fmt = "aac"
    else:
        voice_fmt = "ogg-opus"   # webm/opus 尽力按 ogg-opus 处理

    payload_obj = {
        "EngSerViceType": engine,      # 注意：官方参数名就是这个拼写
        "SourceType": 1,
        "VoiceFormat": voice_fmt,
        "UsrAudioKey": _rand_id(),
        "Data": base64.b64encode(audio_bytes).decode(),
        "DataLen": len(audio_bytes),
        "ProjectId": 0,
        "SubServiceType": 2,
    }
    payload_str = json.dumps(payload_obj, separators=(",", ":"))

    resp = _tc3_request(
        service="asr",
        host="asr.tencentcloudapi.com",
        action="SentenceRecognition",
        version="2019-06-14",
        region=config.TENCENT_ASR_REGION,
        secret_id=sid,
        secret_key=skey,
        payload_str=payload_str,
    )
    # 返回格式: {"Response": {"Result": "...", "RequestId": "..."}}
    # 注意：腾讯云出错时 HTTP 仍是 200，错误在 Response.Error 里，必须显式检查
    r = resp.get("Response", {})
    if "Error" in r:
        err = r["Error"]
        raise RuntimeError(f"腾讯ASR错误 {err.get('Code','')}: {err.get('Message','')}")
    return r.get("Result", "")


def _tc3_request(
    service: str, host: str, action: str, version: str, region: str,
    secret_id: str, secret_key: str, payload_str: str,
) -> dict:
    """通用腾讯云 TC3-HMAC-SHA256 签名 + HTTP POST"""
    timestamp = int(time.time())
    date = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")

    # Canonical Request
    canonical_headers = f"content-type:application/json\nhost:{host}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload_str.encode()).hexdigest()
    canonical_request = "\n".join([
        "POST", "/", "",
        canonical_headers, signed_headers, hashed_payload,
    ])

    # String to Sign
    algorithm = "TC3-HMAC-SHA256"
    credential_scope = f"{date}/{service}/tc3_request"
    hashed_cr = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = "\n".join([algorithm, str(timestamp), credential_scope, hashed_cr])

    # Derived Signing Key
    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    secret_date    = _hmac(f"TC3{secret_key}".encode(), date)
    secret_service = _hmac(secret_date, service)
    secret_signing = _hmac(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    authorization = (
        f"{algorithm} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    req = urllib.request.Request(
        f"https://{host}",
        data=payload_str.encode(),
        headers={
            "Content-Type": "application/json",
            "Host": host,
            "Authorization": authorization,
            "X-TC-Action": action,
            "X-TC-Version": version,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": region,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"腾讯云 ASR 错误 {e.code}: {body}") from e


# ──────────────────────────────────────────────────────────────────────
# TTS — 火山引擎 seed-tts-2.0（WebSocket 双向流 + X-Api-* 认证）
# 声音复刻 — V3 接口（/api/v3/tts/voice_clone，X-Api-Key 认证）
# ──────────────────────────────────────────────────────────────────────

import asyncio
import io as _io
import struct

# TTS WebSocket 端点（新版 seed-tts-2.0）
VOLC_TTS_WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"

# 声音复刻 V3 端点（X-Api-Key 认证，与 TTS 同一个 API Key）
VOLC_CLONE_UPLOAD_URL = "https://openspeech.bytedance.com/api/v3/tts/voice_clone"
VOLC_CLONE_STATUS_URL = "https://openspeech.bytedance.com/api/v3/tts/get_voice"

# 默认音色（seed-tts-2.0 新版音色名）
DEFAULT_VOICE_MAP = {
    "zh":  "zh_female_vv_uranus_bigtts",       # 普通话女声
    "yue": "zh_female_vv_uranus_bigtts",        # 粤语暂用普通话音色
    "en":  "en_female_vv_uranus_bigtts",        # 英文女声
}


# ──────────────────────────────────────────────────────────────────────
# WebSocket TTS 内部实现
# ──────────────────────────────────────────────────────────────────────
_CONN_EVENTS = frozenset({1, 2, 50, 51, 52})   # StartConnection/FinishConnection及对应服务端事件


def _build_ws_msg(event: int, payload: bytes, session_id: str = "") -> bytes:
    """构造 seed-tts-2.0 WebSocket 二进制帧（FullClientRequest + WithEvent）"""
    # Header: version=1, header_size=1(4字节), msg_type=1(FullClientRequest),
    #         flag=4(WithEvent), serialization=JSON, compression=None
    buf = _io.BytesIO()
    buf.write(bytes([0x11, 0x14, 0x10, 0x00]))   # (1<<4)|1, (1<<4)|4, (1<<4)|0, 0x00
    buf.write(struct.pack(">i", event))
    if event not in _CONN_EVENTS:
        sid = session_id.encode("utf-8")
        buf.write(struct.pack(">I", len(sid)))
        buf.write(sid)
    buf.write(struct.pack(">I", len(payload)))
    buf.write(payload)
    return buf.getvalue()


def _parse_ws_msg(data: bytes):
    """解析服务端帧，返回 (msg_type, event, audio_bytes_or_None)"""
    if len(data) < 4:
        return 0, None, None, b""
    mt   = (data[1] >> 4) & 0x0F   # msg_type: 9=FullServerResponse, 11=AudioOnlyServer, 15=Error
    flag = data[1] & 0x0F           # 1=PositiveSeq, 2=LastNoSeq, 3=NegativeSeq, 4=WithEvent
    off  = 4

    event = None
    if flag == 4:   # WithEvent
        if off + 4 > len(data):
            return mt, None, None, b""
        event = struct.unpack(">i", data[off:off + 4])[0]
        off += 4
        if event not in _CONN_EVENTS:
            if off + 4 <= len(data):
                sid_len = struct.unpack(">I", data[off:off + 4])[0]
                off += 4 + sid_len
        else:
            # ConnectionStarted 等：服务端带 connect_id
            if mt == 9 and off + 4 <= len(data):
                cid_len = struct.unpack(">I", data[off:off + 4])[0]
                off += 4 + cid_len
    elif flag in (1, 3):   # PositiveSeq / NegativeSeq → 跳过序列号
        off += 4

    payload = b""
    if off + 4 <= len(data):
        plen = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
        if plen and off + plen <= len(data):
            payload = data[off:off + plen]

    audio = payload if mt == 11 and payload else None
    return mt, event, audio, payload


async def _tts_ws_async(text: str, speaker: str, speed_ratio: float,
                        api_key: str, resource_id: str = "seed-tts-2.0") -> bytes:
    """V3 WebSocket 双向流 TTS，返回 MP3 字节。
    resource_id: seed-tts-2.0(默认音色) / seed-icl-2.0 / seed-icl-1.0(克隆音色)"""
    try:
        import websockets
    except ImportError:
        raise RuntimeError(
            "缺少 websockets 库，请在服务器运行：pip install websockets --break-system-packages"
        )

    req_id     = _rand_id()
    session_id = _rand_id()

    # 正确的认证头：X-Api-Key（不是 X-Api-Access-Key，也不需要 X-Api-App-Id）
    headers = {
        "X-Api-Key":         api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Connect-Id":  _rand_id(),
    }

    speech_rate = max(-50, min(100, round((speed_ratio - 1.0) * 100)))

    def _json(obj) -> bytes:
        return json.dumps(obj, ensure_ascii=False).encode()

    audio_params = {"format": "mp3", "sample_rate": 24000, "speech_rate": speech_rate}
    req_params   = {"speaker": speaker, "audio_params": audio_params}
    base_body    = {"user": {"uid": req_id}, "namespace": "BidirectionalTTS"}

    ws = await asyncio.wait_for(
        websockets.connect(VOLC_TTS_WS_URL, additional_headers=headers,
                           max_size=16 * 1024 * 1024),
        timeout=15,
    )
    try:
        # 1. StartConnection
        await ws.send(_build_ws_msg(1, b"{}"))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        _, ev, _, _pl = _parse_ws_msg(raw)
        if ev != 50:
            raise RuntimeError(f"TTS: 未收到 ConnectionStarted（event={ev} payload={_pl[:200]!r}）")

        # 2. StartSession
        await ws.send(_build_ws_msg(
            100,
            _json({**base_body, "event": 100, "req_params": req_params}),
            session_id,
        ))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        _, ev, _, _pl = _parse_ws_msg(raw)
        if ev != 150:
            raise RuntimeError(f"TTS: 未收到 SessionStarted（event={ev} payload={_pl[:200]!r}）")

        # 3. TaskRequest + FinishSession（告知文本已完整）
        await ws.send(_build_ws_msg(
            200,
            _json({**base_body, "event": 200,
                   "req_params": {**req_params, "text": text}}),
            session_id,
        ))
        await ws.send(_build_ws_msg(102, b"{}", session_id))

        # 4. 收集音频帧
        chunks = []
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            mt, ev, audio, pl = _parse_ws_msg(raw)
            if audio:
                chunks.append(audio)
            if mt == 9 and ev in (152, 359):   # SessionFinished / TTSEnded
                break
            if mt == 15:
                raise RuntimeError(f"TTS 服务器错误帧: {pl[:300].decode('utf-8', errors='replace') if pl else raw[:100]!r}")

        # 5. FinishConnection（best-effort）
        try:
            await ws.send(_build_ws_msg(2, b"{}"))
        except Exception:
            pass

        return b"".join(chunks)
    finally:
        await ws.close()


def tts_synthesize(
    text: str,
    voice_id: Optional[str] = None,
    language: str = "zh",
    speed: float = 1.0,
    volume: float = 1.0,
) -> bytes:
    """
    文字合成语音。
    - voice_id 有值（克隆声音）→ ICL HTTP 接口（volcano_icl cluster）
    - voice_id 为 None           → seed-tts-2.0 WebSocket 默认音色
    返回 MP3 字节；失败抛 RuntimeError。
    """
    api_key = config.VOLC_API_KEY
    if not api_key:
        raise RuntimeError("火山引擎 TTS 未配置，请在 .env 中填入 VOLC_API_KEY")

    text = text[:500]

    if voice_id:
        # 使用声音复刻的 speaker_id 合成（自动探测可用路径）
        return _tts_clone_multi(text, voice_id, speed)

    # 默认音色走 seed-tts-2.0 WebSocket
    speaker = DEFAULT_VOICE_MAP.get(language, DEFAULT_VOICE_MAP["zh"])
    return asyncio.run(_tts_ws_async(text, speaker, speed, api_key))


# 克隆音色合成的可用路径（首次成功后缓存，后续直达）
_CLONE_TTS_WINNER: Optional[str] = None


def _tts_clone_multi(text: str, voice_id: str, speed: float) -> bytes:
    """
    克隆音色合成（声音复刻 2.0）：V3 WebSocket + X-Api-Resource-Id: seed-icl-2.0。
    与默认音色走同一 WS 端点，仅 Resource-Id 与 speaker 不同。
    实测（2026-07）：本账号 API Key 可通过 seed-icl-2.0 认证；
    V1 HTTP 合成与 seed-icl-1.0 均无授权，已移除。
    """
    global _CLONE_TTS_WINNER
    _CLONE_TTS_WINNER = "icl2-key"
    return asyncio.run(_tts_ws_async(
        text, voice_id, speed, config.VOLC_API_KEY, resource_id="seed-icl-2.0"
    ))


# ──────────────────────────────────────────────────────────────────────
# 声音复刻（V3 接口，X-Api-Key 认证）
# ──────────────────────────────────────────────────────────────────────

def _volc_headers(app_id: str, api_key: str, extra: dict | None = None) -> dict:
    """构造火山引擎新版 X-Api-* 认证头"""
    h = {
        "X-Api-App-Id":     app_id,
        "X-Api-Access-Key": api_key,
        "X-Api-Request-Id": _rand_id(),
    }
    if extra:
        h.update(extra)
    return h


def voice_clone_upload(speaker_id: str, sample_bytes: bytes, filename: str,
                       language: int = 0, demo_text: str = "") -> str:
    """
    声音复刻 V3 训练接口：POST /api/v3/tts/voice_clone
    上传即训练，返回 speaker_id。
    认证：X-Api-Key（与 TTS 相同的 API Key）。
    音频格式支持：wav / mp3 / ogg / m4a / aac / pcm（webm 不支持，前端已转 wav）。
    speaker_id: 控制台购买音色获得的 S_ 开头 ID（配置在 VOLC_SPEAKER_IDS）。
    """
    api_key = config.VOLC_API_KEY
    if not api_key:
        raise RuntimeError("火山引擎 TTS 未配置")

    _fn = filename.lower()
    audio_fmt = ("mp3" if _fn.endswith(".mp3") else
                 "m4a" if _fn.endswith(".m4a") else
                 "aac" if _fn.endswith(".aac") else
                 "ogg" if _fn.endswith(".ogg") else
                 "pcm" if _fn.endswith(".pcm") else
                 "wav" if _fn.endswith(".wav") else "")

    audio_obj: dict = {"data": base64.b64encode(sample_bytes).decode()}
    if audio_fmt:   # 文档：pcm/m4a 必传，其余可不指定
        audio_obj["format"] = audio_fmt

    body: dict = {
        "speaker_id": speaker_id,
        "audio":      audio_obj,
        "language":   language,   # 0=中文
    }
    if demo_text:
        body["extra_params"] = {"demo_text": demo_text[:300]}

    headers = {
        "Content-Type":     "application/json",
        "X-Api-Key":        api_key,
        "X-Api-Request-Id": _rand_id(),
    }
    req = urllib.request.Request(VOLC_CLONE_UPLOAD_URL,
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data.get("speaker_id", speaker_id)
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"声音上传错误 {e.code}: {body_err}") from e


def voice_clone_status(speaker_id: str) -> dict:
    """
    查询声音克隆训练状态。
    API status 枚举: 0=NotFound 1=Training 2=Success 3=Failed 4=Active
    Returns: {"status": "training"|"success"|"failed", "voice_id": "..."}
    """
    app_id  = config.VOLC_APP_ID
    api_key = config.VOLC_API_KEY

    headers = {
        "Content-Type":     "application/json",
        "X-Api-Key":        config.VOLC_API_KEY,
        "X-Api-Request-Id": _rand_id(),
    }
    req = urllib.request.Request(VOLC_CLONE_STATUS_URL,
                                 data=json.dumps({"speaker_id": speaker_id}).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        # status: 0=NotFound, 1=Training, 2=Success, 3=Failed, 4=Active（2/4 可合成）
        s = data.get("status", 0)
        if s in (2, 4):
            return {"status": "success", "voice_id": speaker_id}
        elif s == 3:
            return {"status": "failed", "voice_id": ""}
        else:
            return {"status": "training", "voice_id": ""}
    except urllib.error.HTTPError:
        return {"status": "failed", "voice_id": ""}




# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _rand_id() -> str:
    import uuid
    return uuid.uuid4().hex
