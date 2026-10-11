import copy
import datetime as dt
import json

import pytest

from conftest import NOTICE, NOW, FakeLlm, good_extraction
from feedbot import __main__ as main_mod
from feedbot import jobs, quiz
from feedbot.fetch import Page
from feedbot.llm import LlmError, LlmReplyError
from feedbot.sources import Source, link_id
from feedbot.store import MAX_INDEX, Store
from feedbot.validate import post_errors, question_errors

SRC = Source(id="ssc", name="Staff Selection Commission", url="https://ssc.gov.in/notices", category="ssc")


def page_html(n, start=0):
    items = "".join(f'<li><a href="/n/notice-{i}.pdf">Recruitment notice {i} for posts</a></li>' for i in range(start, start + n))
    return f"<html><body><a href='/'>Home</a><ul>{items}</ul></body></html>"


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.got = []

    def get(self, url):
        self.got.append(url)
        v = self.pages.get(url)
        if v is None:
            if url.endswith(".pdf"):
                return Page(url, 200, "text/html", f"<html><body><pre>{NOTICE}</pre></body></html>".encode())
            raise ConnectionError("no route")
        if isinstance(v, Exception):
            raise v
        if isinstance(v, Page):
            return v
        return Page(url, 200, "text/html", v.encode())


def make_extract(i=0):
    e = good_extraction()
    e["title"]["en"] = f"SSC Notice {i} Recruitment"
    e["title"]["hi"] = f"एसएससी सूचना {i} भर्ती"
    return e


def fresh_status():
    return {"sources": {}}


def test_first_run_bootstraps_only_newest_notices(tmp_path):
    store = Store(tmp_path)
    fetcher = FakeFetcher({SRC.url: page_html(20)})
    llm = FakeLlm(extract=[make_extract(i) for i in range(5)])
    status = fresh_status()
    main_mod.run_jobs(store, llm, fetcher, [SRC], NOW, status)
    seen = store.read("state/seen.json")
    assert len(seen) == main_mod.BOOTSTRAP_KEEP + (20 - main_mod.BOOTSTRAP_KEEP) - (main_mod.BOOTSTRAP_KEEP - main_mod.MAX_NEW_PER_SOURCE)
    assert sum(1 for v in seen.values() if v["status"] == "skipped") == 20 - main_mod.BOOTSTRAP_KEEP
    st = status["sources"]["ssc"]
    assert st["ok"] and st["links"] == 21 and st["candidates"] == 20 and st["new"] == main_mod.BOOTSTRAP_KEEP
    assert st["published"] == main_mod.MAX_NEW_PER_SOURCE
    idx = store.read("jobs/index.json")
    assert idx["schema"] == 1 and len(idx["posts"]) == main_mod.MAX_NEW_PER_SOURCE, "each notice url gets its own id"


def test_published_files_match_schema(tmp_path):
    store = Store(tmp_path)
    main_mod.run_jobs(store, FakeLlm(extract=[make_extract()]), FakeFetcher({SRC.url: page_html(1)}), [SRC], NOW, fresh_status())
    idx = store.read("jobs/index.json")
    for s in idx["posts"]:
        assert post_errors(s, full=False) == []
        full = store.read(f"jobs/posts/{s['id']}.json")
        assert full["schema"] == 1 and post_errors(full) == []


def test_second_run_only_processes_new_links(tmp_path):
    store = Store(tmp_path)
    llm = FakeLlm(extract=[make_extract()])
    main_mod.run_jobs(store, llm, FakeFetcher({SRC.url: page_html(3)}), [SRC], NOW, fresh_status())
    calls = len(llm.calls)
    status = fresh_status()
    fetcher = FakeFetcher({SRC.url: page_html(4)})
    main_mod.run_jobs(store, llm, fetcher, [SRC], NOW + dt.timedelta(minutes=15), status)
    assert status["sources"]["ssc"]["new"] == 1
    assert len(llm.calls) - calls == 2  # one extraction + one verification
    assert fetcher.got.count("https://ssc.gov.in/n/notice-3.pdf") == 1


