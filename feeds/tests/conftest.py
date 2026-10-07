from __future__ import annotations

import copy
import datetime as dt
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feedbot import jobs, quiz  # noqa: E402
from feedbot.sources import Source  # noqa: E402

NOTICE = """STAFF SELECTION COMMISSION
Notice
Combined Graduate Level Examination, 2026
Dates for submission of online applications: 10-09-2026 to 05-10-2026
Last date and time for receipt of online applications: 05.10.2026 (23:00)
Last date and time for making online fee payment: 06.10.2026 (23:00)
Schedule of Computer Based Examination (Tier-I): November-December, 2026
The Commission will hold the Combined Graduate Level Examination for filling up of 14582 Group B and Group C posts.
Application Fee: Rs 100/- (Rupees One Hundred only). Women candidates and candidates belonging to SC, ST, PwBD and ESM are exempted from payment of fee.
Age Limit: 18-32 years as on 01.08.2026. Age relaxation: OBC 3 years, SC/ST 5 years.
Essential Qualification: Bachelor's Degree from a recognized University or equivalent.
Pay Level: Rs 25,500 to Rs 1,51,100 (Pay Level-4 to Level-8).
Vacancy: Assistant Section Officer 4200 posts (UR 1700, OBC 1100, EWS 420, SC 630, ST 350).
Selection: Tier-I Computer Based Examination and Tier-II Computer Based Examination followed by document verification.
Candidates must apply online through the official website https://ssc.gov.in only.
"""

SOURCE = Source(id="ssc", name="Staff Selection Commission", url="https://ssc.gov.in/", category="ssc")
NOTICE_URL = "https://ssc.gov.in/api/attachment/uploads/cgl-2026-notice.pdf"
LINKS = {"https://ssc.gov.in"}
NOW = dt.datetime(2026, 9, 12, 6, 0, tzinfo=dt.timezone.utc)


def good_extraction() -> dict[str, Any]:
    return {
        "relevant": True,
        "type": "job",
        "title": {"en": "SSC CGL 2026 Recruitment", "hi": "एसएससी सीजीएल 2026 भर्ती"},
        "org": {"en": "Staff Selection Commission", "hi": "कर्मचारी चयन आयोग", "quote": "STAFF SELECTION COMMISSION"},
        "category": "ssc",
        "shortInfo": {"en": "SSC will fill 14582 Group B and C posts through CGL 2026.",
                      "hi": "एसएससी सीजीएल 2026 से 14582 ग्रुप बी और सी पद भरेगा।"},
        "lastDate": {"value": "2026-10-05", "quote": "Last date and time for receipt of online applications: 05.10.2026 (23:00)"},
        "totalPosts": {"value": 14582, "quote": "filling up of 14582 Group B and Group C posts"},
        "ageMin": {"value": 18, "quote": "Age Limit: 18-32 years as on 01.08.2026"},
        "ageMax": {"value": 32, "quote": "Age Limit: 18-32 years as on 01.08.2026"},
        "ageAsOn": {"value": "2026-08-01", "quote": "Age Limit: 18-32 years as on 01.08.2026"},
        "ageRelaxation": {"en": "OBC 3 years, SC/ST 5 years", "hi": "ओबीसी 3 वर्ष, एससी/एसटी 5 वर्ष",
                          "quote": "Age relaxation: OBC 3 years, SC/ST 5 years."},
        "salary": {"en": "Rs 25,500 to Rs 1,51,100 (Level 4 to 8)", "hi": "₹25,500 से ₹1,51,100 (लेवल 4 से 8)",
                   "quote": "Pay Level: Rs 25,500 to Rs 1,51,100 (Pay Level-4 to Level-8)."},
        "qualifications": {"value": ["graduate"], "quote": "Essential Qualification: Bachelor's Degree from a recognized University"},
        "states": {"value": ["all"], "quote": ""},
        "importantDates": [
            {"label": {"en": "Apply start", "hi": "आवेदन शुरू"}, "date": "2026-09-10", "text": "10-09-2026",
             "quote": "Dates for submission of online applications: 10-09-2026 to 05-10-2026"},
            {"label": {"en": "Last date", "hi": "अंतिम तिथि"}, "date": "2026-10-05", "text": "05.10.2026 (23:00)",
             "quote": "Last date and time for receipt of online applications: 05.10.2026 (23:00)"},
            {"label": {"en": "Fee payment last date", "hi": "शुल्क भुगतान अंतिम तिथि"}, "date": "2026-10-06",
             "text": "06.10.2026 (23:00)", "quote": "Last date and time for making online fee payment: 06.10.2026 (23:00)"},
        ],
        "fees": [
            {"category": {"en": "General / OBC / EWS", "hi": "सामान्य / ओबीसी / ईडब्ल्यूएस"}, "amount": 100,
             "text": "Rs 100", "quote": "Application Fee: Rs 100/- (Rupees One Hundred only)"},
            {"category": {"en": "Women, SC, ST, PwBD, ESM", "hi": "महिला, एससी, एसटी, पीडब्ल्यूबीडी, ईएसएम"}, "amount": 0,
             "text": "Exempted", "quote": "Women candidates and candidates belonging to SC, ST, PwBD and ESM are exempted from payment of fee"},
        ],
        "vacancies": [
            {"post": {"en": "Assistant Section Officer", "hi": "सहायक अनुभाग अधिकारी"}, "total": 4200,
             "breakdown": "UR 1700, OBC 1100, EWS 420, SC 630, ST 350",
             "quote": "Assistant Section Officer 4200 posts (UR 1700, OBC 1100, EWS 420, SC 630, ST 350)"},
        ],
        "eligibility": {"en": "Bachelor's Degree from a recognized University", "hi": "मान्यता प्राप्त विश्वविद्यालय से स्नातक डिग्री",
                        "quote": "Essential Qualification: Bachelor's Degree from a recognized University or equivalent."},
        "selection": [
            {"en": "Tier-I Computer Based Examination", "hi": "टियर-I कंप्यूटर आधारित परीक्षा",
             "quote": "Selection: Tier-I Computer Based Examination and Tier-II Computer Based Examination"},
            {"en": "Document verification", "hi": "दस्तावेज़ सत्यापन",
             "quote": "followed by document verification"},
        ],
        "howToApply": {"en": "Apply online at https://ssc.gov.in only.", "hi": "केवल https://ssc.gov.in पर ऑनलाइन आवेदन करें।",
                       "quote": "Candidates must apply online through the official website https://ssc.gov.in only."},
        "links": [{"label": {"en": "Apply online", "hi": "ऑनलाइन आवेदन"}, "url": "https://ssc.gov.in"}],
    }


