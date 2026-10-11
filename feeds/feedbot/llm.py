"""AI clients for the pipeline. Every client returns one JSON object per call.

Three providers are supported; the pipeline uses whichever key is set (see
make_llm). Keys come from environment variables (GitHub Actions secrets) and
are never logged or written to disk:

    ANTHROPIC_API_KEY  Claude   (extract: CLAUDE_MODEL, verify: CLAUDE_VERIFY_MODEL)
    GEMINI_API_KEY     Gemini   (extract: GEMINI_MODEL, verify: GEMINI_VERIFY_MODEL)
    DEEPSEEK_API_KEY   DeepSeek (extract: DEEPSEEK_MODEL, verify: DEEPSEEK_VERIFY_MODEL)

Set LLM_PROVIDER (claude, gemini or deepseek) to choose when several keys
are set. Callers ask for a role, not a model: model=None (or EXTRACT) for
extraction and model=VERIFY for the independent check, which each client
maps to its stronger model so a mistake by the extractor is not repeated.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

EXTRACT = "extract"
VERIFY = "verify"

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# deepseek-flash answers the old deepseek-chat name too.
DEEPSEEK_EXTRACT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_VERIFY_MODEL = os.environ.get("DEEPSEEK_VERIFY_MODEL", "deepseek-v4-pro")
CLAUDE_EXTRACT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_VERIFY_MODEL = os.environ.get("CLAUDE_VERIFY_MODEL", "claude-opus-5-5")
GEMINI_EXTRACT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_VERIFY_MODEL = os.environ.get("GEMINI_VERIFY_MODEL", "gemini-flash-latest")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

class LlmError(RuntimeError):
    pass


class Llm(Protocol):
    extract_model: str
    verify_model: str

    def json(self, system: str, user: str, *, model: str | None = None,
             max_tokens: int = 4000) -> dict[str, Any]: ...


def parse_json_reply(text: str) -> dict[str, Any]:
    """The JSON object in a model reply (tolerates code fences and prose)."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        value = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise LlmError("reply has no JSON object") from None
        try:
            value = json.loads(t[start:end + 1])
        except json.JSONDecodeError as e:
            raise LlmError(f"reply JSON is invalid: {e}") from None
    if not isinstance(value, dict):
        raise LlmError("reply JSON is not an object")
    return value


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    by_model: dict[str, int] = field(default_factory=dict)

    def add(self, model: str, prompt_tokens: Any = 0, completion_tokens: Any = 0) -> None:
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0) + 1
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)


class _Client:
    """Shared call budget, usage and role -> model mapping."""

    name = "llm"
    extract_model = ""
    verify_model = ""

    def __init__(self, *, max_calls: int) -> None:
        self.usage = Usage()
        self.max_calls = max_calls

    def _model(self, model: str | None) -> str:
        if self.usage.calls >= self.max_calls:
            raise LlmError("call budget for this run is used up")
        if model in (None, EXTRACT):
            return self.extract_model
        if model == VERIFY:
            return self.verify_model
        return model


