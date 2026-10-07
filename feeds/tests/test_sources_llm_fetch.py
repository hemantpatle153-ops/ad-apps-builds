import json
from urllib.parse import urlparse

import pytest
import requests

from feedbot import fetch, llm
from feedbot.sources import CATEGORIES, Source, is_candidate, link_id, load_sources

SRC = Source(id="upsc", name="UPSC", url="https://upsc.gov.in/whats-new", category="upsc")

KEEP = [
    ("Combined Geo-Scientist Examination, 2027 - Notification", "https://upsc.gov.in/sites/default/files/Notif-CGSE-2027.pdf"),
    ("Recruitment of Assistant Professor (Advt No. 12/2026)", "https://upsc.gov.in/recruitment/advt-12-2026"),
    ("e-Admit Card: Civil Services (Main) Examination, 2026", "https://upsconline.nic.in/eadmitcard/index.php"),
    ("Final Result: Engineering Services Examination, 2026", "https://upsc.gov.in/sites/default/files/FR-ESE-2026.pdf"),
    ("Answer Key of NDA (I) 2026", "https://upsc.gov.in/sites/default/files/AK-NDA-I-2026.pdf"),
    ("Syllabus for CDS Exam", "https://upsc.gov.in/syllabus-cds.pdf"),
    ("Walk-in interview for Data Entry Operator", "https://upsc.gov.in/walkin.pdf"),
    ("भर्ती विज्ञापन संख्या 05/2026", "https://upsc.gov.in/hindi/advt-05.pdf"),
    ("परीक्षा परिणाम घोषित", "https://upsc.gov.in/hindi/result.pdf"),
    ("Cut-off marks of CSE 2025", "https://upsc.gov.in/cutoff-cse-2025.pdf"),
    ("Detailed Application Form - Indian Forest Service", "https://upsc.gov.in/daf-ifs.pdf"),
    ("Notice regarding change in exam date of IES/ISS 2026", "https://upsc.gov.in/notice-ies.pdf"),
    ("Corrigendum to advertisement 10/2026", "https://upsc.gov.in/corrigendum-10-2026.pdf"),
    ("Schedule of interview for Specialist Grade III", "https://upsc.gov.in/interview-sched.pdf"),
    ("Marks of recommended candidates CSE 2025 published today", "https://upsc.gov.in/marks-cse-2025.pdf"),
]

DROP = [
    ("Home", "https://upsc.gov.in/"),
    ("Contact Us", "https://upsc.gov.in/contact"),
    ("RTI", "https://upsc.gov.in/rti"),
    ("Tenders", "https://upsc.gov.in/tenders"),
    ("Facebook", "https://facebook.com/upsc"),
    ("Follow us on Twitter", "https://x.com/upsc"),
    ("Recruitment", "https://www.facebook.com/upsc/recruitment"),
    ("Sitemap", "https://upsc.gov.in/sitemap"),
    ("Screen Reader Access", "https://upsc.gov.in/screen-reader"),
    ("Annual Report 2024-25", "https://upsc.gov.in/annual-report.pdf"),
    ("Photo Gallery", "https://upsc.gov.in/gallery"),
    ("Recruitment result", "https://some-private-jobs-blog.com/upsc-result"),
    ("Exam", "https://upsc.gov.in/x"),
    ("Hindi", "https://upsc.gov.in/hi"),
    ("Privacy Policy", "https://upsc.gov.in/privacy"),
    ("Login", "https://upsconline.nic.in/login"),
    ("Archive", "https://upsc.gov.in/archive"),
    ("Web Information Manager", "https://upsc.gov.in/wim"),
]


@pytest.mark.parametrize("text,url", KEEP)
def test_candidate_links_kept(text, url):
    assert is_candidate(text, url, SRC)


@pytest.mark.parametrize("text,url", DROP)
def test_navigation_links_dropped(text, url):
    assert not is_candidate(text, url, SRC)


@pytest.mark.parametrize("a,b,same", [
    ("https://x.gov.in/a.pdf", "https://X.gov.in/a.pdf", True),
    ("https://x.gov.in/a?b=1&c=2", "https://x.gov.in/a?c=2&b=1", True),
    ("https://x.gov.in/a#top", "https://x.gov.in/a", True),
    ("https://x.gov.in/a.pdf", "https://x.gov.in/b.pdf", False),
    ("https://x.gov.in/a?id=1", "https://x.gov.in/a?id=2", False),
])
def test_link_ids(a, b, same):
    assert (link_id(a) == link_id(b)) is same
    assert len(link_id(a)) == 16


