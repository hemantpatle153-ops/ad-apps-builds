import copy
import datetime as dt

import pytest

from conftest import LINKS, NOTICE, NOTICE_URL, NOW, SOURCE, FakeLlm, good_extraction
from feedbot import jobs
from feedbot.validate import post_errors


def paths(fails):
    return {f.path for f in fails}


def test_good_extraction_passes_every_check(extraction):
    assert jobs.check_facts(extraction, NOTICE, LINKS, today=NOW.date()) == []


# Each mutation plants one wrong fact; the checker must name exactly that fact.
MUTATIONS = [
    ("lastDate", lambda d: d["lastDate"].update(value="2026-10-15")),
    ("lastDate", lambda d: d["lastDate"].update(value="2026-05-10")),
    ("lastDate", lambda d: d["lastDate"].update(value="05-10-2026")),
    ("lastDate", lambda d: d["lastDate"].update(quote="Last date is 15 October 2026")),
    ("lastDate", lambda d: d["lastDate"].update(value="2031-10-05", quote="05.10.2031")),
    ("totalPosts", lambda d: d["totalPosts"].update(value=14852)),
    ("totalPosts", lambda d: d["totalPosts"].update(value=1458)),
    ("totalPosts", lambda d: d["totalPosts"].update(value="14582")),
    ("totalPosts", lambda d: d["totalPosts"].update(value=True)),
    ("totalPosts", lambda d: d["totalPosts"].update(value=0, quote="0 posts")),
    ("ageMin", lambda d: d["ageMin"].update(value=21)),
    ("ageMax", lambda d: d["ageMax"].update(value=30)),
    ("ageMax", lambda d: d["ageMax"].update(value=95, quote="95")),
    ("ageMax", lambda d: (d["ageMin"].update(value=32), d["ageMax"].update(value=18))),
    ("ageAsOn", lambda d: d["ageAsOn"].update(value="2026-01-08")),
    ("salary", lambda d: d["salary"].update(en="Rs 35,400 to Rs 1,12,400", hi="₹35,400 से ₹1,12,400")),
    ("salary", lambda d: d["salary"].update(hi="₹25,500 से ₹1,51,000 (लेवल 4 से 8)")),
    ("salary", lambda d: d["salary"].update(hi="Rs 25,500 to Rs 1,51,100 (Level 4 to 8)")),
    ("salary", lambda d: d["salary"].update(quote="Pay: Rs 35,400")),
    ("ageRelaxation", lambda d: d["ageRelaxation"].update(en="OBC 5 years, SC/ST 10 years", hi="ओबीसी 5 वर्ष, एससी/एसटी 10 वर्ष")),
    ("eligibility", lambda d: d["eligibility"].update(quote="Master's degree required")),
    ("howToApply", lambda d: d["howToApply"].update(en="", hi="")),
    ("qualifications", lambda d: d["qualifications"].update(value=["phd"])),
    ("qualifications", lambda d: d["qualifications"].update(quote="Class 10 pass")),
    ("states", lambda d: d["states"].update(value=["XX"])),
    ("importantDates[0]", lambda d: d["importantDates"][0].update(date="2026-09-11")),
    ("importantDates[1]", lambda d: d["importantDates"][1].update(quote="Last date 10.11.2026")),
    ("importantDates[2]", lambda d: d["importantDates"].__setitem__(2, "bad row")),
    ("importantDates[0].label", lambda d: d["importantDates"][0]["label"].update(hi="")),
    ("fees[0]", lambda d: d["fees"][0].update(amount=1000)),
    ("fees[0]", lambda d: d["fees"][0].update(amount=-5)),
    ("fees[0]", lambda d: d["fees"][0].update(amount="100")),
    ("fees[1]", lambda d: d["fees"][1].update(quote="Women pay Rs 50")),
    ("fees[1].category", lambda d: d["fees"][1].update(category={"en": "", "hi": ""})),
    ("vacancies[0]", lambda d: d["vacancies"][0].update(total=4300)),
    ("vacancies[0]", lambda d: d["vacancies"][0].update(breakdown="UR 1800, OBC 1100")),
    ("vacancies[0]", lambda d: d["vacancies"][0].update(quote="ASO 4300 posts")),
    ("vacancies[0]", lambda d: d["vacancies"][0].update(total=0)),
    ("selection[0]", lambda d: d["selection"][0].update(quote="Interview round")),
    ("selection[1]", lambda d: d["selection"][1].update(hi="Document verification")),
    ("links[0]", lambda d: d["links"][0].update(url="https://fake-ssc-jobs.com/apply")),
    ("links[0]", lambda d: d["links"][0].update(url="ftp://ssc.gov.in")),
    ("links[0].label", lambda d: d["links"][0].update(label={"en": "Apply", "hi": ""})),
    ("title", lambda d: d.update(title={"en": "SSC CGL", "hi": ""})),
    ("title", lambda d: d.update(title={"en": "SSC CGL 2026", "hi": "एसएससी सीजीएल 2025"})),
    ("org", lambda d: d["org"].update(quote="Union Public Service Commission")),
    ("shortInfo", lambda d: d["shortInfo"].update(en="SSC will fill 20000 posts.", hi="एसएससी 20000 पद भरेगा।")),
    ("type", lambda d: d.update(type="vacancy")),
    ("category", lambda d: d.update(category="space")),
]


