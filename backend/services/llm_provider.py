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
from typing import Generator

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
