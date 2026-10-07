"""Run the pipeline: python -m feedbot run --data <feed-data checkout> [--jobs] [--quiz]."""

from __future__ import annotations

import argparse
import datetime as dt
import random
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import jobs as jobs_mod
from . import quiz as quiz_mod
from .fetch import Fetcher, document_text, html_links
from .llm import DeepSeek, Llm, LlmError
from .sources import Source, is_candidate, link_id, load_sources
from .store import Store
from .validate import post_errors, question_errors

BOOTSTRAP_KEEP = 6          # first run of a source: process only its newest few notices
MAX_NEW_PER_SOURCE = 5
MAX_NEW_PER_RUN = 25
CA_PER_DAY = 8
BANK_PER_SUBJECT_PER_DAY = 10
SUBJECT_TITLES = {
    "gk": {"en": "General Knowledge", "hi": "सामान्य ज्ञान"},
    "history": {"en": "History", "hi": "इतिहास"},
    "polity": {"en": "Polity", "hi": "राजव्यवस्था"},
    "geography": {"en": "Geography", "hi": "भूगोल"},
    "economy": {"en": "Economy", "hi": "अर्थव्यवस्था"},
    "science": {"en": "Science", "hi": "विज्ञान"},
    "computer": {"en": "Computer", "hi": "कंप्यूटर"},
}


