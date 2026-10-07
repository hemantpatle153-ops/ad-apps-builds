"""Source list and the cheap rules that pick candidate notice links."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .textnorm import normalize

SOURCES_FILE = Path(__file__).resolve().parent.parent / "sources.json"

CATEGORIES = {"ssc", "railway", "banking", "upsc", "defence", "police", "teaching",
              "state_psc", "psu", "medical", "engineering", "other"}


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    kind: str = "html_links"
    category: str = "other"
    states: tuple[str, ...] = ("all",)
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc.lower()


def load_sources(path: Path = SOURCES_FILE) -> tuple[list[Source], list[Source]]:
    data = json.loads(path.read_text(encoding="utf-8"))

    def mk(e: dict) -> Source:
        cat = e.get("category", "other")
        return Source(id=e["id"], name=e["name"], url=e["url"], kind=e.get("kind", "html_links"),
                      category=cat if cat in CATEGORIES else "other",
                      states=tuple(e.get("states") or ("all",)), extra=e)

    return [mk(e) for e in data.get("jobs", [])], [mk(e) for e in data.get("currentAffairs", [])]


_KEEP = re.compile(
    r"recruit|vacanc|advertis|advt|notification|notice|employment|walk.?in|"
    r"admit|e-?admit|call letter|hall ticket|result|marks|answer.?key|cut.?off|syllabus|"
    r"examination|exam |exam$|interview|selection|apply|application|post of|posts|"
    r"भर्ती|विज्ञापन|परिणाम|प्रवेश पत्र|उत्तर कुंजी|परीक्षा|अधिसूचना|रिक्ति|पाठ्यक्रम|साक्षात्कार")
_DROP = re.compile(
    r"\b(home|contact|about us|sitemap|site map|rti|tender|feedback|login|faq|help|"
    r"screen reader|skip to|privacy|terms|disclaimer|copyright|hyperlink|archive|gallery|photo|"
    r"video|twitter|facebook|youtube|instagram|linkedin|telephone|directory|annual report|"
    r"citizen charter|grievance|accessibility|web information manager)\b")
_DOC = re.compile(r"\.(pdf|docx?)(\?|$)", re.I)


def is_candidate(text: str, url: str, source: Source) -> bool:
    """True for links that look like job notices, admit cards or results."""
    t = normalize(text)
    path = urlparse(url).path.lower()
    host = urlparse(url).netloc.lower()
    if _DROP.search(t) and not _KEEP.search(t):
        return False
    if any(s in host for s in ("facebook.", "twitter.", "x.com", "youtube.", "instagram.", "linkedin.", "whatsapp.")):
        return False
    if not (host.endswith(".gov.in") or host.endswith(".nic.in") or host.endswith(".org.in")
            or host.endswith(".ac.in") or host.endswith(".co.in") or host == source.host
            or host.endswith(source.host.removeprefix("www."))):
        return False
    if _KEEP.search(t) or _KEEP.search(path):
        return len(t) >= 6 or bool(_DOC.search(path))
    return bool(_DOC.search(path)) and len(t) >= 12


def link_id(url: str) -> str:
    """Stable key for a link (query order and fragments ignored)."""
    p = urlparse(url.strip())
    q = "&".join(sorted(x for x in p.query.split("&") if x))
    key = f"{p.netloc.lower()}{p.path}?{q}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]
