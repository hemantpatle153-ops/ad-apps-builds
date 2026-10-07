import copy
import datetime as dt
import json
import random
import re

import pytest

from conftest import FakeLlm
from feedbot import quiz
from feedbot.validate import question_errors

RELEASE = """The Union Cabinet chaired by the Prime Minister approved the National Critical Mineral Mission
with an outlay of Rs 16,300 crore. The mission will run for seven years from 2026 to 2033.
It aims to set up 100 mineral processing parks across the country."""
URL = "https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2320362"


def q(text="Which gas do plants absorb for photosynthesis?", opts=("Oxygen", "Carbon dioxide", "Nitrogen", "Helium"),
      answer=1, hi_opts=("ऑक्सीजन", "कार्बन डाइऑक्साइड", "नाइट्रोजन", "हीलियम"),
      hi="पौधे प्रकाश संश्लेषण के लिए कौन सी गैस लेते हैं?", **extra):
    raw = {"q": {"en": text, "hi": hi},
           "options": [{"en": e, "hi": h} for e, h in zip(opts, hi_opts)],
           "answer": answer,
           "explanation": {"en": "Plants take in carbon dioxide.", "hi": "पौधे कार्बन डाइऑक्साइड लेते हैं।"},
           "difficulty": "easy", "exams": ["ssc", "railway"]}
    raw.update(extra)
    return raw


def test_clean_question_accepts_good_one():
    out, why = quiz.clean_question(q(), subject="science")
    assert why == "" and question_errors(out) == []
    assert out["id"].startswith("q-science-") and out["exams"] == ["ssc", "railway"]


BAD = [
    ("not an object", lambda r: "text"),
    ("question text", lambda r: {**r, "q": {"en": "", "hi": ""}}),
    ("question text", lambda r: {**r, "q": {"en": "What?", "hi": "What?"}}),
    ("question text", lambda r: {**r, "q": {"en": "In 1947?", "hi": "1950 में?"}}),
    ("exactly 4", lambda r: {**r, "options": r["options"][:3]}),
    ("exactly 4", lambda r: {**r, "options": r["options"] + r["options"][:1]}),
    ("exactly 4", lambda r: {**r, "options": None}),
    ("missing a language", lambda r: {**r, "options": [r["options"][0], {"en": "x"}, *r["options"][2:]]}),
    ("not distinct", lambda r: {**r, "options": [r["options"][0], r["options"][0], *r["options"][2:]]}),
    ("not distinct", lambda r: {**r, "options": [r["options"][0], {"en": "OXYGEN", "hi": "ऑक्सीजन गैस"}, *r["options"][2:]]}),
    ("answer index", lambda r: {**r, "answer": 4}),
    ("answer index", lambda r: {**r, "answer": -1}),
    ("answer index", lambda r: {**r, "answer": "1"}),
    ("answer index", lambda r: {**r, "answer": True}),
    ("answer index", lambda r: {**r, "answer": None}),
    ("explanation", lambda r: {**r, "explanation": None}),
    ("explanation", lambda r: {**r, "explanation": {"en": "It is 5.", "hi": "यह 6 है।"}}),
    ("time-bound", lambda r: {**r, "q": {"en": "Who is the current Chief Justice?", "hi": "वर्तमान मुख्य न्यायाधीश कौन हैं?"}}),
    ("time-bound", lambda r: {**r, "q": {"en": "Which country recently won the cup?", "hi": "हाल ही में कप किसने जीता?"}}),
    ("all/none", lambda r: {**r, "options": [*r["options"][:3], {"en": "None of the above", "hi": "इनमें से कोई नहीं"}]}),
    ("all/none", lambda r: {**r, "options": [*r["options"][:3], {"en": "All of the above", "hi": "उपरोक्त सभी"}]}),
]


@pytest.mark.parametrize("why,mutate", BAD, ids=[f"{i}-{w}" for i, (w, _) in enumerate(BAD)])
def test_clean_question_rejects(why, mutate):
    out, reason = quiz.clean_question(mutate(q()), subject="science")
    assert out is None and why in reason


def test_time_words_allowed_in_current_affairs():
    raw = q(text="Which mission was approved recently?")
    out, _ = quiz.clean_question(raw, subject="current_affairs")
    assert out is not None


@pytest.mark.parametrize("diff,expected", [("easy", "easy"), ("hard", "hard"), ("tough", "medium"), (None, "medium")])
def test_difficulty_defaults(diff, expected):
    out, _ = quiz.clean_question(q(difficulty=diff), subject="science")
    assert out["difficulty"] == expected