def _ts(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _duplicate(store: Store, post: dict[str, Any]) -> bool:
    """The same notice published by two sites (e.g. SSC and Employment News)."""
    from .textnorm import compact
    key = (compact(post["title"]["en"]), post.get("lastDate"), post.get("type"))
    for p in (store.read("jobs/index.json") or {}).get("posts", []):
        if p.get("id") != post["id"] and (compact((p.get("title") or {}).get("en")), p.get("lastDate"), p.get("type")) == key:
            return True
    return False


def run_jobs(store: Store, llm: Llm, fetcher: Fetcher, sources: list[Source], now: dt.datetime,
             status: dict[str, Any], *, max_new: int = MAX_NEW_PER_RUN) -> None:
    seen: dict[str, Any] = store.read("state/seen.json") or {}
    budget = max_new
    for src in sources:
        st: dict[str, Any] = {"ok": False, "links": 0, "candidates": 0, "new": 0, "published": 0, "review": 0}
        status["sources"][src.id] = st
        try:
            page = fetcher.get(src.url)
            st["http"] = page.status
            if page.status >= 400:
                st["error"] = f"HTTP {page.status}"
                continue
            links = html_links(page.text(), page.url)
            st["links"] = len(links)
            cands = [(t, u) for t, u in links if is_candidate(t, u, src)]
            st["candidates"] = len(cands)
            st["ok"] = True
        except Exception as e:  # one broken site must not stop the others
            st["error"] = str(e)[:200]
            continue
        first_time = not any(v.get("source") == src.id for v in seen.values())
        fresh = [(t, u) for t, u in cands if link_id(u) not in seen]
        if first_time:
            for t, u in fresh[BOOTSTRAP_KEEP:]:
                seen[link_id(u)] = {"source": src.id, "url": u, "status": "skipped", "first": _ts(now)}
            fresh = fresh[:BOOTSTRAP_KEEP]
        st["new"] = len(fresh)
        for text, url in fresh[:MAX_NEW_PER_SOURCE]:
            if budget <= 0:
                break
            key = link_id(url)
            entry = {"source": src.id, "url": url, "title": text[:200], "first": _ts(now)}
            try:
                doc = fetcher.get(url)
                if doc.status >= 400:
                    raise RuntimeError(f"HTTP {doc.status}")
                body, doc_links = document_text(doc)
                budget -= 1
                out = jobs_mod.process_notice(llm, src, notice_url=doc.url, link_text=text, text=body,
                                              links={u for _, u in doc_links}, now=now)
            except LlmError:
                raise
            except Exception as e:
                entry.update(status="error", error=str(e)[:200])
                seen[key] = entry
                continue
            entry["status"] = out.status
            seen[key] = entry
            if out.status == "published" and out.post and post_errors(out.post):
                out.status, entry["status"] = "review", "review"
                out.reasons = out.reasons + ["shape: " + e for e in post_errors(out.post)]
            if out.status == "published" and out.post and _duplicate(store, out.post):
                out.status, entry["status"] = "duplicate", "duplicate"
            if out.status == "published" and out.post:
                store.add_post(out.post, jobs_mod.summary(out.post), now=_ts(now))
                st["published"] += 1
            elif out.status == "review":
                st["review"] += 1
                store.add_review({"key": key, "source": src.id, "url": url, "title": text[:200],
                                  "reasons": out.reasons[:20], "at": _ts(now)})
        store.write("state/seen.json", seen)
    store.write("state/seen.json", seen)


def _rss_items(xml_text: str) -> list[tuple[str, str]]:
    out = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            out.append((title, link))
    return out


def _prid(url: str) -> str | None:
    m = re.search(r"PRID=(\d+)", url, re.I)
    return m.group(1) if m else None


def run_quiz(store: Store, llm: Llm, fetcher: Fetcher, ca_sources: list[Source], now: dt.datetime,
             status: dict[str, Any]) -> None:
    today = quiz_mod.today_ist(now)
    qs: dict[str, Any] = {"today": today}
    status["quiz"] = qs
    # 1. remember every PIB release with the IST day it first appeared
    pib: dict[str, Any] = store.read("state/pib.json") or {}
    for src in ca_sources:
        try:
            page = fetcher.get(src.url)
            items = _rss_items(page.text())
            qs[f"rss:{src.id}"] = len(items)
            for title, link in items:
                pid = _prid(link) or link_id(link)
                pib.setdefault(pid, {"day": today, "title": title, "url": link})
        except Exception as e:
            qs[f"rss:{src.id}"] = f"error: {str(e)[:120]}"
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=10)).isoformat()
    pib = {k: v for k, v in pib.items() if v.get("day", "") >= cutoff}
    store.write("state/pib.json", pib)

    idx = store.quiz_index()
    # 2. yesterday's current affairs, written once
    yday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    if not store.exists(f"quiz/current_affairs/{yday}.json"):
        day_items = [(k, v) for k, v in pib.items() if v.get("day") == yday]
        if day_items:
            report = quiz_mod.Report()
            listing = "\n".join(f"{k}: {v['title']}" for k, v in day_items[:120])
            pick = llm.json(quiz_mod.CA_PICK_SYSTEM, f"Pick up to {CA_PER_DAY} ids:\n{listing}").get("pick") or []
            chosen = [(k, v) for k, v in day_items if k in {str(p) for p in pick}][:CA_PER_DAY]
            notes, questions = [], []
            for i, (k, v) in enumerate(chosen):
                try:
                    body, _ = document_text(fetcher.get(v["url"]))
                except Exception as e:
                    report.reject(f"fetch {v['url']}: {str(e)[:80]}")
                    continue
                if len(body) < 200:
                    report.reject(f"release {k} too short")
                    continue
                note, kept = quiz_mod.current_affairs_item(llm, title=v["title"], url=v["url"], text=body,
                                                           date=yday, index=i + 1, report=report)
                if note:
                    notes.append(note)
                questions += [q for q in kept if not question_errors(q)]
            if notes:
                store.write(f"quiz/current_affairs/{yday}.json",
                            {"schema": 1, "date": yday, "notes": notes, "questions": questions})
                idx.setdefault("currentAffairs", []).append(yday)
            qs["ca"] = {"notes": len(notes), "questions": len(questions), "rejected": report.rejected,
                        "reasons": report.reasons[:30]}
    store.save_quiz_index(idx, now=_ts(now))
    # 3. grow the subject banks a little every day
    if idx.get("bankDay") != today:
        rng = random.Random(today)
        report = quiz_mod.Report()
        for subject in quiz_mod.BANK_SUBJECTS:
            bank = store.bank(subject)
            existing = {quiz_mod.compact(q["q"]["en"]) for q in bank}
            try:
                new = quiz_mod.bank_batch(llm, subject, existing=existing, count=BANK_PER_SUBJECT_PER_DAY,
                                          report=report, rng=rng)
            except LlmError as e:
                report.reject(f"{subject}: {e}")
                continue
            new = [q for q in new if not question_errors(q)]
            if new:
                store.save_bank(subject, bank + new, SUBJECT_TITLES[subject])
        idx = store.quiz_index()
        idx["bankDay"] = today
        qs["banks"] = {"kept": report.kept, "rejected": report.rejected, "reasons": report.reasons[:30]}
    # 4. today's daily quiz
    if not store.exists(f"quiz/daily/{today}.json"):
        recent: set[str] = set()
        for day in idx.get("daily", [])[:60]:
            for q in (store.read(f"quiz/daily/{day}.json") or {}).get("questions", []):
                recent.add(q.get("id"))
        ca_q: list[dict[str, Any]] = []
        for day in sorted(idx.get("currentAffairs", []), reverse=True)[:3]:
            ca_q += (store.read(f"quiz/current_affairs/{day}.json") or {}).get("questions", [])
        banks = {s: store.bank(s) for s in quiz_mod.BANK_SUBJECTS}
        daily = quiz_mod.daily_quiz(today, ca_questions=ca_q, banks=banks, recent_ids=recent)
        if daily:
            store.write(f"quiz/daily/{today}.json", daily)
            idx.setdefault("daily", []).append(today)
            qs["daily"] = "written"
        else:
            qs["daily"] = "not enough verified questions yet"
    store.save_quiz_index(idx, now=_ts(now))
    idx = store.quiz_index()
    store.prune_days("quiz/daily", idx.get("daily", []))
    store.prune_days("quiz/current_affairs", idx.get("currentAffairs", []))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="feedbot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--data", required=True, type=Path)
    r.add_argument("--jobs", action="store_true")
    r.add_argument("--quiz", action="store_true")
    r.add_argument("--max-new", type=int, default=MAX_NEW_PER_RUN)
    args = ap.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    store = Store(args.data)
    status: dict[str, Any] = {"schema": 1, "run": _ts(now), "sources": {}}
    try:
        llm = DeepSeek()
    except LlmError as e:
        status["error"] = str(e)
        store.write("status.json", status)
        print(f"skipped: {e}", file=sys.stderr)
        return 0
    jobs_src, ca_src = load_sources()
    fetcher = Fetcher()
    do_jobs = args.jobs or not args.quiz
    do_quiz = args.quiz or not args.jobs
    code = 0
    try:
        if do_jobs:
            run_jobs(store, llm, fetcher, jobs_src, now, status, max_new=args.max_new)
        if do_quiz:
            run_quiz(store, llm, fetcher, ca_src, now, status)
    except Exception as e:
        status["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        traceback.print_exc()
        code = 1
    status["llm"] = {"calls": llm.usage.calls, "promptTokens": llm.usage.prompt_tokens,
                     "completionTokens": llm.usage.completion_tokens, "byModel": llm.usage.by_model}
    store.write("status.json", status)
    return code


if __name__ == "__main__":
    sys.exit(main())
