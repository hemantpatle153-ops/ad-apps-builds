"""DeepSeek chat client (OpenAI-compatible API) that always returns JSON.

The key comes from the DEEPSEEK_API_KEY environment variable (a GitHub
Actions secret); it is never logged or written to disk.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# deepseek-flash answers the old deepseek-chat name too; the verifier uses
# the stronger model so a mistake by the extractor is not repeated.
EXTRACT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
VERIFY_MODEL = os.environ.get("DEEPSEEK_VERIFY_MODEL", "deepseek-v4-pro")


class LlmError(RuntimeError):
    pass


class Llm(Protocol):
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

    def add(self, model: str, usage: dict[str, Any] | None) -> None:
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0) + 1
        if usage:
            self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.completion_tokens += int(usage.get("completion_tokens") or 0)


class DeepSeek:
    def __init__(self, api_key: str | None = None, *, base_url: str = BASE_URL,
                 timeout: float = 180, retries: int = 3,
                 session: requests.Session | None = None,
                 max_calls: int = 400) -> None:
        key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise LlmError("DEEPSEEK_API_KEY is not set")
        self._key = key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout
        self._retries = retries
        self._http = session or requests.Session()
        self.usage = Usage()
        self.max_calls = max_calls

    def json(self, system: str, user: str, *, model: str | None = None,
             max_tokens: int = 4000) -> dict[str, Any]:
        if self.usage.calls >= self.max_calls:
            raise LlmError("call budget for this run is used up")
        model = model or EXTRACT_MODEL
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
                self.usage.add(model, data.get("usage"))
                content = data["choices"][0]["message"]["content"] or ""
                return parse_json_reply(content)
            except (requests.RequestException, LlmError, KeyError, ValueError) as e:
                last = e
                if isinstance(e, LlmError) and str(e).startswith("HTTP 4") and "429" not in str(e):
                    break
                time.sleep(2 ** attempt * 3)
        raise LlmError(f"DeepSeek call failed: {last}")
