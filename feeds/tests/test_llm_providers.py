"""Claude and Gemini clients, the role -> model mapping and provider choice."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest
import requests

from feedbot import llm
from tests.test_sources_llm_fetch import FakeResponse, FakeSession


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


# --- Claude ----------------------------------------------------------------

def reply(text='{"ok": true}', stop="end_turn", blocks=None, usage=(120, 30)):
    content = blocks if blocks is not None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop, content=content,
                           usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]))


class FakeMessages:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeAnthropic:
    def __init__(self, *replies):
        shared = list(replies)
        self.messages = FakeMessages(shared)
        self.beta = SimpleNamespace(messages=FakeMessages(shared))

    @property
    def calls(self):
        return self.messages.calls + self.beta.messages.calls


def claude(*replies, **kw):
    fake = FakeAnthropic(*replies)
    return llm.Claude("sk-ant-test", client=fake, **kw), fake


def _request():
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def test_claude_default_models():
    c, _ = claude()
    assert c.extract_model == "claude-haiku-5-5"
    assert c.verify_model == "claude-opus-5-5"
    assert c.name == "claude"


def test_claude_extract_uses_extract_model_without_fallbacks():
    c, fake = claude(reply('{"x": 1}'))
    assert c.json("sys", "user") == {"x": 1}
    call = fake.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "user"}]
    assert "fallbacks" not in call and "betas" not in call
    assert fake.beta.messages.calls == []


def test_claude_verify_uses_opus_with_default_fallbacks():
    c, fake = claude(reply('{"results": []}'))
    assert c.json("sys", "user", model=llm.VERIFY) == {"results": []}
    call = fake.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert fake.messages.calls == []


@pytest.mark.parametrize("role,effort", [(None, "medium"), (llm.EXTRACT, "medium"), (llm.VERIFY, "high")])
def test_claude_effort_by_role(role, effort):
    c, fake = claude(reply())
    c.json("s", "u", model=role)
    assert fake.calls[0]["output_config"] == {"effort": effort}


@pytest.mark.parametrize("asked,sent", [(100, 16000), (4000, 16000), (16000, 16000), (30000, 30000)])
def test_claude_leaves_room_for_thinking(asked, sent):
    c, fake = claude(reply())
    c.json("s", "u", max_tokens=asked)
    assert fake.calls[0]["max_tokens"] == sent


def test_claude_sends_no_sampling_or_thinking_params():
    c, fake = claude(reply(), reply())
    c.json("s", "u")
    c.json("s", "u", model=llm.VERIFY)
    for call in fake.calls:
        assert not {"temperature", "top_p", "top_k", "thinking"} & set(call)


@pytest.mark.parametrize("model", sorted(llm.FALLBACK_MODELS))
def test_claude_fallback_models_use_beta(model):
    c, fake = claude(reply())
    c.json("s", "u", model=model)
    assert fake.beta.messages.calls[0]["model"] == model


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-opus-4-8", "claude-sonnet-5"])
def test_claude_other_models_use_plain_messages(model):
    c, fake = claude(reply())
    c.json("s", "u", model=model)
    assert fake.messages.calls[0]["model"] == model


def test_claude_records_usage_per_model():
    c, _ = claude(reply(usage=(100, 20)), reply(usage=(50, 10)))
    c.json("s", "u")
    c.json("s", "u", model=llm.VERIFY)
    assert c.usage.calls == 2
    assert c.usage.prompt_tokens == 150 and c.usage.completion_tokens == 30
    assert c.usage.by_model == {"claude-haiku-5-5": 1, "claude-opus-5-5": 1}


def test_claude_reads_only_text_blocks():
    blocks = [SimpleNamespace(type="thinking", thinking=""),
              SimpleNamespace(type="text", text='{"a": '),
              SimpleNamespace(type="text", text='1}')]
    c, _ = claude(reply(blocks=blocks))
    assert c.json("s", "u") == {"a": 1}


@pytest.mark.parametrize("text", ['```json\n{"a": 1}\n```', 'Here it is: {"a": 1} done', '{"a": 1}'])
def test_claude_tolerates_fences_and_prose(text):
    c, _ = claude(reply(text))
    assert c.json("s", "u") == {"a": 1}


def test_claude_refusal_is_an_error():
    c, _ = claude(reply(stop="refusal", blocks=[]))
    with pytest.raises(llm.LlmError, match="declined"):
        c.json("s", "u", model=llm.VERIFY)
    assert c.usage.calls == 1


def test_claude_cut_off_reply_is_an_error():
    c, _ = claude(reply('{"a": ', stop="max_tokens"))
    with pytest.raises(llm.LlmError, match="cut off"):
        c.json("s", "u")


def test_claude_reply_without_json_is_an_error():
    c, _ = claude(reply("no json here"))
    with pytest.raises(llm.LlmError, match="no JSON"):
        c.json("s", "u")


def test_claude_reply_with_json_array_is_an_error():
    c, _ = claude(reply("[1, 2]"))
    with pytest.raises(llm.LlmError):
        c.json("s", "u")


@pytest.mark.parametrize("cls,status", [(anthropic.BadRequestError, 400), (anthropic.AuthenticationError, 401),
                                        (anthropic.RateLimitError, 429), (anthropic.InternalServerError, 500)])
def test_claude_api_errors_become_llm_errors(cls, status):
    err = cls("nope", response=httpx.Response(status, request=_request()), body=None)
    c, _ = claude(err)
    with pytest.raises(llm.LlmError, match=f"HTTP {status}"):
        c.json("s", "u")


def test_claude_connection_error_becomes_llm_error():
    c, _ = claude(anthropic.APIConnectionError(request=_request()))
    with pytest.raises(llm.LlmError, match="Claude call failed"):
        c.json("s", "u")


def test_claude_call_budget():
    c, _ = claude(reply(), reply(), max_calls=1)
    c.json("s", "u")
    with pytest.raises(llm.LlmError, match="budget"):
        c.json("s", "u")


def test_claude_missing_key():
    with pytest.raises(llm.LlmError, match="ANTHROPIC_API_KEY"):
        llm.Claude()


def test_claude_builds_real_sdk_client_from_key():
    c = llm.Claude("sk-ant-test", retries=5, timeout=60)
    assert isinstance(c._client, anthropic.Anthropic)
    assert c._client.max_retries == 5
    assert "anthropic-workspace-id" not in c._client.default_headers


def test_claude_sends_workspace_id_when_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", " wrkspc_123 ")
    c = llm.Claude("sk-ant-test")
    assert c._client.default_headers["anthropic-workspace-id"] == "wrkspc_123"


def test_claude_blank_workspace_id_sends_no_header(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "  ")
    c = llm.Claude("sk-ant-test")
    assert "anthropic-workspace-id" not in c._client.default_headers


def test_claude_models_follow_env(monkeypatch):
    monkeypatch.setattr(llm, "CLAUDE_EXTRACT_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(llm, "CLAUDE_VERIFY_MODEL", "claude-fable-5-1")
    c, fake = claude(reply(), reply())
    c.json("s", "u")
    c.json("s", "u", model=llm.VERIFY)
    assert [x["model"] for x in fake.calls] == ["claude-sonnet-5-5", "claude-fable-5-1"]


# --- Gemini ----------------------------------------------------------------

def gok(text, usage=None, thought=None, finish="STOP"):
    parts = ([{"text": thought, "thought": True}] if thought else []) + ([{"text": text}] if text else [])
    return FakeResponse(200, {"candidates": [{"content": {"parts": parts}, "finishReason": finish}],
                              "usageMetadata": usage or {"promptTokenCount": 40, "candidatesTokenCount": 8}})


def test_gemini_sends_json_mode_key_header_and_system():
    s = FakeSession([gok('{"x": 1}')])
    g = llm.Gemini("g-key", session=s)
    assert g.json("sys", "user", max_tokens=2000) == {"x": 1}
    url, body, headers = s.posts[0]
    assert url == llm.GEMINI_URL.format(model="gemini-flash-latest")
    assert headers == {"x-goog-api-key": "g-key"}
    assert "g-key" not in url
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "user"}]}]
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json" and cfg["temperature"] == 0
    assert cfg["maxOutputTokens"] == 16000


def test_gemini_usage():
    s = FakeSession([gok("{}"), gok("{}", usage={"promptTokenCount": 5, "candidatesTokenCount": 1})])
    g = llm.Gemini("k", session=s)
    g.json("s", "u")
    g.json("s", "u", model=llm.VERIFY)
    assert g.usage.calls == 2 and g.usage.prompt_tokens == 45 and g.usage.completion_tokens == 9


def test_gemini_skips_thought_parts():
    s = FakeSession([gok('{"a": 2}', thought="thinking about {it}")])
    assert llm.Gemini("k", session=s).json("s", "u") == {"a": 2}


def test_gemini_verify_role_uses_verify_model(monkeypatch):
    monkeypatch.setattr(llm, "GEMINI_VERIFY_MODEL", "gemini-pro-latest")
    s = FakeSession([gok("{}")])
    llm.Gemini("k", session=s).json("s", "u", model=llm.VERIFY)
    assert s.posts[0][0] == llm.GEMINI_URL.format(model="gemini-pro-latest")


@pytest.mark.parametrize("first", [FakeResponse(429), FakeResponse(503), requests.ConnectionError("down"),
                                   gok("not json"), gok("", finish="SAFETY")])
def test_gemini_retries_transient_errors(first):
    s = FakeSession([first, gok('{"y": 2}')])
    assert llm.Gemini("k", session=s).json("s", "u") == {"y": 2}
    assert len(s.posts) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_gemini_does_not_retry_client_errors(status):
    s = FakeSession([FakeResponse(status, text="bad"), gok("{}")])
    with pytest.raises(llm.LlmError, match="Gemini call failed"):
        llm.Gemini("k", session=s).json("s", "u")
    assert len(s.posts) == 1


def test_gemini_gives_up_after_retries():
    s = FakeSession([FakeResponse(500)] * 3)
    with pytest.raises(llm.LlmError):
        llm.Gemini("k", session=s, retries=3).json("s", "u")
    assert len(s.posts) == 3


def test_gemini_empty_reply_names_the_reason():
    s = FakeSession([gok("", finish="MAX_TOKENS")] * 3)
    with pytest.raises(llm.LlmError, match="MAX_TOKENS"):
        llm.Gemini("k", session=s).json("s", "u")


def test_gemini_call_budget():
    s = FakeSession([gok("{}"), gok("{}")])
    g = llm.Gemini("k", session=s, max_calls=1)
    g.json("s", "u")
    with pytest.raises(llm.LlmError, match="budget"):
        g.json("s", "u")


def test_gemini_missing_key():
    with pytest.raises(llm.LlmError, match="GEMINI_API_KEY"):
        llm.Gemini()


# --- roles on DeepSeek -----------------------------------------------------

def test_deepseek_roles():
    from tests.test_sources_llm_fetch import ok
    s = FakeSession([ok("{}"), ok("{}"), ok("{}")])
    d = llm.DeepSeek("k", session=s)
    d.json("s", "u")
    d.json("s", "u", model=llm.EXTRACT)
    d.json("s", "u", model=llm.VERIFY)
    assert [p[1]["model"] for p in s.posts] == [d.extract_model, d.extract_model, d.verify_model]
    assert d.verify_model == "deepseek-v4-pro" and d.name == "deepseek"


# --- choosing a provider ---------------------------------------------------

@pytest.mark.parametrize("env,expected", [
    ({"ANTHROPIC_API_KEY": "a"}, llm.Claude),
    ({"GEMINI_API_KEY": "g"}, llm.Gemini),
    ({"DEEPSEEK_API_KEY": "d"}, llm.DeepSeek),
    ({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g", "DEEPSEEK_API_KEY": "d"}, llm.Claude),
    ({"GEMINI_API_KEY": "g", "DEEPSEEK_API_KEY": "d"}, llm.Gemini),
    ({"ANTHROPIC_API_KEY": "", "DEEPSEEK_API_KEY": "d"}, llm.DeepSeek),
])
def test_make_llm_picks_first_key(env, expected):
    assert type(llm.make_llm(env)) is expected


@pytest.mark.parametrize("choice,expected", [("claude", llm.Claude), ("gemini", llm.Gemini),
                                             ("deepseek", llm.DeepSeek), (" Gemini ", llm.Gemini),
                                             ("CLAUDE", llm.Claude)])
def test_make_llm_follows_provider_choice(choice, expected):
    env = {"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g", "DEEPSEEK_API_KEY": "d", "LLM_PROVIDER": choice}
    assert type(llm.make_llm(env)) is expected


def test_make_llm_chosen_provider_needs_its_key():
    with pytest.raises(llm.LlmError, match="GEMINI_API_KEY is not set"):
        llm.make_llm({"LLM_PROVIDER": "gemini", "ANTHROPIC_API_KEY": "a"})


def test_make_llm_unknown_provider():
    with pytest.raises(llm.LlmError, match="LLM_PROVIDER must be one of"):
        llm.make_llm({"LLM_PROVIDER": "openai", "ANTHROPIC_API_KEY": "a"})


@pytest.mark.parametrize("env", [{}, {"LLM_PROVIDER": ""}, {"ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": ""}])
def test_make_llm_without_any_key(env):
    with pytest.raises(llm.LlmError, match="no AI key is set"):
        llm.make_llm(env)


def test_make_llm_reads_process_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert isinstance(llm.make_llm(), llm.Gemini)


def test_make_llm_passes_the_key():
    c = llm.make_llm({"GEMINI_API_KEY": "secret-g"})
    assert c._key == "secret-g"
