"""India relay: fetches official Indian pages for the feeds pipeline.

Many Indian government sites block or time out servers outside India, so the
pipeline on GitHub asks this small service (on Cloud Run in Mumbai) to fetch
pages for it. It only fetches public pages on allowed official domains, and
only for callers holding RELAY_TOKEN.

POST / with header "Authorization: Bearer <token>" and JSON {"url": "..."}.
The reply body is the page; X-Relay-Status and X-Relay-Url carry the site's
HTTP status and final address.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse

import requests

MAX_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = 40
# A user agent that names Chrome or a bot gets "403 Access Denied" from PIB
# and UPSC; the bare token is accepted everywhere we read.
UA = "Mozilla/5.0"
# Official domains: any *.gov.in / *.nic.in, plus the boards and banks on other domains.
ALLOWED_SUFFIXES = (".gov.in", ".nic.in", ".ac.in", ".org.in")
ALLOWED_HOSTS = {"ibps.in", "sbi.co.in", "bank.sbi", "nta.ac.in"}


_resolve = socket.getaddrinfo


class Refused(Exception):
    pass


def host_allowed(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return False  # names only, never raw addresses
    except ValueError:
        pass
    extra = {h.strip().lower() for h in os.environ.get("RELAY_EXTRA_HOSTS", "").split(",") if h.strip()}
    for allowed in ALLOWED_HOSTS | extra:
        if host == allowed or host.endswith("." + allowed):
            return True
    return host.endswith(ALLOWED_SUFFIXES)


def public_address(host: str, port: int) -> bool:
    """Every address the name resolves to is on the public internet."""
    try:
        infos = _resolve(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return True  # let requests report the DNS error itself
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def check_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise Refused("only http and https")
    if not host_allowed(p.hostname):
        raise Refused(f"host not allowed: {p.hostname}")
    if not public_address(p.hostname, p.port or (443 if p.scheme == "https" else 80)):
        raise Refused("address is not public")


def fetch(url: str, session: requests.Session | None = None) -> tuple[int, str, str, bytes]:
    """(status, final url, content type, body), following redirects only to allowed hosts."""
    http = session or requests.Session()
    headers = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"}
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        r = http.get(url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=False)
        if r.is_redirect and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            r.close()
            continue
        chunks, size = [], 0
        for chunk in r.iter_content(64 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                raise Refused("file too large")
            chunks.append(chunk)
        return r.status_code, url, r.headers.get("content-type", ""), b"".join(chunks)
    raise Refused("too many redirects")


class Handler(BaseHTTPRequestHandler):
    server_version = "feeds-relay"

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # health check
        self._send(200, b"ok")

    def do_POST(self) -> None:
        token = os.environ.get("RELAY_TOKEN", "")
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(given.encode(), token.encode()):
            self._send(401, b"unauthorized")
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 8192)
            url = str(json.loads(self.rfile.read(length) or b"{}").get("url") or "")
            status, final, ctype, body = fetch(url)
        except Refused as e:
            self._send(403, str(e).encode())
            return
        except (ValueError, AttributeError):
            self._send(400, b"send JSON {\"url\": ...}")
            return
        except requests.RequestException as e:
            self._send(502, str(e)[:500].encode())
            return
        self._send(200, body, ctype or "application/octet-stream",
                   {"X-Relay-Status": str(status), "X-Relay-Url": final})

    def log_message(self, fmt: str, *args) -> None:  # one short line per request
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
