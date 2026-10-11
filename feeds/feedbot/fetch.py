"""Polite HTTP fetching of official pages and notices, plus text extraction."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# A browser-like UA that names Chrome or a bot gets "403 Access Denied" from
# PIB and UPSC; the bare token is accepted everywhere we read.
UA = "Mozilla/5.0"
MAX_BYTES = 25 * 1024 * 1024


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    body: bytes

    @property
    def is_pdf(self) -> bool:
        if self.body[:5] == b"%PDF-" or "pdf" in self.content_type:
            return True
        if self.body.lstrip()[:1] == b"<":
            return False  # an HTML page (often an error page) at a .pdf address
        return self.url.lower().split("?")[0].endswith(".pdf")

    def text(self) -> str:
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return self.body.decode(enc)
            except UnicodeDecodeError:
                continue
        return self.body.decode("utf-8", "replace")


class Fetcher:
    """Fetches pages directly, or through the India relay (feeds/relay) when
    FETCH_RELAY_URL and FETCH_RELAY_TOKEN are set, because many Indian
    government sites refuse servers outside India."""

    def __init__(self, *, timeout: float = 40, retries: int = 2, delay: float = 1.0,
                 session: requests.Session | None = None, relay_url: str | None = None,
                 relay_token: str | None = None) -> None:
        self._http = session or requests.Session()
        self._http.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"})
        self._timeout = timeout
        self._retries = retries
        self._delay = delay
        self._relay = (relay_url if relay_url is not None else os.environ.get("FETCH_RELAY_URL", "")).strip()
        self._relay_token = (relay_token if relay_token is not None
                             else os.environ.get("FETCH_RELAY_TOKEN", "")).strip()

    @property
    def relayed(self) -> bool:
        return bool(self._relay and self._relay_token)

    def get(self, url: str) -> Page:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                page = self._via_relay(url) if self.relayed else None
                if page is None:
                    page = self._direct(url)
                time.sleep(self._delay)
                return page
            except requests.RequestException as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise requests.RequestException(f"{url}: {last}")

    def _read(self, r: requests.Response) -> bytes:
        chunks, size = [], 0
        for chunk in r.iter_content(64 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                raise requests.RequestException("file too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _direct(self, url: str) -> Page:
        r = self._http.get(url, timeout=self._timeout, stream=True, allow_redirects=True)
        return Page(r.url, r.status_code, r.headers.get("content-type", "").lower(), self._read(r))

    def _via_relay(self, url: str) -> Page | None:
        """The page through the relay; None when the relay won't fetch this host."""
        r = self._http.post(self._relay, json={"url": url}, timeout=self._timeout + 30, stream=True,
                            headers={"Authorization": f"Bearer {self._relay_token}"})
        if r.status_code == 403:
            return None  # not an official domain the relay serves: fetch it directly
        if r.status_code != 200:
            raise requests.RequestException(f"relay HTTP {r.status_code}: {r.text[:200]}")
        try:
            status = int(r.headers.get("x-relay-status", "200"))
        except ValueError:
            status = 200
        return Page(r.headers.get("x-relay-url") or url, status,
                    r.headers.get("content-type", "").lower(), self._read(r))


def html_text(html: str) -> str:
    """Readable text of an HTML page (scripts, styles and navigation removed)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "svg"]):
        tag.decompose()
    # ASP.NET pages (PIB, Employment News) wrap the whole page in one <form>:
    # keep that one's content, drop small forms such as search boxes.
    total = len(soup.get_text(strip=True))
    for tag in soup("form"):
        if len(tag.get_text(strip=True)) > total / 2:
            tag.unwrap()
        else:
            tag.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def html_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(link text, absolute URL) for every link on the page, in page order."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).scheme not in ("http", "https") or url in seen:
            continue
        seen.add(url)
        text = " ".join(a.get_text(" ").split())
        if not text:
            text = a.get("title") or a.get("aria-label") or ""
        out.append((text.strip(), url))
    return out


def pdf_text(data: bytes) -> str:
    """Text of a PDF; scanned PDFs fall back to OCR (English + Hindi) when available."""
    text = ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages[:40]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # a broken page must not lose the rest
                parts.append("")
        text = "\n".join(parts).strip()
    except Exception:
        text = ""
    if len(text) >= 300:
        return text
    return ocr_pdf(data) or text


def ocr_pdf(data: bytes, *, max_pages: int = 12) -> str:
    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp, "in.pdf")
        pdf.write_bytes(data)
        try:
            subprocess.run(["pdftoppm", "-r", "200", "-l", str(max_pages), "-png", str(pdf), str(Path(tmp, "p"))],
                           check=True, capture_output=True, timeout=300)
        except (subprocess.SubprocessError, OSError):
            return ""
        out = []
        for img in sorted(Path(tmp).glob("p*.png")):
            try:
                r = subprocess.run(["tesseract", str(img), "-", "-l", "eng+hin"],
                                   capture_output=True, timeout=180, text=True)
                out.append(r.stdout)
            except (subprocess.SubprocessError, OSError):
                continue
        return "\n".join(out).strip()


def document_text(page: Page) -> tuple[str, list[tuple[str, str]]]:
    """Text and links of a fetched notice (HTML page or PDF)."""
    if page.is_pdf:
        return pdf_text(page.body), []
    html = page.text()
    return html_text(html), html_links(html, page.url)
