"""One unusable AI reply skips its item; it does not stop the run."""

import pytest

from conftest import NOTICE, NOW, FakeLlm
from feedbot import __main__ as main_mod
from feedbot import llm
from feedbot.fetch import Page
from feedbot.sources import Source
from feedbot.store import Store

SRC = Source(id="ssc", name="Staff Selection Commission", url="https://ssc.gov.in/notices", category="ssc")


def test_stray_backslashes_are_repaired():
    # the 13:30 run died on: Invalid \escape
    reply = 'Here: {"title": "Post_Code\\_12", "path": "C:\\x\\y", "ok": "a\\nb", "q": "say \\"hi\\""}'
    assert llm.parse_json_reply(reply) == {"title": "Post_Code\\_12", "path": "C:\\x\\y", "ok": "a\nb", "q": 'say "hi"'}


def test_valid_escapes_are_untouched():
    assert llm.parse_json_reply('{"a": "x\\\\_y", "b": "\\u0915"}') == {"a": "x\\_y", "b": "\u0915"}


@pytest.mark.parametrize("text", ["no json here", "[1, 2]", '{"a": "unterminated'])
def test_unusable_replies_are_reply_errors(text):
    with pytest.raises(llm.LlmReplyError):
        llm.parse_json_reply(text)


class BadThenGoodLlm(FakeLlm):
    """First notice gets an unusable reply; later calls behave normally."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.failed = False

    def json(self, system, user, *, model=None, max_tokens=4000):
        if not self.failed:
            self.failed = True
            self.calls.append((system, user, model))
            raise llm.LlmReplyError("reply JSON is invalid: Invalid \\escape")
        return super().json(system, user, model=model, max_tokens=max_tokens)


def page(n):
    links = "".join(f'<a href="/n{i}.pdf">Recruitment notice number {i} for posts</a>' for i in range(n))
    return f"<html><body>{links}</body></html>"


class Fetch:
    def get(self, url):
        if url == SRC.url:
            return Page(url, 200, "text/html", page(2).encode())
        return Page(url, 200, "text/html", f"<html><body><pre>{NOTICE}</pre></body></html>".encode())


def test_one_bad_reply_skips_only_its_notice(tmp_path):
    from conftest import good_extraction
    store, status = Store(tmp_path), {"sources": {}}
    fake = BadThenGoodLlm(extract=[good_extraction()])
    main_mod.run_jobs(store, fake, Fetch(), [SRC], NOW, status)
    seen = store.read("state/seen.json")
    assert sorted(v["status"] for v in seen.values())[0] == "error"
    assert len(seen) == 2, "the second notice was still processed"


def test_other_llm_errors_still_stop_the_run(tmp_path):
    class Broken(FakeLlm):
        def json(self, *a, **k):
            raise llm.LlmError("Claude call failed: HTTP 401")
    with pytest.raises(llm.LlmError):
        main_mod.run_jobs(Store(tmp_path), Broken(), Fetch(), [SRC], NOW, {"sources": {}})
