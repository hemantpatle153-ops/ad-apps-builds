"""The India relay service (feeds/relay) and the pipeline's use of it."""

import json
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "relay"))
import relay  # noqa: E402

from feedbot import fetch  # noqa: E402


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Names resolve to a public address unless a test says otherwise."""
    monkeypatch.setattr(relay, "_resolve",
                        lambda host, port, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("164.100.1.1", port))])


# ---- which hosts the relay serves ----

@pytest.mark.parametrize("host", [
    "upsc.gov.in", "www.upsc.gov.in", "uppsc.up.nic.in", "pib.gov.in", "nta.ac.in", "opportunities.rbi.org.in",
    "www.ibps.in", "ibps.in", "sbi.co.in", "UPSC.GOV.IN", "upsc.gov.in.",
])
def test_allowed_hosts(host):
    assert relay.host_allowed(host)


@pytest.mark.parametrize("host", [
    None, "", "example.com", "gov.in.evil.com", "evilgov.in", "metadata.google.internal", "localhost",
    "169.254.169.254", "127.0.0.1", "10.0.0.1", "::1", "notibps.in", "sbi.co.in.evil.net",
])
def test_refused_hosts(host):
    assert not relay.host_allowed(host)


def test_extra_hosts_from_env(monkeypatch):
    assert not relay.host_allowed("www.example-board.com")
    monkeypatch.setenv("RELAY_EXTRA_HOSTS", " example-board.com , other.org ")
    assert relay.host_allowed("www.example-board.com") and relay.host_allowed("other.org")


@pytest.mark.parametrize("url", ["ftp://upsc.gov.in/a", "file:///etc/passwd", "gopher://upsc.gov.in", "upsc.gov.in/x"])
def test_check_url_refuses_other_schemes(url):
    with pytest.raises(relay.Refused):
        relay.check_url(url)


@pytest.mark.parametrize("addr", ["10.1.2.3", "127.0.0.1", "169.254.169.254", "192.168.1.1", "100.64.0.1"])
def test_check_url_refuses_names_pointing_inside(monkeypatch, addr):
    monkeypatch.setattr(relay, "_resolve",
                        lambda host, port, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))])
    with pytest.raises(relay.Refused, match="not public"):
        relay.check_url("https://sneaky.gov.in/")


def test_check_url_leaves_dns_errors_to_requests(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("no such name")
    monkeypatch.setattr(relay, "_resolve", boom)
    relay.check_url("https://gone.gov.in/")  # no Refused


def test_check_url_accepts_official_page():
    relay.check_url("https://upsc.gov.in/whats-new")


# ---- fetching with redirects checked hop by hop ----

class Resp:
    def __init__(self, status=200, body=b"page", headers=None):
        self.status_code, self.body = status, body
        self.headers = {"content-type": "text/html", **(headers or {})}
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses, self.urls, self.kw = list(responses), [], []

    def get(self, url, **kw):
        self.urls.append(url)
        self.kw.append(kw)
        return self.responses.pop(0)


def test_fetch_returns_page():
    s = Session(Resp(body=b"hello"))
    assert relay.fetch("https://upsc.gov.in/a", s) == (200, "https://upsc.gov.in/a", "text/html", b"hello")
    assert s.kw[0]["allow_redirects"] is False and "Bot" not in s.kw[0]["headers"]["User-Agent"]


def test_fetch_follows_allowed_redirect():
    s = Session(Resp(302, headers={"location": "/new"}), Resp(body=b"moved"))
    status, final, _, body = relay.fetch("https://upsc.gov.in/old", s)
    assert (status, final, body) == (200, "https://upsc.gov.in/new", b"moved")
    assert s.urls == ["https://upsc.gov.in/old", "https://upsc.gov.in/new"]


def test_fetch_refuses_redirect_off_allowed_hosts():
    s = Session(Resp(302, headers={"location": "http://169.254.169.254/computeMetadata/v1/"}))
    with pytest.raises(relay.Refused):
        relay.fetch("https://upsc.gov.in/old", s)
    assert len(s.urls) == 1


def test_fetch_stops_redirect_loops():
    s = Session(*[Resp(302, headers={"location": "/loop"}) for _ in range(relay.MAX_REDIRECTS + 1)])
    with pytest.raises(relay.Refused, match="too many"):
        relay.fetch("https://upsc.gov.in/loop", s)


def test_fetch_passes_site_errors_through():
    s = Session(Resp(404, body=b"not here"))
    assert relay.fetch("https://upsc.gov.in/a", s)[0] == 404


def test_fetch_refuses_huge_files(monkeypatch):
    monkeypatch.setattr(relay, "MAX_BYTES", 10)
    with pytest.raises(relay.Refused, match="too large"):
        relay.fetch("https://upsc.gov.in/a", Session(Resp(body=b"x" * 100)))


# ---- the HTTP service ----

@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("RELAY_TOKEN", "secret-token")
    calls = []

    def fake_fetch(url, session=None):
        calls.append(url)
        if "refuse" in url:
            raise relay.Refused("host not allowed: x")
        if "down" in url:
            raise requests.ConnectionError("site down")
        return 404 if "missing" in url else 200, url + "?final", "application/pdf", b"%PDF-1.4 data"

    monkeypatch.setattr(relay, "fetch", fake_fetch)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", calls
    srv.shutdown()


def post(base, body, token="secret-token"):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return requests.post(base, data=data, headers=headers, timeout=10)


def test_service_health(server):
    assert requests.get(server[0], timeout=10).text == "ok"


def test_service_relays_page(server):
    r = post(server[0], {"url": "https://upsc.gov.in/a.pdf"})
    assert r.status_code == 200 and r.content == b"%PDF-1.4 data"
    assert r.headers["X-Relay-Status"] == "200" and r.headers["X-Relay-Url"] == "https://upsc.gov.in/a.pdf?final"
    assert r.headers["Content-Type"] == "application/pdf"


def test_service_passes_site_status(server):
    assert post(server[0], {"url": "https://upsc.gov.in/missing"}).headers["X-Relay-Status"] == "404"


@pytest.mark.parametrize("token", [None, "", "wrong", "secret-token-x"])
def test_service_needs_token(server, token):
    r = post(server[0], {"url": "https://upsc.gov.in/a"}, token=token)
    assert r.status_code == 401 and server[1] == []


def test_service_refuses_when_no_token_configured(server, monkeypatch):
    monkeypatch.setenv("RELAY_TOKEN", "")
    assert post(server[0], {"url": "https://upsc.gov.in/a"}, token="").status_code == 401


def test_service_refused_host_is_403(server):
    assert post(server[0], {"url": "https://refuse.example/"}).status_code == 403


def test_service_site_down_is_502(server):
    r = post(server[0], {"url": "https://down.gov.in/"})
    assert r.status_code == 502 and "site down" in r.text


@pytest.mark.parametrize("body", [b"not json", b"[1,2]"])
def test_service_bad_body_is_400(server, body):
    assert post(server[0], body).status_code == 400


# ---- the pipeline's Fetcher using the relay ----

class RelaySession:
    def __init__(self, *responses):
        self.responses, self.posts, self.gets, self.headers = list(responses), [], [], {}

    def post(self, url, **kw):
        self.posts.append((url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, **kw):
        self.gets.append(url)
        return DirectResp(url)


class RelayResp:
    def __init__(self, status=200, body=b"page", headers=None):
        self.status_code, self.body = status, body
        self.headers = {"content-type": "text/html", **(headers or {})}
        self.text = body.decode("utf-8", "replace")

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]


class DirectResp(RelayResp):
    def __init__(self, url):
        super().__init__(body=b"direct")
        self.url = url


def relayed(session):
    return fetch.Fetcher(session=session, delay=0, retries=0, relay_url="https://relay.run.app",
                         relay_token="tok")


def test_fetcher_without_relay_goes_direct():
    s = RelaySession()
    f = fetch.Fetcher(session=s, delay=0)
    assert not f.relayed
    assert f.get("https://upsc.gov.in/a").body == b"direct" and s.posts == []


def test_fetcher_reads_relay_from_env(monkeypatch):
    monkeypatch.setenv("FETCH_RELAY_URL", " https://relay.run.app ")
    monkeypatch.setenv("FETCH_RELAY_TOKEN", "tok")
    assert fetch.Fetcher(session=RelaySession()).relayed
    monkeypatch.setenv("FETCH_RELAY_TOKEN", "")
    assert not fetch.Fetcher(session=RelaySession()).relayed


def test_fetcher_uses_relay():
    s = RelaySession(RelayResp(body=b"%PDF-1.4", headers={"content-type": "application/pdf",
                                                         "x-relay-status": "200",
                                                         "x-relay-url": "https://upsc.gov.in/b.pdf"}))
    p = relayed(s).get("https://upsc.gov.in/a")
    assert (p.url, p.status, p.body, p.is_pdf) == ("https://upsc.gov.in/b.pdf", 200, b"%PDF-1.4", True)
    url, kw = s.posts[0]
    assert url == "https://relay.run.app" and kw["json"] == {"url": "https://upsc.gov.in/a"}
    assert kw["headers"]["Authorization"] == "Bearer tok" and s.gets == []


def test_fetcher_keeps_site_status_from_relay():
    s = RelaySession(RelayResp(body=b"gone", headers={"x-relay-status": "404"}))
    assert relayed(s).get("https://upsc.gov.in/a").status == 404


def test_fetcher_bad_status_header_means_ok():
    s = RelaySession(RelayResp(headers={"x-relay-status": "abc"}))
    assert relayed(s).get("https://upsc.gov.in/a").status == 200


def test_fetcher_falls_back_to_direct_for_hosts_the_relay_refuses():
    s = RelaySession(RelayResp(403, b"host not allowed"))
    p = relayed(s).get("https://www.example.com/x")
    assert p.body == b"direct" and s.gets == ["https://www.example.com/x"]


@pytest.mark.parametrize("status", [401, 502, 500])
def test_fetcher_relay_errors_raise(status, monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    s = RelaySession(RelayResp(status, b"nope"), RelayResp(status, b"nope"))
    f = fetch.Fetcher(session=s, delay=0, retries=1, relay_url="https://relay.run.app", relay_token="tok")
    with pytest.raises(requests.RequestException, match=f"relay HTTP {status}"):
        f.get("https://upsc.gov.in/a")
    assert len(s.posts) == 2 and s.gets == []


def test_fetcher_relay_retries_after_network_error(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    s = RelaySession(requests.ConnectionError("x"), RelayResp(body=b"second"))
    f = fetch.Fetcher(session=s, delay=0, retries=1, relay_url="https://relay.run.app", relay_token="tok")
    assert f.get("https://upsc.gov.in/a").body == b"second"


def test_fetcher_relay_rejects_huge_files(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    monkeypatch.setattr(fetch, "MAX_BYTES", 10)
    s = RelaySession(RelayResp(body=b"x" * 100))
    with pytest.raises(requests.RequestException):
        relayed(s).get("https://upsc.gov.in/a")
