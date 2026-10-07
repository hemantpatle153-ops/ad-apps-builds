"""Job notice -> verified post.

1. The extractor model returns every fact with an exact quote from the notice.
2. Code checks each fact: the quote must be in the notice, and the fact's
   numbers and dates must be in the quote (plus range and format rules).
3. Failed facts go back to the model with the reason, up to MAX_ROUNDS.
4. Facts that still fail are dropped (shown as "See official notice").
5. A second, stronger model independently checks the remaining facts
   against the notice; anything it can't confirm is dropped too.
6. A post missing its key facts, or losing too many, is held for review
   instead of being published.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from . import llm as llm_mod
from .sources import CATEGORIES, Source
from .textnorm import (date_supported, digits_match, has_devanagari, normalize,
                       number_supported, numbers_in, parse_iso_date, quote_in_source,
                       slugify)

MAX_ROUNDS = 3
MAX_SOURCE_CHARS = 60_000
TYPES = {"job", "admit_card", "result", "answer_key", "syllabus", "admission", "notice"}
QUALIFICATIONS = {"8th", "10th", "12th", "iti", "diploma", "graduate", "postgraduate",
                  "engineering", "medical", "law", "any"}
STATES = {"all", "AN", "AP", "AR", "AS", "BR", "CH", "CG", "DN", "DL", "GA", "GJ", "HR", "HP",
          "JK", "JH", "KA", "KL", "LA", "LD", "MP", "MH", "MN", "ML", "MZ", "NL", "OD", "PY",
          "PB", "RJ", "SK", "TN", "TS", "TR", "UP", "UK", "WB"}

EXTRACT_SYSTEM = """You turn an official Indian government notice into structured data for a job alert app.
Rules:
- Use ONLY the notice text. Never guess, never use outside knowledge. If something is not in the notice, use null or leave the list empty.
- For every fact give "quote": a short exact copy (5-40 words) of the notice text that states it, copied character for character.
- Dates as YYYY-MM-DD. Numbers as plain integers (no commas).
- Every text the user sees has "en" (English) and "hi" (natural Hindi in Devanagari). Both must carry the same numbers.
- Reply with one JSON object only."""

EXTRACT_SHAPE = """JSON shape:
{
 "relevant": true/false,            // false if this is not a recruitment, admit card, result, answer key, syllabus or admission notice for candidates
 "type": "job|admit_card|result|answer_key|syllabus|admission|notice",
 "title": {"en": "...", "hi": "..."},   // short, e.g. "SSC CGL 2026 Recruitment"
 "org": {"en": "...", "hi": "...", "quote": "..."},
 "category": "ssc|railway|banking|upsc|defence|police|teaching|state_psc|psu|medical|engineering|other",
 "shortInfo": {"en": "<= 60 words", "hi": "..."},
 "lastDate": {"value": "YYYY-MM-DD" or null, "quote": "..."},
 "totalPosts": {"value": 123 or null, "quote": "..."},
 "ageMin": {"value": 18 or null, "quote": "..."},
 "ageMax": {"value": 32 or null, "quote": "..."},
 "ageAsOn": {"value": "YYYY-MM-DD" or null, "quote": "..."},
 "ageRelaxation": {"en": "...", "hi": "...", "quote": "..."} or null,
 "salary": {"en": "...", "hi": "...", "quote": "..."} or null,
 "qualifications": {"value": ["8th","10th","12th","iti","diploma","graduate","postgraduate","engineering","medical","law","any"], "quote": "..."},
 "states": {"value": ["all"] or state codes like "UP","BR", "quote": "..."},
 "importantDates": [{"label": {"en": "...", "hi": "..."}, "date": "YYYY-MM-DD" or null, "text": "as written", "quote": "..."}],
 "fees": [{"category": {"en": "...", "hi": "..."}, "amount": 100 or null, "text": "as written", "quote": "..."}],
 "vacancies": [{"post": {"en": "...", "hi": "..."}, "total": 10 or null, "breakdown": "UR 4, OBC 3, ..." or null, "quote": "..."}],
 "eligibility": {"en": "...", "hi": "...", "quote": "..."} or null,
 "selection": [{"en": "...", "hi": "...", "quote": "..."}],
 "howToApply": {"en": "...", "hi": "...", "quote": "..."} or null,
 "links": [{"label": {"en": "...", "hi": "..."}, "url": "https://..."}]   // only URLs written in the notice or its link list
}"""

FIX_SYSTEM = EXTRACT_SYSTEM + """
You are correcting your previous answer. Some facts failed an automatic check against the notice.
For each failed fact: copy the quote exactly from the notice and make the value match it, or set the value to null (or remove the row) if the notice does not state it.
Return the full corrected JSON object in the same shape."""

VERIFY_SYSTEM = """You are a strict fact checker for an Indian government job alert app.
You get a notice and a numbered list of claims. For each claim decide, using ONLY the notice:
"yes" = the notice clearly states it, "no" = the notice says something different, "unclear" = not stated or ambiguous.
Pay close attention to dates, numbers, fees, ages and post counts. Reply as JSON: {"results": [{"id": 1, "verdict": "yes|no|unclear", "why": "short"}]}"""


_FREE = re.compile(r"exempt|no fee|nil|free of cost|not required to pay|शुल्क नहीं|छूट|निःशुल्क|शून्य")


@dataclass
class Failure:
    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


@dataclass
class Outcome:
    post: dict[str, Any] | None
    status: str                      # published | review | irrelevant
    reasons: list[str] = field(default_factory=list)
    rounds: int = 0
    facts_checked: int = 0
    facts_dropped: int = 0


def _text_ok(v: Any) -> bool:
    return isinstance(v, dict) and isinstance(v.get("en"), str) and v["en"].strip() != ""


def _bilingual_failures(path: str, v: Any) -> list[Failure]:
    if not _text_ok(v):
        return [Failure(path, "missing English text")]
    out = []
    hi = v.get("hi")
    if not isinstance(hi, str) or not has_devanagari(hi):
        out.append(Failure(path, "Hindi text missing or not in Devanagari"))
    elif not digits_match(v["en"], hi):
        out.append(Failure(path, "English and Hindi texts carry different numbers"))
    return out


def _quoted_text_failures(path: str, v: Any, source: str) -> list[Failure]:
    out = _bilingual_failures(path, v)
    if out and out[0].reason == "missing English text":
        return out
    q = v.get("quote")
    if not quote_in_source(q, source):
        out.append(Failure(path, "quote not found in notice"))
    else:
        quoted = set(numbers_in(q))
        extra = [n for n in numbers_in(v["en"]) if n not in quoted and not quote_in_source(n, source)]
        if extra:
            out.append(Failure(path, f"numbers {extra} are not in the notice"))
    return out


def check_facts(data: dict[str, Any], source: str, links: set[str], *,
                today: dt.date | None = None) -> list[Failure]:
    """Every rule a fact must pass. Empty list = all facts supported."""
    today = today or dt.date.today()
    fails: list[Failure] = []
    if data.get("type") not in TYPES:
        fails.append(Failure("type", "unknown type"))
    if data.get("category") not in CATEGORIES:
        fails.append(Failure("category", "unknown category"))
    fails += _bilingual_failures("title", data.get("title"))
    org = data.get("org")
    fails += _bilingual_failures("org", org)
    if _text_ok(org) and not quote_in_source(org.get("quote"), source):
        fails.append(Failure("org", "quote not found in notice"))
    if data.get("shortInfo") is not None:
        fails += _bilingual_failures("shortInfo", data.get("shortInfo"))
    if _text_ok(data.get("shortInfo")):
        extra = [n for n in numbers_in(data["shortInfo"]["en"]) if not quote_in_source(n, source)]
        if extra:
            fails.append(Failure("shortInfo", f"numbers {extra} are not in the notice"))

    def scalar(name: str, kind: str, lo: float | None = None, hi: float | None = None) -> None:
        f = data.get(name)
        if not isinstance(f, dict) or f.get("value") is None:
            return
        v, q = f["value"], f.get("quote")
        if not quote_in_source(q, source):
            fails.append(Failure(name, "quote not found in notice"))
            return
        if kind == "date":
            d = parse_iso_date(v)
            if d is None:
                fails.append(Failure(name, "not a YYYY-MM-DD date"))
            elif not date_supported(v, q):
                fails.append(Failure(name, f"date {v} is not written in the quote"))
            elif not (today - dt.timedelta(days=730) <= d <= today + dt.timedelta(days=800)):
                fails.append(Failure(name, "date out of range"))
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
                fails.append(Failure(name, "not a whole number"))
            elif not number_supported(int(v), q):
                fails.append(Failure(name, f"number {v} is not written in the quote"))
            elif (lo is not None and v < lo) or (hi is not None and v > hi):
                fails.append(Failure(name, "number out of range"))

    scalar("lastDate", "date")
    scalar("ageAsOn", "date")
    scalar("totalPosts", "int", 1, 500_000)
    scalar("ageMin", "int", 14, 70)
    scalar("ageMax", "int", 14, 70)
    amin, amax = (data.get("ageMin") or {}).get("value"), (data.get("ageMax") or {}).get("value")
    if isinstance(amin, int) and isinstance(amax, int) and amin > amax:
        fails.append(Failure("ageMax", "minimum age is above maximum age"))

    for name in ("ageRelaxation", "salary", "eligibility", "howToApply"):
        if data.get(name) is not None:
            fails += _quoted_text_failures(name, data[name], source)

    for name, allowed in (("qualifications", QUALIFICATIONS), ("states", STATES)):
        f = data.get(name)
        if not isinstance(f, dict) or not f.get("value"):
            continue
        vals = f["value"]
        if not isinstance(vals, list) or any(x not in allowed for x in vals):
            fails.append(Failure(name, "values outside the allowed list"))
        elif name == "qualifications" and not quote_in_source(f.get("quote"), source):
            fails.append(Failure(name, "quote not found in notice"))

    for i, row in enumerate(data.get("importantDates") or []):
        p = f"importantDates[{i}]"
        if not isinstance(row, dict):
            fails.append(Failure(p, "not an object"))
            continue
        fails += _bilingual_failures(p + ".label", row.get("label"))
        if not quote_in_source(row.get("quote"), source):
            fails.append(Failure(p, "quote not found in notice"))
        elif row.get("date") is not None and not date_supported(row.get("date"), row.get("quote")):
            fails.append(Failure(p, f"date {row.get('date')} is not written in the quote"))

    for i, row in enumerate(data.get("fees") or []):
        p = f"fees[{i}]"
        if not isinstance(row, dict):
            fails.append(Failure(p, "not an object"))
            continue
        fails += _bilingual_failures(p + ".category", row.get("category"))
        if not quote_in_source(row.get("quote"), source):
            fails.append(Failure(p, "quote not found in notice"))
        elif row.get("amount") is not None:
            a = row["amount"]
            if isinstance(a, bool) or not isinstance(a, int) or a < 0 or a > 100_000:
                fails.append(Failure(p, "amount is not a sensible whole number"))
            elif a == 0 and _FREE.search(normalize(row.get("quote"))):
                pass
            elif not number_supported(a, row.get("quote")):
                fails.append(Failure(p, f"amount {a} is not written in the quote"))

    for i, row in enumerate(data.get("vacancies") or []):
        p = f"vacancies[{i}]"
        if not isinstance(row, dict):
            fails.append(Failure(p, "not an object"))
            continue
        fails += _bilingual_failures(p + ".post", row.get("post"))
        if not quote_in_source(row.get("quote"), source):
            fails.append(Failure(p, "quote not found in notice"))
            continue
        t = row.get("total")
        if t is not None:
            if isinstance(t, bool) or not isinstance(t, int) or t < 1 or t > 500_000:
                fails.append(Failure(p, "total is not a sensible whole number"))
            elif not number_supported(t, row.get("quote")):
                fails.append(Failure(p, f"total {t} is not written in the quote"))
        b = row.get("breakdown")
        if b:
            missing = [n for n in numbers_in(b) if n not in numbers_in(row.get("quote")) and not quote_in_source(n, source)]
            if missing:
                fails.append(Failure(p, f"breakdown numbers {missing} are not in the notice"))

    for i, row in enumerate(data.get("selection") or []):
        fails += _quoted_text_failures(f"selection[{i}]", row, source)

    for i, row in enumerate(data.get("links") or []):
        p = f"links[{i}]"
        url = (row or {}).get("url") if isinstance(row, dict) else None
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https"):
            fails.append(Failure(p, "not a web link"))
        elif url not in links and normalize(url) not in normalize(source):
            fails.append(Failure(p, "link is not in the notice"))
        else:
            fails += _bilingual_failures(p + ".label", row.get("label"))
    return fails


def drop_failed(data: dict[str, Any], fails: list[Failure]) -> tuple[dict[str, Any], int]:
    """Copy of `data` with every failed fact removed. Returns (data, facts dropped)."""
    data = copy.deepcopy(data)
    dropped = 0
    rows: dict[str, set[int]] = {}
    for f in fails:
        head = f.path.split(".")[0]
        if "[" in head:
            name, idx = head[:-1].split("[")
            rows.setdefault(name, set()).add(int(idx))
        elif head in ("lastDate", "totalPosts", "ageMin", "ageMax", "ageAsOn"):
            if isinstance(data.get(head), dict) and data[head].get("value") is not None:
                data[head]["value"] = None
                dropped += 1
        elif head in ("ageRelaxation", "salary", "eligibility", "howToApply", "shortInfo"):
            if data.get(head) is not None:
                data[head] = None
                dropped += 1
        elif head in ("qualifications", "states"):
            if isinstance(data.get(head), dict) and data[head].get("value"):
                data[head]["value"] = []
                dropped += 1
    for name, idxs in rows.items():
        lst = data.get(name) or []
        data[name] = [r for i, r in enumerate(lst) if i not in idxs]
        dropped += len([i for i in idxs if i < len(lst)])
    return data, dropped


def _claims(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(path, plain-English claim) for every fact the verifier must confirm."""
    out: list[tuple[str, str]] = []
    org = (data.get("org") or {}).get("en", "")

    def add(path: str, text: str) -> None:
        out.append((path, text))

    for name, label in (("lastDate", "The last date to apply is"), ("ageAsOn", "Age is counted as on"),
                        ("totalPosts", "The total number of posts/vacancies is"),
                        ("ageMin", "The minimum age limit is"), ("ageMax", "The maximum age limit is")):
        v = (data.get(name) or {}).get("value")
        if v is not None:
            add(name, f"{label} {v}.")
    for name, label in (("salary", "Pay/salary"), ("ageRelaxation", "Age relaxation"),
                        ("eligibility", "Eligibility"), ("howToApply", "How to apply")):
        v = data.get(name)
        if _text_ok(v):
            add(name, f"{label}: {v['en']}")
    quals = (data.get("qualifications") or {}).get("value") or []
    if quals:
        add("qualifications", f"The minimum qualification accepted includes: {', '.join(quals)}.")
    for i, r in enumerate(data.get("importantDates") or []):
        if _text_ok(r.get("label")):
            add(f"importantDates[{i}]", f"{r['label']['en']}: {r.get('date') or r.get('text')}.")
    for i, r in enumerate(data.get("fees") or []):
        if _text_ok(r.get("category")):
            add(f"fees[{i}]", f"Application fee for {r['category']['en']}: {r.get('text') or r.get('amount')}.")
    for i, r in enumerate(data.get("vacancies") or []):
        if _text_ok(r.get("post")):
            bits = [f"{r['post']['en']}"]
            if r.get("total") is not None:
                bits.append(f"total {r['total']}")
            if r.get("breakdown"):
                bits.append(f"category-wise {r['breakdown']}")
            add(f"vacancies[{i}]", "Vacancy: " + ", ".join(bits) + ".")
    for i, r in enumerate(data.get("selection") or []):
        if _text_ok(r):
            add(f"selection[{i}]", f"Selection stage: {r['en']}")
    if _text_ok(data.get("shortInfo")):
        add("shortInfo", f"Summary: {data['shortInfo']['en']}")
    if org:
        add("org", f"The notice is issued by {org}.")
    return out


