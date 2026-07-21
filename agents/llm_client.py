"""
agents/llm_client.py

Swappable LLM abstraction layer. All LLM calls in the system go through
BaseLLMClient. Provider is selected via the LLM_PROVIDER environment variable.

Supported providers:
  groq    — Groq API, Llama 3 8B (default, free tier)
  ollama  — Local Ollama, phi3-mini (offline fallback)
  bedrock — AWS Bedrock, Claude Haiku (production at scale)
  openai  — OpenAI, GPT-4o-mini (production alternative)

Fallback chain:
  groq (if GROQ_API_KEY set) → ollama (if OLLAMA_HOST reachable) → rules

Usage:
  from agents.llm_client import get_llm_client
  client = get_llm_client()
  response = client.complete(prompt="...", system="...")
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------

class BaseLLMClient(ABC):
    """
    Minimal interface all LLM clients must implement.
    complete() takes a user prompt and optional system prompt,
    returns the model's text response as a string.
    """

    @abstractmethod
    def complete(self, prompt: str, system: str = "") -> str:
        raise NotImplementedError

    def complete_json(self, prompt: str, system: str = "") -> dict:
        """
        Convenience wrapper: calls complete() and attempts JSON parse.
        Returns parsed dict or raises ValueError on parse failure.
        Callers should catch ValueError and fall back to rule-based routing.
        """
        raw = self.complete(prompt=prompt, system=system)
        # Strip markdown code fences if model wrapped the JSON
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            return json.loads(raw.strip())
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM returned non-JSON output: {raw[:200]}") from exc


# ---------------------------------------------------------------------------
# Groq client (default)
# ---------------------------------------------------------------------------

class GroqClient(BaseLLMClient):
    """
    Groq API client using Llama 3 8B.
    Free tier: 30 req/min, 14,400 req/day.
    Docs: https://console.groq.com/docs/openai
    """

    DEFAULT_MODEL = "llama3-8b-8192"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 30,
    ):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        self.model = model or os.environ.get("GROQ_MODEL", self.DEFAULT_MODEL)
        self.timeout = timeout
        self.base_url = "https://api.groq.com/openai/v1"

        if not self.api_key:
            raise ValueError(
                "GROQ_API_KEY is not set. "
                "Set LLM_PROVIDER=ollama to use local fallback."
            )

    def complete(self, prompt: str, system: str = "") -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,  # Low temperature for consistent structured output
            "max_tokens": 1024,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise RuntimeError("Groq rate limit exceeded. Consider switching LLM_PROVIDER=ollama.") from exc
            raise


# ---------------------------------------------------------------------------
# Ollama client (offline fallback)
# ---------------------------------------------------------------------------

class OllamaClient(BaseLLMClient):
    """
    Local Ollama client.
    Default model: phi3-mini (runs on 4GB RAM, ~2.3GB download).
    Alternative: gemma:2b (~1.5GB, lighter but less capable).
    Docs: https://ollama.com/library
    """

    DEFAULT_MODEL = "phi3-mini"

    def __init__(
        self,
        host: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
    ):
        self.host = host or os.environ.get("OLLAMA_HOST", "http://ollama:11434")
        self.model = model or os.environ.get("OLLAMA_MODEL", self.DEFAULT_MODEL)
        self.timeout = timeout

    def complete(self, prompt: str, system: str = "") -> str:
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        if system:
            payload["system"] = system

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.host}/api/generate",
                    json=payload,
                )
                resp.raise_for_status()
                return resp.json()["response"]
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Cannot reach Ollama at {self.host}. "
                "Is the ollama service running? (docker compose up ollama)"
            ) from exc


# ---------------------------------------------------------------------------
# Bedrock client stub (production scale-up)
# ---------------------------------------------------------------------------

class BedrockClient(BaseLLMClient):
    """
    AWS Bedrock client stub.
    To activate: set LLM_PROVIDER=bedrock in environment.
    Requires: boto3, IAM role with bedrock:InvokeModel permission.
    Recommended model: anthropic.claude-haiku-20240307-v1:0 (cost-efficient).

    Upgrade path from Groq:
      1. Add bedrock:InvokeModel to the triage_agent Lambda IAM role (infra/template.yaml)
      2. Set LLM_PROVIDER=bedrock in SSM Parameter Store or Lambda env vars
      3. No code changes required.
    """

    DEFAULT_MODEL = "anthropic.claude-haiku-20240307-v1:0"

    def __init__(self, model: Optional[str] = None, region: Optional[str] = None):
        self.model = model or os.environ.get("BEDROCK_MODEL", self.DEFAULT_MODEL)
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")

    def complete(self, prompt: str, system: str = "") -> str:
        try:
            import boto3  # noqa: PLC0415
            import json as _json  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("boto3 is required for BedrockClient. pip install boto3") from exc

        client = boto3.client("bedrock-runtime", region_name=self.region)
        body = _json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        })
        resp = client.invoke_model(modelId=self.model, body=body)
        result = _json.loads(resp["body"].read())
        return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# OpenAI client stub (production alternative)
# ---------------------------------------------------------------------------

class OpenAIClient(BaseLLMClient):
    """
    OpenAI client stub.
    To activate: set LLM_PROVIDER=openai and OPENAI_API_KEY in environment.
    Recommended model: gpt-4o-mini (cost-efficient, strong JSON output).
    """

    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("OPENAI_MODEL", self.DEFAULT_MODEL)
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not set.")

    def complete(self, prompt: str, system: str = "") -> str:
        try:
            from openai import OpenAI  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("openai package required. pip install openai") from exc

        client = OpenAI(api_key=self.api_key)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.1,
            max_tokens=1024,
        )
        return resp.choices[0].message.content


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def get_llm_client() -> BaseLLMClient:
    """
    Returns the appropriate LLM client based on LLM_PROVIDER env var.
    Implements the fallback chain:
      groq → ollama → raises RuntimeError (caller falls back to rule-based routing)
    """
    provider = os.environ.get("LLM_PROVIDER", "groq").lower()
    logger.info("LLM_PROVIDER=%s", provider)

    if provider == "groq":
        try:
            return GroqClient()
        except ValueError:
            logger.warning(
                "GROQ_API_KEY not set — falling back to OllamaClient. "
                "Set GROQ_API_KEY or LLM_PROVIDER=ollama to suppress this warning."
            )
            return OllamaClient()

    if provider == "ollama":
        return OllamaClient()

    if provider == "bedrock":
        return BedrockClient()

    if provider == "openai":
        return OpenAIClient()

    raise ValueError(
        f"Unknown LLM_PROVIDER: '{provider}'. "
        "Valid values: groq, ollama, bedrock, openai"
    )