class FakeLlm:
    """Scripted DeepSeek: routes by system prompt, records every call."""

    def __init__(self, *, extract: list[dict[str, Any]] | None = None,
                 verify: Callable[[str], dict[str, Any]] | None = None,
                 solve: Callable[[str], dict[str, Any]] | None = None,
                 ca: list[dict[str, Any]] | None = None,
                 bank: Callable[[str], dict[str, Any]] | None = None,
                 pick: list[str] | None = None) -> None:
        self.extract = list(extract or [])
        self.verify_fn = verify or (lambda user: {"results": [{"id": i, "verdict": "yes"} for i in range(1, 200)]})
        self.solve_fn = solve
        self.ca = list(ca or [])
        self.bank_fn = bank
        self.pick = pick
        self.calls: list[tuple[str, str, str | None]] = []

    def json(self, system: str, user: str, *, model: str | None = None, max_tokens: int = 4000) -> dict[str, Any]:
        self.calls.append((system, user, model))
        if system == jobs.VERIFY_SYSTEM:
            return self.verify_fn(user)
        if system in (jobs.EXTRACT_SYSTEM, jobs.FIX_SYSTEM):
            return copy.deepcopy(self.extract.pop(0) if len(self.extract) > 1 else self.extract[0])
        if system == quiz.SOLVE_SYSTEM:
            assert self.solve_fn, "unexpected solve call"
            return self.solve_fn(user)
        if system == quiz.CA_SYSTEM:
            return copy.deepcopy(self.ca.pop(0) if len(self.ca) > 1 else self.ca[0])
        if system == quiz.BANK_SYSTEM:
            assert self.bank_fn, "unexpected bank call"
            return self.bank_fn(user)
        if system == quiz.CA_PICK_SYSTEM:
            return {"pick": self.pick or []}
        raise AssertionError("unknown prompt")

    def count(self, system: str) -> int:
        return sum(1 for s, _, _ in self.calls if s == system)


@pytest.fixture
def extraction() -> dict[str, Any]:
    return good_extraction()