@pytest.mark.parametrize("exams,expected", [(["ssc"], ["ssc"]), (["nasa"], list(quiz.EXAMS)), (None, list(quiz.EXAMS)),
                                            (["upsc", "x", "banking"], ["upsc", "banking"])])
def test_exam_tags(exams, expected):
    out, _ = quiz.clean_question(q(exams=exams), subject="science")
    assert out["exams"] == expected


def test_ids_are_stable_and_ignore_layout():
    a = quiz.question_id("gk", "Capital of India?")
    assert a == quiz.question_id("gk", "  capital of   india ? ")
    assert a != quiz.question_id("history", "Capital of India?")


def solver(answer_by_text):
    """Answers by option text, so the verifier is independent of option order."""
    def solve(user):
        out = []
        for block in re.split(r"\n(?=\d+\. )", user.split("QUESTIONS:\n", 1)[1]):
            m = re.match(r"(\d+)\. (.*?)\n", block)
            if not m:
                continue
            idx, text = int(m.group(1)), m.group(2)
            opts = re.findall(r"  (\d)\) (.*)", block)
            want = answer_by_text.get(text)
            ans = next((int(k) for k, o in opts if o == want), None)
            out.append({"id": idx, "answer": ans, "sure": ans is not None})
        return {"answers": out}
    return solve


@pytest.mark.parametrize("seed", range(40))
def test_solve_maps_shuffled_options_back(seed):
    qq, _ = quiz.clean_question(q(), subject="science")
    rng = random.Random(seed)
    perm = [0, 1, 2, 3]
    rng.shuffle(perm)
    llm = FakeLlm(solve=solver({qq["q"]["en"]: "Carbon dioxide"}))
    assert quiz.solve(llm, [qq], order=[perm]) == [1]


def test_solve_handles_unsure_and_garbage():
    qq, _ = quiz.clean_question(q(), subject="science")
    llm = FakeLlm(solve=lambda u: {"answers": [{"id": 1, "answer": 2, "sure": False}, {"id": "x"}, {"id": 2, "answer": 9}]})
    assert quiz.solve(llm, [qq, qq]) == [None, None]


def ca_reply(**over):
    reply = {
        "note": {"title": {"en": "Critical Mineral Mission approved", "hi": "क्रिटिकल मिनरल मिशन को मंज़ूरी"},
                 "body": {"en": "Cabinet approved the National Critical Mineral Mission with Rs 16,300 crore for 2026 to 2033.",
                          "hi": "कैबिनेट ने 2026 से 2033 के लिए 16,300 करोड़ रुपये के राष्ट्रीय क्रिटिकल मिनरल मिशन को मंज़ूरी दी।"},
                 "tags": ["economy"], "quote": "approved the National Critical Mineral Mission with an outlay of Rs 16,300 crore"},
        "questions": [
            {"q": {"en": "What is the outlay of the National Critical Mineral Mission?",
                   "hi": "राष्ट्रीय क्रिटिकल मिनरल मिशन का परिव्यय कितना है?"},
             "options": [{"en": "Rs 16,300 crore", "hi": "16,300 करोड़ रुपये"}, {"en": "Rs 18,000 crore", "hi": "18,000 करोड़ रुपये"},
                         {"en": "Rs 12,500 crore", "hi": "12,500 करोड़ रुपये"}, {"en": "Rs 20,000 crore", "hi": "20,000 करोड़ रुपये"}],
             "answer": 0, "explanation": {"en": "The outlay is Rs 16,300 crore.", "hi": "परिव्यय 16,300 करोड़ रुपये है।"},
             "difficulty": "medium", "exams": ["upsc", "ssc"], "quote": "with an outlay of Rs 16,300 crore"},
            {"q": {"en": "How many mineral processing parks will the mission set up?",
                   "hi": "मिशन कितने खनिज प्रसंस्करण पार्क स्थापित करेगा?"},
             "options": [{"en": "50", "hi": "50"}, {"en": "75", "hi": "75"}, {"en": "100", "hi": "100"}, {"en": "150", "hi": "150"}],
             "answer": 2, "explanation": {"en": "It aims at 100 parks.", "hi": "लक्ष्य 100 पार्क है।"},
             "difficulty": "easy", "exams": ["ssc"], "quote": "It aims to set up 100 mineral processing parks"},
        ],
    }
    reply.update(over)
    return reply


ANSWERS = {"What is the outlay of the National Critical Mineral Mission?": "Rs 16,300 crore",
           "How many mineral processing parks will the mission set up?": "100"}