def test_run_budget_is_respected(tmp_path):
    store = Store(tmp_path)
    srcs = [Source(id=f"s{i}", name=f"S{i}", url=f"https://s{i}.gov.in/", category="ssc") for i in range(4)]
    pages = {s.url: page_html(5) for s in srcs}
    llm = FakeLlm(extract=[make_extract()])
    main_mod.run_jobs(store, llm, FakeFetcher(pages), srcs, NOW, fresh_status(), max_new=7)
    assert llm.count(main_mod.jobs_mod.EXTRACT_SYSTEM) == 7


def test_broken_source_does_not_stop_others(tmp_path):
    store = Store(tmp_path)
    bad = Source(id="bad", name="Bad", url="https://bad.gov.in/", category="ssc")
    status = fresh_status()
    main_mod.run_jobs(store, FakeLlm(extract=[make_extract()]),
                      FakeFetcher({bad.url: ConnectionError("timeout"), SRC.url: page_html(1)}), [bad, SRC], NOW, status)
    assert status["sources"]["bad"]["ok"] is False and "timeout" in status["sources"]["bad"]["error"]
    assert status["sources"]["ssc"]["published"] == 1


def test_http_error_page_is_reported(tmp_path):
    status = fresh_status()
    main_mod.run_jobs(Store(tmp_path), FakeLlm(extract=[make_extract()]),
                      FakeFetcher({SRC.url: Page(SRC.url, 403, "text/html", b"")}), [SRC], NOW, status)
    assert status["sources"]["ssc"]["error"] == "HTTP 403"


def test_broken_notice_is_marked_error_and_not_retried(tmp_path):
    store = Store(tmp_path)
    url = "https://ssc.gov.in/n/notice-0.pdf"
    llm = FakeLlm(extract=[make_extract()])
    main_mod.run_jobs(store, llm, FakeFetcher({SRC.url: page_html(1), url: ConnectionError("x")}), [SRC], NOW, fresh_status())
    seen = store.read("state/seen.json")
    assert seen[link_id(url)]["status"] == "error" and not llm.calls


def test_review_items_are_listed(tmp_path):
    store = Store(tmp_path)
    bad = make_extract()
    bad["org"]["quote"] = "Ministry of Magic"
    main_mod.run_jobs(store, FakeLlm(extract=[bad]), FakeFetcher({SRC.url: page_html(1)}), [SRC], NOW, fresh_status())
    review = store.read("review/index.json")
    assert len(review) == 1 and review[0]["source"] == "ssc" and review[0]["reasons"]
    assert store.read("jobs/index.json") is None


def test_irrelevant_notice_is_remembered(tmp_path):
    store = Store(tmp_path)
    main_mod.run_jobs(store, FakeLlm(extract=[{"relevant": False}]), FakeFetcher({SRC.url: page_html(1)}), [SRC], NOW, fresh_status())
    assert list(store.read("state/seen.json").values())[0]["status"] == "irrelevant"


def test_llm_outage_stops_run_but_keeps_progress(tmp_path):
    class Broken(FakeLlm):
        def json(self, *a, **k):
            raise LlmError("down")

    store = Store(tmp_path)
    with pytest.raises(LlmError):
        main_mod.run_jobs(store, Broken(), FakeFetcher({SRC.url: page_html(1)}), [SRC], NOW, fresh_status())


def test_garbled_reply_skips_that_notice_and_retries_it_once(tmp_path):
    class Garbled(FakeLlm):
        def __init__(self, bad, **k):
            super().__init__(**k)
            self.bad = bad

        def json(self, system, user, **k):
            if self.bad and system == jobs.EXTRACT_SYSTEM and "notice-0" in user:
                self.calls.append((system, user, None))
                raise LlmReplyError("reply JSON is invalid")
            return super().json(system, user, **k)

    store = Store(tmp_path)
    key = link_id("https://ssc.gov.in/n/notice-0.pdf")
    run = lambda llm, n=2: main_mod.run_jobs(store, llm, FakeFetcher({SRC.url: page_html(n)}), [SRC], NOW, fresh_status())
    status = fresh_status()
    main_mod.run_jobs(store, Garbled(True, extract=[make_extract(1)]), FakeFetcher({SRC.url: page_html(2)}), [SRC], NOW, status)
    assert status["sources"]["ssc"]["llmErrors"] == 1
    assert len(store.read("jobs/index.json")["posts"]) == 1  # the other notice still went out
    assert store.read("state/seen.json")[key]["status"] == "retry"
    run(Garbled(True, extract=[make_extract(1)]))
    assert store.read("state/seen.json")[key]["status"] == "error"
    llm = Garbled(True, extract=[make_extract(1)])
    run(llm)
    assert not llm.calls  # given up after two tries