@pytest.mark.parametrize("path,mutate", MUTATIONS, ids=[f"{i}-{p}" for i, (p, _) in enumerate(MUTATIONS)])
def test_each_wrong_fact_is_caught(path, mutate):
    data = good_extraction()
    mutate(data)
    fails = jobs.check_facts(data, NOTICE, LINKS, today=NOW.date())
    assert path in paths(fails), [str(f) for f in fails]


@pytest.mark.parametrize("path,mutate", MUTATIONS, ids=[f"{i}-{p}" for i, (p, _) in enumerate(MUTATIONS)])
def test_dropping_failed_facts_leaves_only_good_ones(path, mutate):
    data = good_extraction()
    mutate(data)
    fails = jobs.check_facts(data, NOTICE, LINKS, today=NOW.date())
    cleaned, dropped = jobs.drop_failed(data, fails)
    remaining = jobs.check_facts(cleaned, NOTICE, LINKS, today=NOW.date())
    core = {"title", "org", "type", "category"}
    assert paths(remaining) <= core | {f.path for f in remaining if f.path.split(".")[0].split("[")[0] in core}
    if path.split(".")[0].split("[")[0] not in core:
        assert dropped >= 1


@pytest.mark.parametrize("field", ["lastDate", "totalPosts", "ageMin", "ageMax", "ageAsOn"])
def test_null_scalars_are_allowed(field, extraction):
    extraction[field] = {"value": None, "quote": ""}
    assert jobs.check_facts(extraction, NOTICE, LINKS, today=NOW.date()) == []


@pytest.mark.parametrize("field", ["salary", "ageRelaxation", "eligibility", "howToApply"])
def test_optional_texts_may_be_null(field, extraction):
    extraction[field] = None
    assert jobs.check_facts(extraction, NOTICE, LINKS, today=NOW.date()) == []


@pytest.mark.parametrize("field", ["importantDates", "fees", "vacancies", "selection", "links"])
def test_empty_lists_are_allowed(field, extraction):
    extraction[field] = []
    assert jobs.check_facts(extraction, NOTICE, LINKS, today=NOW.date()) == []


def test_link_written_in_notice_text_is_accepted(extraction):
    extraction["links"] = [{"label": {"en": "Website", "hi": "वेबसाइट"}, "url": "https://ssc.gov.in"}]
    assert jobs.check_facts(extraction, NOTICE, set(), today=NOW.date()) == []


def test_drop_failed_counts_and_keeps_order(extraction):
    fails = [jobs.Failure("fees[0]", "x"), jobs.Failure("importantDates[1]", "x"), jobs.Failure("salary", "x"),
             jobs.Failure("lastDate", "x"), jobs.Failure("qualifications", "x"), jobs.Failure("fees[9]", "x")]
    cleaned, dropped = jobs.drop_failed(extraction, fails)
    assert dropped == 5
    assert [r["text"] for r in cleaned["importantDates"]] == ["10-09-2026", "06.10.2026 (23:00)"]
    assert len(cleaned["fees"]) == 1 and cleaned["fees"][0]["amount"] == 0
    assert cleaned["salary"] is None and cleaned["lastDate"]["value"] is None
    assert cleaned["qualifications"]["value"] == []
    assert extraction["salary"] is not None, "original must not change"