def verify(llm: llm_mod.Llm, data: dict[str, Any], source: str) -> list[Failure]:
    claims = _claims(data)
    if not claims:
        return []
    listing = "\n".join(f"{i + 1}. {c}" for i, (_, c) in enumerate(claims))
    reply = llm.json(VERIFY_SYSTEM, f"NOTICE:\n{source[:MAX_SOURCE_CHARS]}\n\nCLAIMS:\n{listing}",
                     model=llm_mod.VERIFY_MODEL, max_tokens=3000)
    verdicts: dict[int, str] = {}
    for r in reply.get("results") or []:
        try:
            verdicts[int(r.get("id"))] = str(r.get("verdict", "")).lower()
        except (TypeError, ValueError):
            continue
    fails = []
    for i, (path, _) in enumerate(claims):
        v = verdicts.get(i + 1, "unclear")
        if v != "yes":
            fails.append(Failure(path, f"independent check said {v}"))
    return fails


def _user_prompt(source: Source, notice_url: str, link_text: str, text: str,
                 links: set[str]) -> str:
    link_list = "\n".join(sorted(links)[:80])
    return (f"Publisher: {source.name}\nNotice title on the website: {link_text}\nNotice URL: {notice_url}\n"
            f"Links found on the notice page:\n{link_list or '(none)'}\n\nNOTICE TEXT:\n{text[:MAX_SOURCE_CHARS]}\n\n{EXTRACT_SHAPE}")