def test_garbled_reply_then_good_reply_publishes(tmp_path):
    class Once(FakeLlm):
        failed = False

        def json(self, system, user, **k):
            if not Once.failed and system == jobs.EXTRACT_SYSTEM:
                Once.failed = True
                raise LlmReplyError("reply has no JSON object")
            return super().json(system, user, **k)

    store = Store(tmp_path)
    for _ in range(2):
        main_mod.run_jobs(store, Once(extract=[make_extract()]), FakeFetcher({SRC.url: page_html(1)}), [SRC], NOW, fresh_status())
    assert list(store.read("state/seen.json").values())[0]["status"] == "published"


def test_index_is_capped(tmp_path):
    store = Store(tmp_path)
    base = {"type": "job", "title": {"en": "t", "hi": "त"}, "org": {"en": "o", "hi": "ओ"}}
    for i in range(MAX_INDEX + 5):
        ts = (NOW + dt.timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        post = {"id": f"p-{i}", "postedAt": ts, **base}
        store.add_post(post, {"id": f"p-{i}", "postedAt": ts})
    idx = store.read("jobs/index.json")
    assert len(idx["posts"]) == MAX_INDEX and idx["posts"][0]["id"] == f"p-{MAX_INDEX + 4}"
    assert not (tmp_path / "jobs/posts/p-0.json").exists()
    assert (tmp_path / f"jobs/posts/p-{MAX_INDEX + 4}.json").exists()


def test_store_survives_corrupt_json(tmp_path):
    (tmp_path / "x.json").write_text("{nope")
    assert Store(tmp_path).read("x.json", default=7) == 7


def test_review_list_replaces_same_key(tmp_path):
    s = Store(tmp_path)
    s.add_review({"key": "a", "n": 1})
    s.add_review({"key": "b"})
    s.add_review({"key": "a", "n": 2})
    assert [e["key"] for e in s.read("review/index.json")] == ["a", "b"]
    assert s.read("review/index.json")[0]["n"] == 2


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Cabinet approves Critical Mineral Mission</title><link>https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2001</link></item>
<item><title>Minister meets delegation</title><link>https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2002</link></item>
</channel></rss>"""
CA_SRC = Source(id="pib", name="PIB", url="https://pib.gov.in/rss", kind="rss")


def quiz_llm():
    from test_quiz import ANSWERS, bank_answers, bank_reply, ca_reply, solver

    answers = dict(ANSWERS)
    answers.update(bank_answers(10))

    def bank(user):
        subj = user.split("Subject: ")[1].split("\n")[0]
        r = bank_reply(10)
        for k, qq in enumerate(r["questions"]):
            qq["q"]["en"] = f"[{subj}] " + qq["q"]["en"]
            answers[qq["q"]["en"]] = bank_answers(10)[f"In which year did event number {k + 1} happen?"]
        return r

    return FakeLlm(ca=[ca_reply()], solve=solver(answers), bank=bank, pick=["2001"])


def release_page():
    from test_quiz import RELEASE
    return f"<html><body><p>{RELEASE}</p></body></html>"


def test_quiz_day_one_and_two(tmp_path):
    store = Store(tmp_path)
    pages = {CA_SRC.url: RSS, "https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2001": release_page()}
    day1 = dt.datetime(2026, 10, 6, 4, 0, tzinfo=dt.timezone.utc)
    llm = quiz_llm()
    status = {}
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], day1, status)
    assert status["quiz"]["today"] == "2026-10-06"
    assert store.read("state/pib.json")["2001"]["day"] == "2026-10-06"
    idx = store.read("quiz/index.json")
    assert idx["bankDay"] == "2026-10-06" and len(idx["banks"]) == len(quiz.BANK_SUBJECTS)
    assert all(b["count"] == 10 for b in idx["banks"])
    assert idx["daily"] == ["2026-10-06"], "70 bank questions are enough for a quiz"
    # day one: nothing was collected yesterday, so today's releases become current affairs
    ca1 = store.read("quiz/current_affairs/2026-10-06.json")
    assert len(ca1["notes"]) == 1 and len(ca1["questions"]) == 2
    d1 = store.read("quiz/daily/2026-10-06.json")
    assert sum(x["subject"] == "current_affairs" for x in d1["questions"]) == 2
    # next day: yesterday's PIB releases become current affairs
    day2 = day1 + dt.timedelta(days=1)
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], day2, status := {})
    ca = store.read("quiz/current_affairs/2026-10-06.json")
    assert len(ca["notes"]) == 1 and len(ca["questions"]) == 2
    assert all(question_errors(x) == [] for x in ca["questions"])
    idx = store.read("quiz/index.json")
    assert idx["currentAffairs"] == ["2026-10-06"] and idx["daily"] == ["2026-10-07", "2026-10-06"]
    d2 = store.read("quiz/daily/2026-10-07.json")
    assert sum(x["subject"] == "current_affairs" for x in d2["questions"]) == 0, "already asked yesterday"
    assert not {x["id"] for x in d1["questions"]} & {x["id"] for x in d2["questions"]}


def test_quiz_rerun_same_day_is_cheap(tmp_path):
    store = Store(tmp_path)
    pages = {CA_SRC.url: RSS, "https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2001": release_page()}
    llm = quiz_llm()
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], NOW, {})
    n = len(llm.calls)
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], NOW + dt.timedelta(minutes=15), {})
    assert len(llm.calls) == n


def test_quiz_old_days_are_pruned(tmp_path):
    store = Store(tmp_path)
    (tmp_path / "quiz/daily").mkdir(parents=True)
    (tmp_path / "quiz/daily/2020-01-01.json").write_text("{}")
    main_mod.run_quiz(store, quiz_llm(), FakeFetcher({CA_SRC.url: RSS}), [CA_SRC], NOW, {})
    assert not (tmp_path / "quiz/daily/2020-01-01.json").exists()


def test_bad_rss_is_reported(tmp_path):
    status = {}
    main_mod.run_quiz(Store(tmp_path), quiz_llm(), FakeFetcher({CA_SRC.url: ConnectionError("x")}), [CA_SRC], NOW, status)
    assert status["quiz"]["rss:pib"].startswith("error")


@pytest.mark.parametrize("xml,count", [(RSS, 2), ("<rss><channel></channel></rss>", 0), ("not xml", 0),
                                       ("<rss><channel><item><title>x</title></item></channel></rss>", 0)])
def test_rss_items(xml, count):
    assert len(main_mod._rss_items(xml)) == count


@pytest.mark.parametrize("url,prid", [("https://pib.gov.in/P.aspx?PRID=123", "123"), ("https://pib.gov.in/p?prid=9", "9"),
                                      ("https://pib.gov.in/x", None)])
def test_prid(url, prid):
    assert main_mod._prid(url) == prid


def test_main_without_key_writes_status_and_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert main_mod.main(["run", "--data", str(tmp_path)]) == 0
    status = json.loads((tmp_path / "status.json").read_text())
    assert "no AI key is set" in status["error"]


def test_main_runs_both_parts_and_records_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    calls = []
    monkeypatch.setattr(main_mod, "run_jobs", lambda *a, **k: calls.append("jobs"))
    monkeypatch.setattr(main_mod, "run_quiz", lambda *a, **k: calls.append("quiz"))
    assert main_mod.main(["run", "--data", str(tmp_path)]) == 0
    assert calls == ["jobs", "quiz"]
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["llm"]["calls"] == 0 and status["run"].endswith("Z")
    assert status["llm"]["provider"] == "deepseek"


@pytest.mark.parametrize("var,provider", [("ANTHROPIC_API_KEY", "claude"), ("GEMINI_API_KEY", "gemini")])
def test_main_uses_whichever_key_is_set(tmp_path, monkeypatch, var, provider):
    monkeypatch.setenv(var, "k")
    monkeypatch.setattr(main_mod, "run_jobs", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "run_quiz", lambda *a, **k: None)
    assert main_mod.main(["run", "--data", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "status.json").read_text())["llm"]["provider"] == provider


def test_main_bad_provider_choice_writes_status(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert main_mod.main(["run", "--data", str(tmp_path)]) == 0
    assert "GEMINI_API_KEY is not set" in json.loads((tmp_path / "status.json").read_text())["error"]


@pytest.mark.parametrize("flag,expected", [("--jobs", ["jobs"]), ("--quiz", ["quiz"])])
def test_main_part_flags(tmp_path, monkeypatch, flag, expected):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    calls = []
    monkeypatch.setattr(main_mod, "run_jobs", lambda *a, **k: calls.append("jobs"))
    monkeypatch.setattr(main_mod, "run_quiz", lambda *a, **k: calls.append("quiz"))
    main_mod.main(["run", "--data", str(tmp_path), flag])
    assert calls == expected


def test_main_reports_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(main_mod, "run_jobs", boom)
    assert main_mod.main(["run", "--data", str(tmp_path), "--jobs"]) == 1
    assert "kaput" in json.loads((tmp_path / "status.json").read_text())["error"]


def test_same_notice_from_two_sites_is_published_once(tmp_path):
    store = Store(tmp_path)
    other = Source(id="empnews", name="Employment News", url="https://employmentnews.gov.in/", category="other")
    pages = {SRC.url: page_html(1), other.url: page_html(1).replace("/n/", "/en/")}
    status = fresh_status()
    main_mod.run_jobs(store, FakeLlm(extract=[make_extract()]), FakeFetcher(pages), [SRC, other], NOW, status)
    assert len(store.read("jobs/index.json")["posts"]) == 1
    assert "duplicate" in {v["status"] for v in store.read("state/seen.json").values()}


def test_out_of_time_stops_new_notices_but_keeps_progress(tmp_path, monkeypatch):
    store = Store(tmp_path)
    srcs = [Source(id=f"s{i}", name=f"S{i}", url=f"https://s{i}.gov.in/", category="ssc") for i in range(3)]
    llm = FakeLlm(extract=[make_extract()])
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: next(clock))
    status = fresh_status()
    # first check is at t=0, the second at t=10: one notice fits before the deadline
    main_mod.run_jobs(store, llm, FakeFetcher({s.url: page_html(3) for s in srcs}), srcs, NOW, status, deadline=5)
    assert llm.count(main_mod.jobs_mod.EXTRACT_SYSTEM) == 1
    assert status["outOfTime"] is True
    seen = store.read("state/seen.json")
    assert sum(1 for v in seen.values() if v["status"] == "published") == 1
    # the next run picks up what was left
    status2 = fresh_status()
    main_mod.run_jobs(store, llm, FakeFetcher({s.url: page_html(3) for s in srcs}), srcs,
                      NOW + dt.timedelta(minutes=15), status2)
    assert "outOfTime" not in status2
    assert llm.count(main_mod.jobs_mod.EXTRACT_SYSTEM) > 1


def test_no_deadline_means_no_limit(tmp_path):
    assert main_mod._out_of_time(None) is False
    assert main_mod._out_of_time(main_mod.time.monotonic() - 1) is True
    assert main_mod._out_of_time(main_mod.time.monotonic() + 60) is False


def test_quiz_out_of_time_skips_ai_and_retries_banks_next_run(tmp_path):
    store = Store(tmp_path)
    pages = {CA_SRC.url: RSS}
    llm = quiz_llm()
    status = {}
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], NOW, status,
                      deadline=main_mod.time.monotonic() - 1)
    assert llm.calls == []
    idx = store.read("quiz/index.json") or {}
    assert idx.get("bankDay") is None, "banks are retried on the next run"
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], NOW + dt.timedelta(minutes=15), {})
    assert store.read("quiz/index.json")["bankDay"] == main_mod.quiz_mod.today_ist(NOW)


def test_main_passes_run_minutes(tmp_path, monkeypatch):
    seen = {}

    def fake_jobs(*a, deadline=None, **k):
        seen["jobs"] = deadline

    def fake_quiz(*a, deadline=None, **k):
        seen["quiz"] = deadline

    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(main_mod, "run_jobs", fake_jobs)
    monkeypatch.setattr(main_mod, "run_quiz", fake_quiz)
    monkeypatch.setattr(main_mod, "load_sources", lambda: ([], []))
    start = main_mod.time.monotonic()
    assert main_mod.main(["run", "--data", str(tmp_path), "--minutes", "2"]) == 0
    assert seen["jobs"] == seen["quiz"]
    assert start + 110 < seen["jobs"] < start + 130


def test_unreadable_releases_retry_next_run(tmp_path):
    store = Store(tmp_path)
    pages = {CA_SRC.url: RSS}  # the release page itself can't be fetched
    llm = quiz_llm()
    status = {}
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], NOW, status)
    assert status["quiz"]["ca"]["notes"] == 0
    assert llm.count(quiz.CA_PICK_SYSTEM) == 1
    pages["https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2001"] = release_page()
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], NOW + dt.timedelta(minutes=15), status := {})
    assert status["quiz"]["ca"]["notes"] == 1


def test_current_affairs_tried_once_per_day_when_read(tmp_path):
    store = Store(tmp_path)
    pages = {CA_SRC.url: RSS, "https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2001": release_page()}
    llm = quiz_llm()
    llm.ca = [{"note": None, "questions": []}]  # the model gives nothing usable
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], NOW, {})
    picks = llm.count(quiz.CA_PICK_SYSTEM)
    assert picks == 1 and store.read("quiz/index.json")["caAttempt"] == quiz.today_ist(NOW)
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], NOW + dt.timedelta(minutes=15), {})
    assert llm.count(quiz.CA_PICK_SYSTEM) == picks


def test_old_tried_marker_is_ignored(tmp_path):
    store = Store(tmp_path)
    idx = store.quiz_index()
    idx["caTried"] = quiz.today_ist(NOW)
    store.save_quiz_index(idx, now="x")
    pages = {CA_SRC.url: RSS, "https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2001": release_page()}
    status = {}
    main_mod.run_quiz(store, quiz_llm(), FakeFetcher(pages), [CA_SRC], NOW, status)
    assert status["quiz"]["ca"]["notes"] == 1
    assert "caTried" not in store.read("quiz/index.json")


def test_current_affairs_prefers_yesterday_when_collected(tmp_path):
    store = Store(tmp_path)
    today = quiz.today_ist(NOW)
    yday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    store.write("state/pib.json", {"1999": {"day": yday, "title": "Old release",
                                            "url": "https://pib.gov.in/PressReleaseIframePage.aspx?PRID=1999"}})
    pages = {CA_SRC.url: RSS, "https://pib.gov.in/PressReleaseIframePage.aspx?PRID=1999": release_page()}
    llm = quiz_llm()
    llm.pick = ["1999"]
    main_mod.run_quiz(store, llm, FakeFetcher(pages), [CA_SRC], NOW, {})
    assert store.exists(f"quiz/current_affairs/{yday}.json")
    assert not store.exists(f"quiz/current_affairs/{today}.json")


def test_rss_http_error_is_reported(tmp_path):
    from feedbot.fetch import Page
    status = {}

    class Denied:
        def get(self, url):
            return Page(url, 403, "text/html", b"<html>Access Denied</html>")

    main_mod.run_quiz(Store(tmp_path), quiz_llm(), Denied(), [CA_SRC], NOW, status)
    assert status["quiz"][f"rss:{CA_SRC.id}"] == "error: HTTP 403"