def test_sources_file_is_valid():
    jobs_src, ca_src = load_sources()
    ids = [s.id for s in jobs_src + ca_src]
    assert len(ids) == len(set(ids)) and len(jobs_src) >= 10 and ca_src
    for s in jobs_src + ca_src:
        assert urlparse(s.url).scheme == "https" and s.name and s.kind in ("html_links", "rss")
    for s in jobs_src:
        assert s.category in CATEGORIES and s.states


def test_unknown_category_becomes_other(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"jobs": [{"id": "a", "name": "A", "url": "https://a.gov.in", "category": "zzz"}]}))
    jobs_src, ca_src = load_sources(p)
    assert jobs_src[0].category == "other" and jobs_src[0].states == ("all",) and ca_src == []


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 2}\n```', {"a": 2}),
    ('Here you go: {"a": 3} thanks', {"a": 3}),
    ('```\n{"a": [1, 2]}\n```', {"a": [1, 2]}),
    ('  {"हिंदी": "हाँ"} ', {"हिंदी": "हाँ"}),
])
def test_parse_json_reply(text, expected):
    assert llm.parse_json_reply(text) == expected


@pytest.mark.parametrize("text", ["", "no json", "[1, 2]", '{"a": }', "{broken"])
def test_parse_json_reply_errors(text):
    with pytest.raises(llm.LlmError):
        llm.parse_json_reply(text)


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append((url, json, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(content, usage=None):
    return FakeResponse(200, {"choices": [{"message": {"content": content}}], "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)


def test_deepseek_sends_json_mode_and_key():
    s = FakeSession([ok('{"x": 1}')])
    c = llm.DeepSeek("sk-test", session=s)
    assert c.json("sys", "user") == {"x": 1}
    url, body, headers = s.posts[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
    assert body["model"] == llm.EXTRACT_MODEL and headers["Authorization"] == "Bearer sk-test"
    assert c.usage.calls == 1 and c.usage.prompt_tokens == 10 and c.usage.completion_tokens == 5


@pytest.mark.parametrize("first", [FakeResponse(429), FakeResponse(503), requests.ConnectionError("down"), ok("not json")])
def test_deepseek_retries_transient_errors(first):
    s = FakeSession([first, ok('{"y": 2}')])
    assert llm.DeepSeek("k", session=s).json("s", "u", model="deepseek-v4-pro") == {"y": 2}
    assert s.posts[-1][1]["model"] == "deepseek-v4-pro"


def test_deepseek_does_not_retry_bad_request():
    s = FakeSession([FakeResponse(400, text="bad"), ok("{}")])
    with pytest.raises(llm.LlmError):
        llm.DeepSeek("k", session=s).json("s", "u")
    assert len(s.posts) == 1


def test_deepseek_gives_up_after_retries():
    s = FakeSession([FakeResponse(500)] * 3)
    with pytest.raises(llm.LlmError):
        llm.DeepSeek("k", session=s, retries=3).json("s", "u")


def test_deepseek_call_budget():
    s = FakeSession([ok("{}"), ok("{}")])
    c = llm.DeepSeek("k", session=s, max_calls=1)
    c.json("s", "u")
    with pytest.raises(llm.LlmError, match="budget"):
        c.json("s", "u")


def test_missing_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(llm.LlmError, match="DEEPSEEK_API_KEY"):
        llm.DeepSeek()


HTML = """<html><head><script>var x=1</script><style>.a{}</style></head><body>
<nav><a href="/">Home</a></nav>
<h1>What's New</h1>
<ul>
<li><a href="/files/notice.pdf">Notification: Exam 2026</a></li>
<li><a href="https://upsconline.nic.in/eadmit">e-Admit Card</a></li>
<li><a href="#top">Top</a></li><li><a href="javascript:void(0)">JS</a></li>
<li><a href="mailto:a@b.c">Mail</a></li>
<li><a href="/files/notice.pdf">Duplicate</a></li>
<li><a href="/img.pdf" title="Result PDF"><img src="x.png"></a></li>
</ul><footer>Footer text</footer></body></html>"""


def test_html_links():
    links = fetch.html_links(HTML, "https://upsc.gov.in/whats-new")
    assert links == [("Home", "https://upsc.gov.in/"),
                     ("Notification: Exam 2026", "https://upsc.gov.in/files/notice.pdf"),
                     ("e-Admit Card", "https://upsconline.nic.in/eadmit"),
                     ("Result PDF", "https://upsc.gov.in/img.pdf")]


def test_html_text_drops_scripts_and_nav():
    t = fetch.html_text(HTML)
    assert "What's New" in t and "var x" not in t and "Footer" not in t and "Home" not in t


@pytest.mark.parametrize("ctype,body,url,is_pdf", [
    ("application/pdf", b"x", "https://a/b", True), ("", b"%PDF-1.7", "https://a/b", True),
    ("", b"\x00bin", "https://a/b.PDF?x=1", True), ("text/html", b"<html>", "https://a/b.pdf", False),
    ("text/html", b"<html>", "https://a/b", False),
])
def test_page_is_pdf(ctype, body, url, is_pdf):
    assert fetch.Page(url, 200, ctype, body).is_pdf is is_pdf


@pytest.mark.parametrize("body,expected", [("नमस्ते".encode(), "नमस्ते"), ("café".encode("latin-1"), "café"), (b"plain", "plain")])
def test_page_text_decoding(body, expected):
    assert fetch.Page("u", 200, "text/html", body).text() == expected


def make_pdf(text: str) -> bytes:
    """A tiny valid one-page PDF with `text` (ASCII) for extraction tests."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def test_pdf_text_extracts_long_text(monkeypatch):
    line = "Last date for online application is 05.10.2026 and total posts are 14582. " * 6
    assert "05.10.2026" in fetch.pdf_text(make_pdf(line))


def test_short_pdf_text_falls_back_to_ocr(monkeypatch):
    monkeypatch.setattr(fetch, "ocr_pdf", lambda data: "OCR TEXT")
    assert fetch.pdf_text(make_pdf("tiny")) == "OCR TEXT"


def test_broken_pdf_does_not_crash(monkeypatch):
    monkeypatch.setattr(fetch, "ocr_pdf", lambda data: "")
    assert fetch.pdf_text(b"%PDF-garbage") == ""


def test_document_text_routes_by_type(monkeypatch):
    monkeypatch.setattr(fetch, "pdf_text", lambda b: "pdf!")
    assert fetch.document_text(fetch.Page("https://a/x.pdf", 200, "application/pdf", b"%PDF")) == ("pdf!", [])
    text, links = fetch.document_text(fetch.Page("https://upsc.gov.in/a", 200, "text/html", HTML.encode()))
    assert "What's New" in text and ("e-Admit Card", "https://upsconline.nic.in/eadmit") in links


class FakeGetSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}

    def get(self, url, **kw):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class StreamResp:
    def __init__(self, body=b"ok", status=200, ctype="text/html", url="https://a.gov.in/x"):
        self.body, self.status_code, self.headers, self.url = body, status, {"content-type": ctype}, url

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]


def test_fetcher_returns_page_and_follows_final_url():
    f = fetch.Fetcher(session=FakeGetSession([StreamResp(b"hello", url="https://a.gov.in/final")]), delay=0)
    p = f.get("https://a.gov.in/x")
    assert p.body == b"hello" and p.url == "https://a.gov.in/final" and p.status == 200


def test_fetcher_retries_then_raises():
    f = fetch.Fetcher(session=FakeGetSession([requests.Timeout("t")] * 3), retries=2, delay=0)
    with pytest.raises(requests.RequestException):
        f.get("https://a.gov.in/x")


def test_fetcher_rejects_huge_files(monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 10)
    f = fetch.Fetcher(session=FakeGetSession([StreamResp(b"x" * 100)] * 3), retries=2, delay=0)
    with pytest.raises(requests.RequestException):
        f.get("https://a.gov.in/x")


def test_fetcher_sets_user_agent():
    s = FakeGetSession([])
    fetch.Fetcher(session=s)
    assert "VacancyBellBot" in s.headers["User-Agent"]