def run(llm, **kw):
    return jobs.process_notice(llm, SOURCE, notice_url=NOTICE_URL, link_text="CGL 2026 notice",
                               text=kw.pop("text", NOTICE), links=LINKS, now=NOW, **kw)


def test_clean_notice_publishes_in_one_round(extraction):
    llm = FakeLlm(extract=[extraction])
    out = run(llm)
    assert out.status == "published" and out.rounds == 1 and out.facts_dropped == 0
    assert llm.count(jobs.EXTRACT_SYSTEM) == 1 and llm.count(jobs.FIX_SYSTEM) == 0
    assert llm.count(jobs.VERIFY_SYSTEM) == 1
    assert post_errors(out.post) == []
    p = out.post
    assert p["lastDate"] == "2026-10-05" and p["totalPosts"] == 14582 and p["officialNoticeUrl"] == NOTICE_URL
    assert p["id"].startswith("ssc-ssc-cgl-2026-recruitment-")
    assert p["age"] == {"min": 18, "max": 32, "asOn": "2026-08-01",
                        "relaxation": {"en": "OBC 3 years, SC/ST 5 years", "hi": "ओबीसी 3 वर्ष, एससी/एसटी 5 वर्ष"}}


def test_wrong_fact_is_fixed_in_the_next_round(extraction):
    bad = copy.deepcopy(extraction)
    bad["lastDate"]["value"] = "2026-10-15"
    llm = FakeLlm(extract=[bad, extraction])
    out = run(llm)
    assert out.status == "published" and out.rounds == 2
    assert out.post["lastDate"] == "2026-10-05"
    fix_prompt = [u for s, u, _ in llm.calls if s == jobs.FIX_SYSTEM][0]
    assert "lastDate: date 2026-10-15 is not written in the quote" in fix_prompt


def test_fact_that_never_gets_fixed_is_dropped(extraction):
    bad = copy.deepcopy(extraction)
    bad["fees"][0]["amount"] = 1000
    llm = FakeLlm(extract=[bad])
    out = run(llm)
    assert out.status == "published" and out.rounds == jobs.MAX_ROUNDS
    assert [f["amount"] for f in out.post["fees"]] == [0]
    assert out.facts_dropped == 1


def test_verifier_disagreement_drops_the_fact(extraction):
    def verify(user):
        lines = [ln for ln in user.split("CLAIMS:\n")[1].splitlines() if ln.strip()]
        res = []
        for ln in lines:
            i = int(ln.split(".")[0])
            res.append({"id": i, "verdict": "no" if "total number of posts" in ln else "yes"})
        return {"results": res}

    out = run(FakeLlm(extract=[extraction], verify=verify))
    assert out.status == "published"
    assert out.post["totalPosts"] == 4200, "falls back to the vacancy table sum"
    assert any("independent check said no" in r for r in out.reasons)


def test_verifier_silence_counts_as_unclear(extraction):
    out = run(FakeLlm(extract=[extraction], verify=lambda u: {"results": []}))
    assert out.status == "review"


def test_irrelevant_notice_is_skipped(extraction):
    out = run(FakeLlm(extract=[{"relevant": False}]))
    assert out.status == "irrelevant" and out.post is None


def test_unreadable_notice_goes_to_review():
    llm = FakeLlm(extract=[{}])
    out = run(llm, text="  ")
    assert out.status == "review" and not llm.calls


def test_unconfirmed_org_goes_to_review(extraction):
    extraction["org"]["quote"] = "Ministry of Magic"
    assert run(FakeLlm(extract=[extraction])).status == "review"


def test_job_without_any_date_goes_to_review(extraction):
    extraction["lastDate"]["value"] = None
    extraction["importantDates"] = []
    assert run(FakeLlm(extract=[extraction])).status == "review"


def test_result_without_dates_is_fine(extraction):
    extraction.update(type="result", importantDates=[])
    extraction["lastDate"]["value"] = None
    assert run(FakeLlm(extract=[extraction])).status == "published"


