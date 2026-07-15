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

def stt_recognize(audio_bytes: bytes, language: str = "zh") -> str:
    """
    语音转文字。
    language: "zh" | "yue" | "en"
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

    payload_obj = {
        "EngineModelType": engine,
        "ChannelNum": 1,
        "ResTextFormat": 0,
        "SourceType": 1,
        "Data": base64.b64encode(audio_bytes).decode(),
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
    # 腾讯云返回格式: {"Response": {"Result": "...", "RequestId": "..."}}
    return resp.get("Response", {}).get("Result", "")


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
# 声音复刻 — mega_tts V3（HTTP + X-Api-* 认证）
# ──────────────────────────────────────────────────────────────────────

import asyncio
import io as _io
import struct

# TTS WebSocket 端点（新版 seed-tts-2.0）
VOLC_TTS_WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"

# 声音复刻 HTTP 端点（mega_tts，新版鉴权）
VOLC_CLONE_UPLOAD_URL = "https://openspeech.bytedance.com/api/v1/mega_tts/audio/upload"
VOLC_CLONE_CREATE_URL = "https://openspeech.bytedance.com/api/v1/mega_tts/train/icl"
VOLC_CLONE_STATUS_URL = "https://openspeech.bytedance.com/api/v1/mega_tts/train/status"

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
        return 0, None, None
    mt   = (data[1] >> 4) & 0x0F   # msg_type: 9=FullServerResponse, 11=AudioOnlyServer, 15=Error
    flag = data[1] & 0x0F           # 1=PositiveSeq, 2=LastNoSeq, 3=NegativeSeq, 4=WithEvent
    off  = 4

    event = None
    if flag == 4:   # WithEvent
        if off + 4 > len(data):
            return mt, None, None
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
    return mt, event, audio


async def _tts_ws_async(text: str, speaker: str, speed_ratio: float,
                        api_key: str) -> bytes:
    """seed-tts-2.0 WebSocket 双向流 TTS，返回 MP3 字节"""
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
        "X-Api-Resource-Id": "seed-tts-2.0",
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
        _, ev, _ = _parse_ws_msg(raw)
        if ev != 50:
            raise RuntimeError(f"TTS: 未收到 ConnectionStarted（收到 event={ev}）")

        # 2. StartSession
        await ws.send(_build_ws_msg(
            100,
            _json({**base_body, "event": 100, "req_params": req_params}),
            session_id,
        ))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        _, ev, _ = _parse_ws_msg(raw)
        if ev != 150:
            raise RuntimeError(f"TTS: 未收到 SessionStarted（收到 event={ev}）")

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
            mt, ev, audio = _parse_ws_msg(raw)
            if audio:
                chunks.append(audio)
            if mt == 9 and ev in (152, 359):   # SessionFinished / TTSEnded
                break
            if mt == 15:
                raise RuntimeError("TTS 服务器返回错误帧")

        # 5. FinishConnection（best-effort）
        try:
            await ws.send(_build_ws_msg(2, b"{}"))
        except Exception:
            pass

        return b"".join(chunks)
    finally:
        await ws.close()


VOLC_ICL_TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"


def _tts_icl_http(text: str, voice_id: str, api_key: str, speed_ratio: float) -> bytes:
    """
    声音复刻合成（ICL HTTP 接口）。
    voice_id 是声音克隆训练后的 speaker_id，cluster 固定为 volcano_icl。
    """
    req_id  = _rand_id()
    payload = json.dumps({
        "app":  {"cluster": "volcano_icl"},
        "user": {"uid": req_id[:16]},
        "audio": {
            "voice_type":  voice_id,
            "encoding":    "mp3",
            "speed_ratio": round(speed_ratio, 2),
        },
        "request": {
            "reqid":     req_id,
            "text":      text,
            "operation": "query",
        },
    }, ensure_ascii=False).encode()

    req = urllib.request.Request(
        VOLC_ICL_TTS_URL,
        data=payload,
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        code = data.get("code", 0)
        if code != 3000:
            raise RuntimeError(f"ICL TTS 错误 code={code}: {data.get('message', '')}")
        import base64 as _b64
        return _b64.b64decode(data.get("data", ""))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ICL TTS HTTP 错误 {e.code}: {body}") from e


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
        # 使用声音复刻的 speaker_id 合成
        return _tts_icl_http(text, voice_id, api_key, speed)

    # 默认音色走 seed-tts-2.0 WebSocket
    speaker = DEFAULT_VOICE_MAP.get(language, DEFAULT_VOICE_MAP["zh"])
    return asyncio.run(_tts_ws_async(text, speaker, speed, api_key))


# ──────────────────────────────────────────────────────────────────────
# 声音复刻（mega_tts V3，HTTP + X-Api-* 认证）
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


def voice_clone_upload(avatar_id: str, sample_bytes: bytes, filename: str) -> str:
    """
    上传声音样本到火山引擎 mega_tts，返回 audio_id。
    """
    app_id  = config.VOLC_APP_ID
    api_key = config.VOLC_API_KEY
    if not app_id or not api_key:
        raise RuntimeError("火山引擎 TTS 未配置")

    boundary = "----FormBoundary" + _rand_id()[:16]
    _fn = filename.lower()
    ctype = ("audio/mpeg" if _fn.endswith(".mp3")  else
             "audio/mp4"  if _fn.endswith(".m4a")  else
             "audio/aac"  if _fn.endswith(".aac")  else
             "audio/webm" if _fn.endswith(".webm") else
             "audio/ogg"  if _fn.endswith(".ogg")  else
             "audio/wav")

    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"speaker_id\"\r\n\r\n{avatar_id[:64]}",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio_file\"; filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n",
    ]
    body = ("\r\n".join(parts)).encode() + sample_bytes + f"\r\n--{boundary}--\r\n".encode()

    headers = _volc_headers(app_id, api_key,
                            {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    req = urllib.request.Request(VOLC_CLONE_UPLOAD_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        if data.get("BaseResp", {}).get("StatusCode") != 0:
            raise RuntimeError(f"声音上传失败: {data}")
        return data.get("audio_id", "")
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"声音上传错误 {e.code}: {body_err}") from e


def voice_clone_train(avatar_id: str, audio_ids: list[str], language: str = "zh") -> str:
    """
    提交声音复刻训练任务，返回 speaker_id。
    """
    app_id  = config.VOLC_APP_ID
    api_key = config.VOLC_API_KEY

    payload = json.dumps({
        "speaker_id": avatar_id[:64],
        "audio_ids":  audio_ids,
        "language":   language,
    }).encode()

    headers = _volc_headers(app_id, api_key, {"Content-Type": "application/json"})
    req = urllib.request.Request(VOLC_CLONE_CREATE_URL, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if data.get("BaseResp", {}).get("StatusCode") != 0:
            raise RuntimeError(f"训练提交失败: {data}")
        return data.get("speaker_id", avatar_id[:64])
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"声音训练错误 {e.code}: {body_err}") from e


def voice_clone_status(speaker_id: str) -> dict:
    """
    查询声音克隆训练状态。
    Returns: {"status": "training"|"success"|"failed", "voice_id": "..."}
    """
    app_id  = config.VOLC_APP_ID
    api_key = config.VOLC_API_KEY

    payload = json.dumps({"speaker_id": speaker_id}).encode()
    headers = _volc_headers(app_id, api_key, {"Content-Type": "application/json"})
    req = urllib.request.Request(VOLC_CLONE_STATUS_URL, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        train_status = data.get("train_status", 0)   # 0=training, 1=success, 2=failed
        status_map = {0: "training", 1: "success", 2: "failed"}
        return {
            "status":   status_map.get(train_status, "training"),
            "voice_id": speaker_id if train_status == 1 else "",
        }
    except urllib.error.HTTPError as e:
        return {"status": "failed", "voice_id": ""}




# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _rand_id() -> str:
    import uuid
    return uuid.uuid4().hex