def ca(llm):
    report = quiz.Report()
    note, kept = quiz.current_affairs_item(llm, title="Mission", url=URL, text=RELEASE, date="2026-10-06",
                                           index=1, report=report)
    return note, kept, report


def test_current_affairs_happy_path():
    note, kept, report = ca(FakeLlm(ca=[ca_reply()], solve=solver(ANSWERS)))
    assert note["id"] == "ca-2026-10-06-1" and note["source"]["url"] == URL
    assert len(kept) == 2 and report.kept == 2 and report.rejected == 0
    assert all(question_errors(x) == [] for x in kept)
    assert all(x["source"] == {"name": "PIB", "url": URL} for x in kept)


def test_verifier_disagreement_rejects_question():
    wrong = dict(ANSWERS)
    wrong["How many mineral processing parks will the mission set up?"] = "75"
    note, kept, report = ca(FakeLlm(ca=[ca_reply()], solve=solver(wrong)))
    assert len(kept) == 1 and report.rejected == 1


def test_note_with_invented_number_is_dropped():
    r = ca_reply()
    r["note"]["body"] = {"en": "Cabinet approved Rs 26,300 crore.", "hi": "कैबिनेट ने 26,300 करोड़ मंज़ूर किए।"}
    note, kept, report = ca(FakeLlm(ca=[r], solve=solver(ANSWERS)))
    assert note is None and len(kept) == 2


def test_note_with_fake_quote_is_dropped():
    r = ca_reply()
    r["note"]["quote"] = "The mission was cancelled"
    note, _, _ = ca(FakeLlm(ca=[r], solve=solver(ANSWERS)))
    assert note is None


def test_long_note_is_dropped():
    r = ca_reply()
    r["note"]["body"] = {"en": "word " * 90, "hi": "शब्द " * 90}
    note, _, _ = ca(FakeLlm(ca=[r], solve=solver(ANSWERS)))
    assert note is None


def test_question_whose_answer_is_not_in_release_is_rejected():
    r = ca_reply()
    r["questions"][1]["options"][2] = {"en": "120", "hi": "120"}
    r["questions"][1]["answer"] = 2
    note, kept, report = ca(FakeLlm(ca=[r], solve=solver(ANSWERS)))
    assert len(kept) == 1 and any("correct option" in x for x in report.reasons)


def test_question_with_fake_quote_is_rejected():
    r = ca_reply()
    r["questions"][0]["quote"] = "outlay of Rs 99 crore"
    _, kept, report = ca(FakeLlm(ca=[r], solve=solver(ANSWERS)))
    assert len(kept) == 1 and any("quote not in release" in x for x in report.reasons)


def test_no_questions_means_no_solve_call():
    llm = FakeLlm(ca=[ca_reply(questions=[])])
    note, kept, _ = ca(llm)
    assert note and kept == [] and llm.count(quiz.SOLVE_SYSTEM) == 0


def bank_reply(n=5, subject="history"):
    qs = []
    for i in range(n):
        qs.append({"q": {"en": f"In which year did event number {i + 1} happen?", "hi": f"घटना संख्या {i + 1} किस वर्ष हुई?"},
                   "options": [{"en": str(1900 + i * 4 + k), "hi": str(1900 + i * 4 + k)} for k in range(4)],
                   "answer": i % 4, "explanation": {"en": f"It was {1900 + i * 4 + i % 4}.", "hi": f"यह {1900 + i * 4 + i % 4} था।"},
                   "difficulty": "medium", "exams": ["ssc"]})
    return {"questions": qs}


def bank_answers(n=5, wrong=()):
    return {f"In which year did event number {i + 1} happen?": str(1900 + i * 4 + (i % 4 if i not in wrong else (i + 1) % 4))
            for i in range(n)}


@pytest.mark.parametrize("seed", range(10))
def test_bank_keeps_only_double_confirmed(seed):
    llm = FakeLlm(bank=lambda u: bank_reply(), solve=solver(bank_answers(wrong={1, 3})))
    report = quiz.Report()
    kept = quiz.bank_batch(llm, "history", existing=set(), count=5, report=report, rng=random.Random(seed))
    assert [k["q"]["en"] for k in kept] == [f"In which year did event number {i} happen?" for i in (1, 3, 5)]
    assert llm.count(quiz.SOLVE_SYSTEM) == 2 and report.rejected == 2


