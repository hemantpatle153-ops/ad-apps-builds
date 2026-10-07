"""Roz Quiz content: current affairs notes, verified questions, daily quiz.

Current affairs come from PIB press releases. Every note and question must
quote the release; numbers must match; and a second, stronger model must
answer each question on its own and reach the same option before it is used.
Static GK questions for the subject banks have no single source text, so they
are only kept when the verifier answers them correctly twice, with the
options in a different order each time.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any

from . import llm as llm_mod
from .textnorm import compact, digits_match, has_devanagari, normalize, numbers_in, quote_in_source

SUBJECTS = ("gk", "history", "polity", "geography", "economy", "science", "current_affairs",
            "maths", "reasoning", "english", "computer")
BANK_SUBJECTS = ("gk", "history", "polity", "geography", "economy", "science", "computer")
EXAMS = ("ssc", "railway", "banking", "upsc", "state_psc", "defence", "teaching")
DIFFICULTY = ("easy", "medium", "hard")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

CA_PICK_SYSTEM = """You select press releases that matter for Indian competitive exams (SSC, Railways, Banking, UPSC, State PSC).
Prefer: government schemes, appointments, awards, summits, reports and indices, economy and RBI, defence exercises, science and space, sports wins, agreements with other countries, important days.
Skip: routine meetings, speeches without facts, minor regional events. Reply as JSON {"pick": [ids]}."""

CA_SYSTEM = """You write current affairs for an exam preparation app, using ONLY the press release given.
Never use outside knowledge. Every note and question needs "quote": an exact copy (5-40 words) of the release text that supports it.
Texts have "en" (English) and "hi" (natural Hindi, Devanagari) carrying the same numbers.
Questions are exam style multiple choice with exactly 4 options, one correct, plausible wrong options, and a 1-2 sentence explanation.
Reply with one JSON object only:
{"note": {"title": {"en","hi"}, "body": {"en": "<= 60 words", "hi"}, "tags": ["economy|polity|science|defence|sports|awards|international|environment|schemes|appointments|days"], "quote": "..."},
 "questions": [{"q": {"en","hi"}, "options": [{"en","hi"} x4], "answer": 0-3, "explanation": {"en","hi"}, "difficulty": "easy|medium|hard", "exams": ["ssc","railway","banking","upsc","state_psc","defence","teaching"], "quote": "..."}]}"""

SOLVE_SYSTEM = """Answer multiple choice questions. If a passage is given, use only the passage.
Reply as JSON {"answers": [{"id": 1, "answer": 0-3 or null, "sure": true/false}]}. Use null if no option is clearly correct."""

BANK_SYSTEM = """You write multiple choice questions for Indian government exam preparation (SSC, Railways, Banking, UPSC, State PSC).
Only static, settled facts that have one undisputed correct answer and will not change (no "current", "latest", "present" holder questions, no statistics that change).
Exactly 4 options, one correct, plausible wrong options, 1-2 sentence explanation. Texts have "en" and "hi" (Devanagari) with the same numbers.
Reply with one JSON object only: {"questions": [{"q": {"en","hi"}, "options": [{"en","hi"} x4], "answer": 0-3, "explanation": {"en","hi"}, "difficulty": "easy|medium|hard", "exams": [...]}]}"""

_TIMEBOUND = re.compile(r"\b(current|currently|present|presently|latest|recent|recently|newly|this year|now|incumbent|sitting)\b")


@dataclass
class Report:
    kept: int = 0
    rejected: int = 0
    reasons: list[str] = field(default_factory=list)

    def reject(self, why: str) -> None:
        self.rejected += 1
        if len(self.reasons) < 200:
            self.reasons.append(why)


def _bi(v: Any) -> dict[str, str] | None:
    if not isinstance(v, dict):
        return None
    en, hi = v.get("en"), v.get("hi")
    if not isinstance(en, str) or not en.strip() or not isinstance(hi, str) or not hi.strip():
        return None
    # Pure numbers or symbols ("1947", "25%") read the same in both languages.
    if not has_devanagari(hi) and re.search(r"[A-Za-z]", en):
        return None
    if not digits_match(en, hi):
        return None
    return {"en": en.strip(), "hi": hi.strip()}


def question_id(prefix: str, text_en: str) -> str:
    return f"q-{prefix}-{hashlib.sha1(compact(text_en).encode()).hexdigest()[:10]}"


def clean_question(raw: Any, *, subject: str, default_exams: tuple[str, ...] = EXAMS) -> tuple[dict[str, Any] | None, str]:
    """Validated question in the published shape, or (None, reason)."""
    if not isinstance(raw, dict):
        return None, "not an object"
    q = _bi(raw.get("q"))
    if not q:
        return None, "question text missing, not bilingual, or numbers differ"
    opts_raw = raw.get("options")
    if not isinstance(opts_raw, list) or len(opts_raw) != 4:
        return None, "needs exactly 4 options"
    opts = [_bi(o) for o in opts_raw]
    if any(o is None for o in opts):
        return None, "an option is missing a language or numbers differ"
    if len({compact(o["en"]) for o in opts}) != 4 or len({compact(o["hi"]) for o in opts}) != 4:
        return None, "options are not distinct"
    ans = raw.get("answer")
    if isinstance(ans, bool) or not isinstance(ans, int) or not 0 <= ans <= 3:
        return None, "answer index out of range"
    expl = _bi(raw.get("explanation"))
    if not expl:
        return None, "explanation missing or numbers differ"
    if subject != "current_affairs" and _TIMEBOUND.search(normalize(q["en"])):
        return None, "time-bound question in a static bank"
    if any(normalize(o["en"]) in ("all of the above", "none of the above", "both a and b") for o in opts):
        return None, "uses all/none of the above"
    diff = raw.get("difficulty") if raw.get("difficulty") in DIFFICULTY else "medium"
    exams = [e for e in raw.get("exams") or [] if e in EXAMS] or list(default_exams)
    return {"id": question_id(subject, q["en"]), "q": q, "options": opts, "answer": ans,
            "explanation": expl, "subject": subject, "exams": exams, "difficulty": diff,
            "source": None, "asked": None, "verified": True}, ""


def solve(llm: llm_mod.Llm, questions: list[dict[str, Any]], *, passage: str | None = None,
          order: list[list[int]] | None = None) -> list[int | None]:
    """Verifier's own answers (indexes into each question's original options)."""
    order = order or [[0, 1, 2, 3] for _ in questions]
    lines = []
    for i, (qq, perm) in enumerate(zip(questions, order)):
        opts = "\n".join(f"  {k}) {qq['options'][j]['en']}" for k, j in enumerate(perm))
        lines.append(f"{i + 1}. {qq['q']['en']}\n{opts}")
    user = ("" if passage is None else f"PASSAGE:\n{passage[:20000]}\n\n") + "QUESTIONS:\n" + "\n".join(lines)
    reply = llm.json(SOLVE_SYSTEM, user, model=llm_mod.VERIFY_MODEL, max_tokens=1500)
    got: dict[int, int | None] = {}
    for r in reply.get("answers") or []:
        try:
            idx = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        a = r.get("answer")
        if isinstance(a, int) and not isinstance(a, bool) and 0 <= a <= 3 and r.get("sure", True) is not False:
            got[idx] = a
        else:
            got[idx] = None
    out: list[int | None] = []
    for i, perm in enumerate(order):
        a = got.get(i + 1)
        out.append(None if a is None else perm[a])
    return out


def _supported_by(text_en: str, quote: str, source: str) -> bool:
    return all(n in numbers_in(quote) or quote_in_source(n, source) for n in numbers_in(text_en))


def current_affairs_item(llm: llm_mod.Llm, *, title: str, url: str, text: str, date: str,
                         index: int, report: Report) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """One note and its checked questions from one press release."""
    reply = llm.json(CA_SYSTEM, f"PRESS RELEASE ({title}):\n{text[:20000]}\n\nWrite one note and 2 questions.")
    note_raw = reply.get("note") or {}
    note = None
    t, b = _bi(note_raw.get("title")), _bi(note_raw.get("body"))
    nq = note_raw.get("quote")
    if t and b and quote_in_source(nq, text) and _supported_by(b["en"], nq or "", text) \
            and len(b["en"].split()) <= 75:
        tags = [x for x in note_raw.get("tags") or [] if isinstance(x, str)][:3]
        note = {"id": f"ca-{date}-{index}", "title": t, "body": b, "source": {"name": "PIB", "url": url}, "tags": tags}
    else:
        report.reject(f"note from {url}: failed checks")
    candidates = []
    for raw in reply.get("questions") or []:
        q, why = clean_question(raw, subject="current_affairs")
        quote = (raw or {}).get("quote") if isinstance(raw, dict) else None
        if not q:
            report.reject(f"CA question: {why}")
            continue
        if not quote_in_source(quote, text):
            report.reject("CA question: quote not in release")
            continue
        right = q["options"][q["answer"]]["en"]
        if not (_supported_by(right, quote, text) and (quote_in_source(right, text) or numbers_in(right)
                                                       or compact(right)[:12] in compact(text))):
            report.reject("CA question: correct option not found in release")
            continue
        q["source"] = {"name": "PIB", "url": url}
        candidates.append(q)
    kept = []
    if candidates:
        answers = solve(llm, candidates, passage=text)
        for q, a in zip(candidates, answers):
            if a == q["answer"]:
                kept.append(q)
                report.kept += 1
            else:
                report.reject("CA question: verifier picked a different answer")
    return note, kept


def bank_batch(llm: llm_mod.Llm, subject: str, *, existing: set[str], count: int,
               report: Report, rng: random.Random) -> list[dict[str, Any]]:
    """New static questions for one subject, each answered correctly twice by the verifier."""
    avoid = "\n".join(sorted(existing)[:60])
    reply = llm.json(BANK_SYSTEM, f"Subject: {subject}\nWrite {count} new questions, mixed difficulty.\n"
                                  f"Do not repeat these (normalised) questions:\n{avoid}")
    candidates = []
    for raw in reply.get("questions") or []:
        q, why = clean_question(raw, subject=subject)
        if not q:
            report.reject(f"{subject}: {why}")
            continue
        key = compact(q["q"]["en"])
        if key in existing:
            report.reject(f"{subject}: duplicate")
            continue
        existing.add(key)
        candidates.append(q)
    if not candidates:
        return []
    first = solve(llm, candidates)
    perms = []
    for _ in candidates:
        p = [0, 1, 2, 3]
        while p == [0, 1, 2, 3]:
            rng.shuffle(p)
        perms.append(p)
    second = solve(llm, candidates, order=perms)
    kept = []
    for q, a, b in zip(candidates, first, second):
        if a == q["answer"] and b == q["answer"]:
            kept.append(q)
            report.kept += 1
        else:
            report.reject(f"{subject}: verifier disagreed")
    return kept


def daily_quiz(date: str, *, ca_questions: list[dict[str, Any]], banks: dict[str, list[dict[str, Any]]],
               recent_ids: set[str], size: int = 10) -> dict[str, Any] | None:
    """The day's 10 questions: up to 4 current affairs, the rest spread across subjects.

    Deterministic for a date, and never repeats a question used in recent daily quizzes.
    """
    rng = random.Random(f"roz-quiz-{date}")
    picked: list[dict[str, Any]] = []
    used = set(recent_ids)
    ca = [q for q in ca_questions if q["id"] not in used]
    rng.shuffle(ca)
    for q in ca[:4]:
        picked.append(q)
        used.add(q["id"])
    subjects = [s for s in BANK_SUBJECTS if banks.get(s)]
    rng.shuffle(subjects)
    pools = {s: [q for q in banks[s] if q["id"] not in used] for s in subjects}
    for s in subjects:
        rng.shuffle(pools[s])
    while len(picked) < size and any(pools.values()):
        for s in subjects:
            if len(picked) >= size:
                break
            if pools[s]:
                q = pools[s].pop()
                if q["id"] not in used:
                    picked.append(q)
                    used.add(q["id"])
    if len(picked) < size:
        return None
    rng.shuffle(picked)
    d = dt.date.fromisoformat(date)
    title = {"en": f"Daily Quiz · {d.day} {d.strftime('%b %Y')}",
             "hi": f"दैनिक क्विज़ · {d.day} {['जनवरी', 'फरवरी', 'मार्च', 'अप्रैल', 'मई', 'जून', 'जुलाई', 'अगस्त', 'सितंबर', 'अक्टूबर', 'नवंबर', 'दिसंबर'][d.month - 1]} {d.year}"}
    return {"schema": 1, "date": date, "title": title, "questions": picked}


def today_ist(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(IST).date().isoformat()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