def process_notice(llm: llm_mod.Llm, source: Source, *, notice_url: str, link_text: str,
                   text: str, links: set[str], now: dt.datetime | None = None) -> Outcome:
    now = now or dt.datetime.now(dt.timezone.utc)
    if len(normalize(text)) < 80:
        return Outcome(None, "review", ["notice text could not be read"])
    links = set(links) | {notice_url}
    prompt = _user_prompt(source, notice_url, link_text, text, links)
    data = llm.json(EXTRACT_SYSTEM, prompt)
    if data.get("relevant") is False:
        return Outcome(None, "irrelevant", ["not a candidate notice"])
    rounds = 1
    fails = check_facts(data, text, links, today=now.date())
    while fails and rounds < MAX_ROUNDS:
        fix = (f"{prompt}\n\nYOUR PREVIOUS JSON:\n{json.dumps(data, ensure_ascii=False)}\n\n"
               "FAILED CHECKS:\n" + "\n".join(f"- {f}" for f in fails))
        data = llm.json(FIX_SYSTEM, fix)
        rounds += 1
        fails = check_facts(data, text, links, today=now.date())
    total_before = len(_claims(data))
    data, dropped = drop_failed(data, fails)
    reasons = [str(f) for f in fails]
    vfails = verify(llm, data, text)
    data, vdropped = drop_failed(data, vfails)
    reasons += [str(f) for f in vfails]
    dropped += vdropped
    for core in ("title", "org"):
        if any(f.path == core for f in fails + vfails) or not _text_ok(data.get(core)):
            return Outcome(None, "review", reasons + [f"{core} could not be confirmed"], rounds,
                           total_before, dropped)
    if data.get("type") not in TYPES or data.get("category") not in CATEGORIES:
        return Outcome(None, "review", reasons + ["type or category unknown"], rounds, total_before, dropped)
    if total_before and dropped / max(1, total_before) > 0.4:
        return Outcome(None, "review", reasons + ["too many facts failed the checks"], rounds,
                       total_before, dropped)
    if data["type"] == "job" and (data.get("lastDate") or {}).get("value") is None \
            and not data.get("importantDates"):
        return Outcome(None, "review", reasons + ["job without any confirmed date"], rounds,
                       total_before, dropped)
    post = build_post(data, source, notice_url=notice_url, now=now, rounds=rounds,
                      checked=total_before, dropped=dropped)
    return Outcome(post, "published", reasons, rounds, total_before, dropped)


