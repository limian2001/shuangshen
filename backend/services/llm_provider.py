from __future__ import annotations
"""
LLM 统一接口
支持切换: anthropic | openai | local (Ollama) | deepseek

新增模型：在 LLMProvider._call_xxx() 中添加对应方法，
并在 chat() 的 dispatch 字典里注册即可。
"""
import json
import os
import urllib.request
import urllib.error
from typing import Generator, List, Optional

from backend.core.config import config


class LLMProvider:
    """
    统一的 LLM 调用接口。
    所有替身对话、人格构建均通过此类，方便日后切换模型。
    """

    def __init__(self):
        self.provider = config.LLM_PROVIDER

    def chat(
        self,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int = 800,
        temperature: float = 0.75,
        stream: bool = False,
    ) -> str:
        """
        统一对话接口。

        Args:
            system_prompt: 人格 System Prompt
            messages: [{"role": "user"|"assistant", "content": "..."}]
            max_tokens: 最大输出 token
            temperature: 随机度（0~1）
            stream: 是否流式（当前实现均返回完整字符串）

        Returns:
            助手回复文本
        """
        dispatch = {
            "anthropic": self._call_anthropic,
            "openai":    self._call_openai,
            "local":     self._call_local,
            "deepseek":  self._call_deepseek,
        }
        fn = dispatch.get(self.provider)
        if fn is None:
            raise ValueError(f"未知 LLM 提供商: {self.provider}")
        return fn(system_prompt, messages, max_tokens, temperature)

    # ─── Anthropic Claude ───────────────────────────────────────
    def _call_anthropic(
        self, system: str, messages: list, max_tokens: int, temperature: float
    ) -> str:
        api_key = config.ANTHROPIC_API_KEY
        if not api_key or api_key.startswith("sk-ant-xxx"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY 未配置，请在 .env 中填入真实 Key"
            )

        payload = json.dumps({
            "model": config.ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data["content"][0]["text"]

    # ─── OpenAI ─────────────────────────────────────────────────
    def _call_openai(
        self, system: str, messages: list, max_tokens: int, temperature: float
    ) -> str:
        api_key = config.OPENAI_API_KEY
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY 未配置")

        full_messages = [{"role": "system", "content": system}] + messages
        payload = json.dumps({
            "model": config.OPENAI_MODEL,
            "messages": full_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]

    # ─── DeepSeek ────────────────────────────────────────────────
    def _call_deepseek(
        self, system: str, messages: list, max_tokens: int, temperature: float
    ) -> str:
        api_key = config.DEEPSEEK_API_KEY
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY 未配置，请在 .env 中填入真实 Key")

        full_messages = [{"role": "system", "content": system}] + messages
        payload = json.dumps({
            "model": config.DEEPSEEK_MODEL,
            "messages": full_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]

    # ─── Vision — 图片描述 ──────────────────────────────────────
    def describe_image(self, image_bytes: bytes, media_type: str, prompt: str) -> str:
        """
        调用当前 LLM Provider 的视觉能力描述图片。
        返回中文描述文本；不支持时抛 RuntimeError。
        """
        import base64
        b64 = base64.b64encode(image_bytes).decode()

        if self.provider == "anthropic":
            return self._vision_anthropic(b64, media_type, prompt)
        elif self.provider in ("deepseek", "openai"):
            return self._vision_openai_compat(b64, media_type, prompt)
        else:
            raise RuntimeError(
                f"当前 LLM 提供商 '{self.provider}' 不支持图片识别，"
                "请切换到 anthropic / deepseek / openai"
            )

    def _vision_anthropic(self, b64: str, media_type: str, prompt: str) -> str:
        api_key = config.ANTHROPIC_API_KEY
        if not api_key or api_key.startswith("sk-ant-xxx"):
            raise RuntimeError("ANTHROPIC_API_KEY 未配置，无法使用图片识别")
        import json, urllib.request
        payload = json.dumps({
            "model": config.ANTHROPIC_MODEL,
            "max_tokens": 300,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["content"][0]["text"].strip()

    def _vision_openai_compat(self, b64: str, media_type: str, prompt: str) -> str:
        """DeepSeek / OpenAI 兼容接口的图片描述（OpenAI vision 格式）"""
        if self.provider == "deepseek":
            api_key = config.DEEPSEEK_API_KEY
            base_url = "https://api.deepseek.com/v1/chat/completions"
            model = config.DEEPSEEK_MODEL
            if not api_key:
                raise RuntimeError("DEEPSEEK_API_KEY 未配置，无法使用图片识别")
        else:
            api_key = config.OPENAI_API_KEY
            base_url = "https://api.openai.com/v1/chat/completions"
            model = config.OPENAI_MODEL
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY 未配置，无法使用图片识别")

        import json, urllib.request
        payload = json.dumps({
            "model": model,
            "max_tokens": 300,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }).encode()
        req = urllib.request.Request(
            base_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()

    # ─── DeepSeek Embedding ──────────────────────────────────────
    def embed(self, text: str) -> Optional[List[float]]:
        """
        将文本转换为向量（仅支持 DeepSeek provider）。
        失败时返回 None，调用方应降级到关键词检索。
        """
        api_key = config.DEEPSEEK_API_KEY
        if not api_key:
            return None
        try:
            payload = json.dumps({
                "model": config.DEEPSEEK_EMBED_MODEL,
                "input": text[:2000],   # 防止超长
            }).encode()
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/embeddings",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            return data["data"][0]["embedding"]
        except Exception as e:
            print(f"[LLM] Embedding 调用失败: {e}")
            return None

    def batch_embed(self, texts: List[str]) -> List[Optional[List[float]]]:
        """
        批量获取向量。DeepSeek API 支持数组输入，单次调用。
        返回与 texts 等长的列表，失败项为 None。
        """
        api_key = config.DEEPSEEK_API_KEY
        if not api_key or not texts:
            return [None] * len(texts)
        try:
            # DeepSeek embedding API 兼容 OpenAI，支持 input 为字符串数组
            payload = json.dumps({
                "model": config.DEEPSEEK_EMBED_MODEL,
                "input": [t[:2000] for t in texts],
            }).encode()
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/embeddings",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            # API 返回的 data 数组按 index 排列
            result: List[Optional[List[float]]] = [None] * len(texts)
            for item in data.get("data", []):
                idx = item.get("index", 0)
                if idx < len(result):
                    result[idx] = item["embedding"]
            return result
        except Exception as e:
            print(f"[LLM] batch_embed 调用失败: {e}")
            return [None] * len(texts)

    # ─── 本地模型 (Ollama) ───────────────────────────────────────
    def _call_local(
        self, system: str, messages: list, max_tokens: int, temperature: float
    ) -> str:
        full_messages = [{"role": "system", "content": system}] + messages
        payload = json.dumps({
            "model": config.LOCAL_LLM_MODEL,
            "messages": full_messages,
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }).encode()

        req = urllib.request.Request(
            f"{config.LOCAL_LLM_BASE_URL}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["message"]["content"]


# 单例
llm = LLMProvider()