class DeepSeek(_Client):
    """DeepSeek chat API (OpenAI-compatible) in JSON mode."""

    name = "deepseek"

    def __init__(self, api_key: str | None = None, *, base_url: str = BASE_URL,
                 timeout: float = 180, retries: int = 3,
                 session: requests.Session | None = None,
                 max_calls: int = 400) -> None:
        super().__init__(max_calls=max_calls)
        key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise LlmError("DEEPSEEK_API_KEY is not set")
        self.extract_model = DEEPSEEK_EXTRACT_MODEL
        self.verify_model = DEEPSEEK_VERIFY_MODEL
        self._key = key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout
        self._retries = retries
        self._http = session or requests.Session()

    def json(self, system: str, user: str, *, model: str | None = None,
             max_tokens: int = 4000) -> dict[str, Any]:
        model = self._model(model)
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                r = self._http.post(
                    self._url, json=body, timeout=self._timeout,
                    headers={"Authorization": f"Bearer {self._key}"})
                if r.status_code in (429, 500, 502, 503, 504):
                    raise LlmError(f"HTTP {r.status_code}")
                if r.status_code >= 400:
                    raise LlmError(f"HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                u = data.get("usage") or {}
                self.usage.add(model, u.get("prompt_tokens"), u.get("completion_tokens"))
                content = data["choices"][0]["message"]["content"] or ""
                return parse_json_reply(content)
            except (requests.RequestException, LlmError, KeyError, ValueError) as e:
                last = e
                if isinstance(e, LlmError) and str(e).startswith("HTTP 4") and "429" not in str(e):
                    break
                time.sleep(2 ** attempt * 3)
        raise LlmError(f"DeepSeek call failed: {last}")


# Models that accept the server-side refusal fallback (Haiku has none).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class Claude(_Client):
    """Anthropic Messages API through the official SDK.

    The JSON shape is set by each prompt, so replies are parsed from text.
    Thinking is on (adaptive) on current models and counts toward
    max_tokens, so every call gets at least CLAUDE_MIN_TOKENS.
    """

    name = "claude"
    MIN_TOKENS = 16000

    def __init__(self, api_key: str | None = None, *, timeout: float = 300,
                 retries: int = 3, client: Any = None, max_calls: int = 400) -> None:
        super().__init__(max_calls=max_calls)
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key and client is None:
            raise LlmError("ANTHROPIC_API_KEY is not set")
        self.extract_model = CLAUDE_EXTRACT_MODEL
        self.verify_model = CLAUDE_VERIFY_MODEL
        if client is None:
            import anthropic  # only needed when Claude is the provider
            # keys not made inside a workspace must name one
            workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
            headers = {"anthropic-workspace-id": workspace} if workspace else None
            client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=retries,
                                         default_headers=headers)
        self._client = client

    def json(self, system: str, user: str, *, model: str | None = None,
             max_tokens: int = 4000) -> dict[str, Any]:
        import anthropic

        model = self._model(model)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max(max_tokens, self.MIN_TOKENS),
            "system": system,
            "messages": [{"role": "user", "content": user}],
            # Fact checking is the point of the verifier; extraction is routine.
            "output_config": {"effort": "high" if model == self.verify_model else "medium"},
        }
        try:
            if model in FALLBACK_MODELS:
                # A safety decline is re-run on a fallback model in the same call.
                reply = self._client.beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            else:
                reply = self._client.messages.create(**params)
        except anthropic.APIStatusError as e:
            raise LlmError(f"Claude call failed: HTTP {e.status_code}: {str(e.message)[:200]}") from None
        except anthropic.APIConnectionError as e:
            raise LlmError(f"Claude call failed: {e}") from None
        u = reply.usage
        self.usage.add(model, getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0))
        if reply.stop_reason == "refusal":
            raise LlmError("Claude declined this request")
        if reply.stop_reason == "max_tokens":
            raise LlmError("Claude reply was cut off (max_tokens)")
        text = "".join(b.text for b in reply.content if b.type == "text")
        return parse_json_reply(text)


class Gemini(_Client):
    """Google Gemini generateContent API in JSON mode."""

    name = "gemini"

    def __init__(self, api_key: str | None = None, *, timeout: float = 180, retries: int = 3,
                 session: requests.Session | None = None, max_calls: int = 400) -> None:
        super().__init__(max_calls=max_calls)
        key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise LlmError("GEMINI_API_KEY is not set")
        self.extract_model = GEMINI_EXTRACT_MODEL
        self.verify_model = GEMINI_VERIFY_MODEL
        self._key = key
        self._timeout = timeout
        self._retries = retries
        self._http = session or requests.Session()

    def json(self, system: str, user: str, *, model: str | None = None,
             max_tokens: int = 4000) -> dict[str, Any]:
        model = self._model(model)
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            # Flash models think before answering; leave room for both.
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0,
                                 "maxOutputTokens": max(max_tokens, 16000)},
        }
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                r = self._http.post(GEMINI_URL.format(model=model), json=body, timeout=self._timeout,
                                    headers={"x-goog-api-key": self._key})
                if r.status_code in (429, 500, 502, 503, 504):
                    raise LlmError(f"HTTP {r.status_code}")
                if r.status_code >= 400:
                    raise LlmError(f"HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                u = data.get("usageMetadata") or {}
                self.usage.add(model, u.get("promptTokenCount"), u.get("candidatesTokenCount"))
                cand = (data.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if not text:
                    raise LlmError(f"empty reply ({cand.get('finishReason') or 'no candidate'})")
                return parse_json_reply(text)
            except (requests.RequestException, LlmError, KeyError, ValueError) as e:
                last = e
                if isinstance(e, LlmError) and str(e).startswith("HTTP 4") and "429" not in str(e):
                    break
                time.sleep(2 ** attempt * 3)
        raise LlmError(f"Gemini call failed: {last}")


PROVIDERS = {"claude": ("ANTHROPIC_API_KEY", Claude),
             "gemini": ("GEMINI_API_KEY", Gemini),
             "deepseek": ("DEEPSEEK_API_KEY", DeepSeek)}


def make_llm(env: dict[str, str] | None = None) -> _Client:
    """The client for LLM_PROVIDER, or for the first key that is set
    (Claude, then Gemini, then DeepSeek)."""
    env = os.environ if env is None else env
    choice = (env.get("LLM_PROVIDER") or "").strip().lower()
    if choice:
        if choice not in PROVIDERS:
            raise LlmError(f"LLM_PROVIDER must be one of {', '.join(PROVIDERS)}, not {choice!r}")
        var, cls = PROVIDERS[choice]
        if not env.get(var):
            raise LlmError(f"LLM_PROVIDER is {choice} but {var} is not set")
        return cls(env[var])
    for var, cls in PROVIDERS.values():
        if env.get(var):
            return cls(env[var])
    raise LlmError("no AI key is set (ANTHROPIC_API_KEY, GEMINI_API_KEY or DEEPSEEK_API_KEY)")