def _strip(v: Any) -> dict[str, str] | None:
    if not _text_ok(v):
        return None
    return {"en": v["en"].strip(), "hi": (v.get("hi") or v["en"]).strip()}


def post_id(source: Source, title_en: str, notice_url: str) -> str:
    from .sources import link_id
    return f"{source.id}-{slugify(title_en, max_len=48)}-{link_id(notice_url)[:6]}"


def build_post(data: dict[str, Any], source: Source, *, notice_url: str, now: dt.datetime,
               rounds: int, checked: int, dropped: int) -> dict[str, Any]:
    ts = now.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    title = _strip(data["title"])
    states = [s for s in ((data.get("states") or {}).get("value") or []) if s in STATES] or list(source.states)
    quals = [q for q in ((data.get("qualifications") or {}).get("value") or []) if q in QUALIFICATIONS]
    links = [{"label": _strip(r["label"]), "url": r["url"]} for r in data.get("links") or []
             if isinstance(r, dict) and _strip(r.get("label")) and isinstance(r.get("url"), str)]
    is_pdf = notice_url.lower().split("?")[0].endswith(".pdf")
    total = (data.get("totalPosts") or {}).get("value")
    if total is None:
        totals = [r.get("total") for r in data.get("vacancies") or []]
        if totals and all(isinstance(t, int) for t in totals):
            total = sum(totals)
    return {
        "id": post_id(source, title["en"], notice_url),
        "type": data["type"],
        "title": title,
        "org": _strip(data["org"]),
        "source": source.id,
        "category": data["category"] if data["category"] != "other" else source.category,
        "postedAt": ts,
        "updatedAt": ts,
        "lastDate": (data.get("lastDate") or {}).get("value"),
        "totalPosts": total,
        "qualifications": quals,
        "states": states,
        "ageMin": (data.get("ageMin") or {}).get("value"),
        "ageMax": (data.get("ageMax") or {}).get("value"),
        "salary": _strip(data.get("salary")),
        "tags": [],
        "verified": True,
        "shortInfo": _strip(data.get("shortInfo")),
        "importantDates": [{"label": _strip(r["label"]), "date": r.get("date"), "text": r.get("text") or r.get("date")}
                           for r in data.get("importantDates") or [] if _strip(r.get("label"))],
        "fees": [{"category": _strip(r["category"]), "amount": r.get("amount"), "text": r.get("text")}
                 for r in data.get("fees") or [] if _strip(r.get("category"))],
        "age": {"min": (data.get("ageMin") or {}).get("value"), "max": (data.get("ageMax") or {}).get("value"),
                "asOn": (data.get("ageAsOn") or {}).get("value"), "relaxation": _strip(data.get("ageRelaxation"))},
        "vacancies": [{"post": _strip(r["post"]), "total": r.get("total"), "breakdown": r.get("breakdown")}
                      for r in data.get("vacancies") or [] if _strip(r.get("post"))],
        "eligibility": _strip(data.get("eligibility")),
        "selection": [s for s in (_strip(r) for r in data.get("selection") or []) if s],
        "howToApply": _strip(data.get("howToApply")),
        "links": links,
        "officialNoticeUrl": notice_url if is_pdf else None,
        "sourcePageUrl": notice_url if not is_pdf else source.url,
        "check": {"model": llm_mod.EXTRACT_MODEL, "verifier": llm_mod.VERIFY_MODEL, "rounds": rounds,
                  "factsChecked": checked, "factsDropped": dropped},
    }


SUMMARY_KEYS = ("id", "type", "title", "org", "source", "category", "postedAt", "updatedAt", "lastDate",
                "totalPosts", "qualifications", "states", "ageMin", "ageMax", "salary", "tags", "verified")


def summary(post: dict[str, Any]) -> dict[str, Any]:
    return {k: post.get(k) for k in SUMMARY_KEYS}
