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


def alert(tag: str, msg: str):
    """降级/故障告警日志 — 统一前缀，方便 docker logs | grep ALERT 监控"""
    print(f"⚠️ [ALERT][{tag}] {msg}", flush=True)


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
            "cloudbase": self._call_cloudbase,
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

    # ─── 腾讯 CloudBase AI 网关（OpenAI 兼容） ────────────────────
    def _call_cloudbase(
        self, system: str, messages: list, max_tokens: int, temperature: float
    ) -> str:
        base = config.CLOUDBASE_BASE_URL.rstrip("/")
        key  = config.CLOUDBASE_API_KEY
        if not base or not key:
            raise RuntimeError("CLOUDBASE_BASE_URL / CLOUDBASE_API_KEY 未配置")

        full_messages = [{"role": "system", "content": system}] + messages

        def _once(mt: int) -> str:
            payload = json.dumps({
                "model": config.CLOUDBASE_MODEL,
                "messages": full_messages,
                "max_tokens": mt,
                "temperature": temperature,
            }).encode()
            req = urllib.request.Request(
                f"{base}/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read())
            return data["choices"][0]["message"].get("content") or ""

        try:
            # v4-flash 是思维链模型：reasoning 也消耗 max_tokens 额度，
            # 小额度调用可能全被推理吃掉导致正文为空 → 给足余量 + 空结果扩容重试
            content = _once(max(max_tokens, 300) + 500)
            if not content:
                alert("LLM重试", f"CloudBase 返回空正文(思维链耗尽额度)，扩容至 {max_tokens + 2000} 重试")
                content = _once(max_tokens + 2000)
            if content:
                return content
            raise RuntimeError("CloudBase 连续两次返回空正文")
        except Exception as e:
            # CloudBase 失败（额度耗尽/网关异常）→ 自动回退 DeepSeek 直连
            if config.DEEPSEEK_API_KEY:
                alert("LLM降级", f"CloudBase 调用失败，回退 DeepSeek 直连: {e}")
                return self._call_deepseek(system, messages, max_tokens, temperature)
            raise

    # ─── DeepSeek Embedding ──────────────────────────────────────
    _embed_url_winner: Optional[str] = None   # 首个成功的 embedding URL，缓存直达

    def _embed_request(self, inputs: List[str]) -> list:
        """
        调用 CloudBase 混元 embedding（OpenAI 兼容）。
        embedding 挂在 hunyuan provider 下，且新老路径格式不同
        （…/v1/ai/hunyuan/v1/embeddings 或 …/v1/ai/hunyuan/embeddings），
        逐个尝试，成功后缓存 URL。
        返回 API 的 data 数组；全部失败抛异常。
        """
        import re as _re
        base = config.CLOUDBASE_BASE_URL.rstrip("/")
        key  = config.CLOUDBASE_API_KEY
        if not base or not key:
            raise RuntimeError("CloudBase 未配置（CLOUDBASE_BASE_URL / CLOUDBASE_API_KEY）")

        env_base = _re.sub(r'/v1/ai/[^/]+$', '', base)   # 去掉 provider 段
        prov = config.CLOUDBASE_EMBED_PROVIDER
        candidates = ([LLMProvider._embed_url_winner] if LLMProvider._embed_url_winner else [
            f"{env_base}/v1/ai/{prov}/v1/embeddings",   # 老格式（provider 后多一层 /v1）
            f"{env_base}/v1/ai/{prov}/embeddings",      # 新格式
            f"{base}/embeddings",                        # cloudbase 分组兜底
        ])

        payload = json.dumps({
            "model": config.CLOUDBASE_EMBED_MODEL,
            "input": [t[:2000] for t in inputs],
        }).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }

        errors = []
        for url in candidates:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                if LLMProvider._embed_url_winner != url:
                    LLMProvider._embed_url_winner = url
                    print(f"[LLM] embedding 路径确定: {url}")
                return data.get("data", [])
            except urllib.error.HTTPError as e:
                errors.append(f"[{url}] {e.code}: {e.read().decode('utf-8', errors='replace')[:120]}")
            except Exception as e:
                errors.append(f"[{url}] {e}")
        LLMProvider._embed_url_winner = None
        raise RuntimeError("embedding 全部路径失败:\n" + "\n".join(errors))

    def embed(self, text: str) -> Optional[List[float]]:
        """
        文本 → 向量（CloudBase 混元 embedding）。
        失败返回 None，调用方降级关键词检索 —— 降级必须触发 ALERT 日志。
        """
        try:
            data = self._embed_request([text])
            if data:
                return data[0]["embedding"]
            alert("EMBED降级", "embedding 返回空数据，RAG 将降级为关键词匹配")
            return None
        except Exception as e:
            alert("EMBED降级", f"embedding 调用失败，RAG 将降级为关键词匹配: {e}")
            return None

    def batch_embed(self, texts: List[str]) -> List[Optional[List[float]]]:
        """
        批量向量化。返回与 texts 等长的列表，失败项为 None。
        """
        if not texts:
            return []
        try:
            data = self._embed_request(texts)
            result: List[Optional[List[float]]] = [None] * len(texts)
            for item in data:
                idx = item.get("index", 0)
                if idx < len(result):
                    result[idx] = item["embedding"]
            missing = sum(1 for r in result if r is None)
            if missing:
                alert("EMBED降级", f"batch_embed 缺失 {missing}/{len(texts)} 条向量")
            return result
        except Exception as e:
            alert("EMBED降级", f"batch_embed 调用失败（{len(texts)}条全部降级）: {e}")
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