def test_bank_second_pass_uses_new_option_order():
    seen_orders = []

    def solve(user):
        seen_orders.append(re.findall(r"  \d\) (\S+)", user)[:4])
        return solver(bank_answers(1))(user)

    llm = FakeLlm(bank=lambda u: bank_reply(1), solve=solve)
    quiz.bank_batch(llm, "history", existing=set(), count=1, report=quiz.Report(), rng=random.Random(1))
    assert seen_orders[0] != seen_orders[1] and sorted(seen_orders[0]) == sorted(seen_orders[1])


def test_bank_skips_duplicates():
    existing = {quiz.compact("In which year did event number 1 happen?")}
    llm = FakeLlm(bank=lambda u: bank_reply(2), solve=solver(bank_answers(2)))
    report = quiz.Report()
    kept = quiz.bank_batch(llm, "history", existing=existing, count=2, report=report, rng=random.Random(0))
    assert len(kept) == 1 and "history: duplicate" in report.reasons


def test_bank_with_nothing_valid_makes_no_solve_call():
    llm = FakeLlm(bank=lambda u: {"questions": [{"q": "bad"}]})
    assert quiz.bank_batch(llm, "gk", existing=set(), count=3, report=quiz.Report(), rng=random.Random(0)) == []


def make_bank(subject, n):
    out = []
    for i in range(n):
        raw = q(text=f"{subject} question {i}?", hi=f"प्रश्न {i}?")
        qq, _ = quiz.clean_question(raw, subject=subject)
        out.append(qq)
    return out


BANKS = {s: make_bank(s, 30) for s in quiz.BANK_SUBJECTS}
CA_QS = [quiz.clean_question(q(text=f"CA question {i}?", hi=f"प्रश्न {i}?"), subject="current_affairs")[0] for i in range(12)]
DATES = [(dt.date(2026, 10, 7) + dt.timedelta(days=i)).isoformat() for i in range(60)]


@pytest.mark.parametrize("date", DATES)
def test_daily_quiz_is_deterministic_and_valid(date):
    a = quiz.daily_quiz(date, ca_questions=CA_QS, banks=BANKS, recent_ids=set())
    b = quiz.daily_quiz(date, ca_questions=CA_QS, banks=BANKS, recent_ids=set())
    assert a == b and len(a["questions"]) == 10 and a["date"] == date
    assert len({x["id"] for x in a["questions"]}) == 10
    assert sum(x["subject"] == "current_affairs" for x in a["questions"]) == 4
    assert all(question_errors(x) == [] for x in a["questions"])
    assert a["title"]["en"].startswith("Daily Quiz") and a["title"]["hi"].startswith("दैनिक क्विज़")


def test_daily_quizzes_do_not_repeat_for_three_weeks():
    used: set[str] = set()
    for date in DATES[:21]:
        d = quiz.daily_quiz(date, ca_questions=CA_QS, banks=BANKS, recent_ids=used)
        assert d is not None
        ids = {x["id"] for x in d["questions"]}
        assert not ids & used
        used |= ids


def test_daily_quiz_needs_enough_questions():
    small = {"gk": make_bank("gk", 3)}
    assert quiz.daily_quiz("2026-10-07", ca_questions=[], banks=small, recent_ids=set()) is None


def test_daily_quiz_spreads_subjects():
    d = quiz.daily_quiz("2026-10-07", ca_questions=[], banks=BANKS, recent_ids=set())
    assert len({x["subject"] for x in d["questions"]}) >= 6


@pytest.mark.parametrize("hour,expected", [(0, "2026-10-07"), (18, "2026-10-07"), (18.5, "2026-10-08"), (23, "2026-10-08")])
def test_today_ist(hour, expected):
    now = dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc) + dt.timedelta(hours=hour)
    assert quiz.today_ist(now) == expected


def test_report_caps_reasons():
    r = quiz.Report()
    for i in range(500):
        r.reject(str(i))
    assert r.rejected == 500 and len(r.reasons) == 200


def test_dumps_keeps_hindi_readable():
    assert "दैनिक" in quiz.dumps({"x": "दैनिक"})
    assert json.loads(quiz.dumps({"a": [1]})) == {"a": [1]}


def test_bank_subjects_have_titles():
    from feedbot.__main__ import SUBJECT_TITLES
    assert set(SUBJECT_TITLES) == set(quiz.BANK_SUBJECTS)
    assert set(quiz.BANK_SUBJECTS) <= set(quiz.SUBJECTS)


def test_clean_question_does_not_mutate_input():
    raw = q()
    before = copy.deepcopy(raw)
    quiz.clean_question(raw, subject="science")
    assert raw == before