def test_too_many_dropped_facts_goes_to_review(extraction):
    bad = copy.deepcopy(extraction)
    for r in bad["importantDates"]:
        r["date"] = "2027-01-01"
    for r in bad["fees"]:
        r["quote"] = "nothing"
    bad["vacancies"][0]["total"] = 1
    bad["salary"]["quote"] = "nope"
    bad["eligibility"]["quote"] = "nope"
    bad["ageRelaxation"]["quote"] = "nope"
    bad["totalPosts"]["value"] = 1
    out = run(FakeLlm(extract=[bad]))
    assert out.status == "review"
    assert "too many facts failed the checks" in out.reasons


def test_unknown_category_goes_to_review(extraction):
    extraction["category"] = "astronaut"
    assert run(FakeLlm(extract=[extraction])).status == "review"


def test_verifier_uses_the_stronger_model(extraction):
    llm = FakeLlm(extract=[extraction])
    run(llm)
    models = {s: m for s, _, m in llm.calls}
    assert models[jobs.VERIFY_SYSTEM] == jobs.llm_mod.VERIFY_MODEL


def test_prompt_contains_notice_links_and_shape(extraction):
    llm = FakeLlm(extract=[extraction])
    run(llm)
    prompt = llm.calls[0][1]
    assert "NOTICE TEXT:" in prompt and NOTICE_URL in prompt and '"relevant"' in prompt


def test_long_notices_are_truncated(extraction):
    llm = FakeLlm(extract=[extraction])
    run(llm, text=NOTICE + "x" * 200_000)
    assert len(llm.calls[0][1]) < jobs.MAX_SOURCE_CHARS + 10_000


def test_summary_has_only_index_keys(extraction):
    out = run(FakeLlm(extract=[extraction]))
    s = jobs.summary(out.post)
    assert set(s) == set(jobs.SUMMARY_KEYS)
    assert post_errors(s, full=False) == []


def test_html_notice_points_to_page_not_pdf(extraction):
    out = jobs.process_notice(FakeLlm(extract=[extraction]), SOURCE, notice_url="https://ssc.gov.in/notice/123",
                              link_text="x", text=NOTICE, links=LINKS, now=NOW)
    assert out.post["officialNoticeUrl"] is None and out.post["sourcePageUrl"] == "https://ssc.gov.in/notice/123"


def test_post_ids_are_stable_and_distinct():
    a = jobs.post_id(SOURCE, "SSC CGL 2026", "https://ssc.gov.in/a.pdf")
    assert a == jobs.post_id(SOURCE, "SSC CGL 2026", "https://ssc.gov.in/a.pdf")
    assert a != jobs.post_id(SOURCE, "SSC CGL 2026", "https://ssc.gov.in/b.pdf")


def test_states_default_to_source_states(extraction):
    extraction["states"] = {"value": [], "quote": ""}
    src = jobs.Source(id="bpsc", name="BPSC", url="https://bpsc.bihar.gov.in/", category="state_psc", states=("BR",))
    out = jobs.process_notice(FakeLlm(extract=[extraction]), src, notice_url=NOTICE_URL, link_text="x",
                              text=NOTICE, links=LINKS, now=NOW)
    assert out.post["states"] == ["BR"]


def test_other_category_takes_source_category(extraction):
    extraction["category"] = "other"
    out = run(FakeLlm(extract=[extraction]))
    assert out.post["category"] == "ssc"


def test_timestamps_are_utc_z(extraction):
    out = run(FakeLlm(extract=[extraction]))
    assert out.post["postedAt"] == "2026-09-12T06:00:00Z"


@pytest.mark.parametrize("days", [-9000, 900])
def test_dates_far_out_of_range_rejected(days, extraction):
    d = NOW.date() + dt.timedelta(days=days)
    text = NOTICE + f"\nSpecial date {d.day:02d}.{d.month:02d}.{d.year}\n"
    extraction["lastDate"] = {"value": d.isoformat(), "quote": f"Special date {d.day:02d}.{d.month:02d}.{d.year}"}
    assert "lastDate" in paths(jobs.check_facts(extraction, text, LINKS, today=NOW.date()))
