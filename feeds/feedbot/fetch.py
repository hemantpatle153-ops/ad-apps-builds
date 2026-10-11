"""Polite HTTP fetching of official pages and notices, plus text extraction."""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Mobile Safari/537.36 VacancyBellBot/1.0 (+https://github.com/hemantpatle153-ops/ad-apps-builds)")
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
    def __init__(self, *, timeout: float = 40, retries: int = 2, delay: float = 1.0,
                 session: requests.Session | None = None) -> None:
        self._http = session or requests.Session()
        self._http.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"})
        self._timeout = timeout
        self._retries = retries
        self._delay = delay

    def get(self, url: str) -> Page:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                r = self._http.get(url, timeout=self._timeout, stream=True, allow_redirects=True)
                chunks, size = [], 0
                for chunk in r.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise requests.RequestException("file too large")
                    chunks.append(chunk)
                time.sleep(self._delay)
                return Page(r.url, r.status_code, r.headers.get("content-type", "").lower(), b"".join(chunks))
            except requests.RequestException as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise requests.RequestException(f"{url}: {last}")


def html_text(html: str) -> str:
    """Readable text of an HTML page (scripts, styles and navigation removed)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "svg", "form"]):
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
