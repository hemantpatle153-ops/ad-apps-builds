"""Shape checks for published files (the apps rely on these; see SCHEMA.md)."""

from __future__ import annotations

import re
from typing import Any

from .jobs import QUALIFICATIONS, STATES, TYPES
from .quiz import DIFFICULTY, EXAMS, SUBJECTS
from .sources import CATEGORIES
from .textnorm import parse_iso_date

_ID = re.compile(r"^[a-z0-9-]+$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _text(v: Any, path: str, errs: list[str], *, nullable: bool = False) -> None:
    if v is None and nullable:
        return
    if not (isinstance(v, dict) and isinstance(v.get("en"), str) and v["en"]
            and isinstance(v.get("hi"), str) and v["hi"]):
        errs.append(f"{path}: needs en and hi text")


def _date(v: Any, path: str, errs: list[str]) -> None:
    if v is not None and parse_iso_date(v) is None:
        errs.append(f"{path}: not a YYYY-MM-DD date")


def _int(v: Any, path: str, errs: list[str]) -> None:
    if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
        errs.append(f"{path}: not a whole number")


def post_errors(p: Any, *, full: bool = True) -> list[str]:
    errs: list[str] = []
    if not isinstance(p, dict):
        return ["post is not an object"]
    if not isinstance(p.get("id"), str) or not _ID.match(p["id"]):
        errs.append("id: must be [a-z0-9-]")
    if p.get("type") not in TYPES:
        errs.append("type: unknown")
    if p.get("category") not in CATEGORIES:
        errs.append("category: unknown")
    _text(p.get("title"), "title", errs)
    _text(p.get("org"), "org", errs)
    _text(p.get("salary"), "salary", errs, nullable=True)
    for k in ("postedAt", "updatedAt"):
        if not isinstance(p.get(k), str) or not _TS.match(p[k]):
            errs.append(f"{k}: not an ISO UTC timestamp")
    _date(p.get("lastDate"), "lastDate", errs)
    for k in ("totalPosts", "ageMin", "ageMax"):
        _int(p.get(k), k, errs)
    if not isinstance(p.get("qualifications"), list) or any(q not in QUALIFICATIONS for q in p["qualifications"]):
        errs.append("qualifications: unknown value")
    if not isinstance(p.get("states"), list) or not p["states"] or any(s not in STATES for s in p["states"]):
        errs.append("states: unknown value")
    if p.get("verified") is not True:
        errs.append("verified: must be true")
    if not full:
        return errs
    _text(p.get("shortInfo"), "shortInfo", errs, nullable=True)
    for i, r in enumerate(p.get("importantDates") or []):
        _text(r.get("label"), f"importantDates[{i}].label", errs)
        _date(r.get("date"), f"importantDates[{i}].date", errs)
    for i, r in enumerate(p.get("fees") or []):
        _text(r.get("category"), f"fees[{i}].category", errs)
        _int(r.get("amount"), f"fees[{i}].amount", errs)
    age = p.get("age")
    if not isinstance(age, dict):
        errs.append("age: missing")
    else:
        _int(age.get("min"), "age.min", errs)
        _int(age.get("max"), "age.max", errs)
        _date(age.get("asOn"), "age.asOn", errs)
        _text(age.get("relaxation"), "age.relaxation", errs, nullable=True)
    for i, r in enumerate(p.get("vacancies") or []):
        _text(r.get("post"), f"vacancies[{i}].post", errs)
        _int(r.get("total"), f"vacancies[{i}].total", errs)
    for k in ("eligibility", "howToApply"):
        _text(p.get(k), k, errs, nullable=True)
    for i, r in enumerate(p.get("selection") or []):
        _text(r, f"selection[{i}]", errs)
    for i, r in enumerate(p.get("links") or []):
        _text(r.get("label"), f"links[{i}].label", errs)
        if not isinstance(r.get("url"), str) or not r["url"].startswith(("http://", "https://")):
            errs.append(f"links[{i}].url: not a web link")
    return errs


def question_errors(q: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(q, dict):
        return ["question is not an object"]
    if not isinstance(q.get("id"), str) or not q["id"].startswith("q-"):
        errs.append("id: must start with q-")
    _text(q.get("q"), "q", errs)
    opts = q.get("options")
    if not isinstance(opts, list) or len(opts) != 4:
        errs.append("options: need exactly 4")
    else:
        for i, o in enumerate(opts):
            _text(o, f"options[{i}]", errs)
    a = q.get("answer")
    if isinstance(a, bool) or not isinstance(a, int) or not 0 <= a <= 3:
        errs.append("answer: must be 0-3")
    _text(q.get("explanation"), "explanation", errs)
    if q.get("subject") not in SUBJECTS:
        errs.append("subject: unknown")
    if not isinstance(q.get("exams"), list) or any(e not in EXAMS for e in q["exams"]):
        errs.append("exams: unknown value")
    if q.get("difficulty") not in DIFFICULTY:
        errs.append("difficulty: unknown")
    if q.get("verified") is not True:
        errs.append("verified: must be true")
    return errs
