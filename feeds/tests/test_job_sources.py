"""Source-specific link extraction: SBI, DRDO, RRB, SSC, IBPS and BPSC pages."""

import json
import re
import ssl
from pathlib import Path

import pytest
import requests

from conftest import NOW
from feedbot import __main__ as main_mod
from feedbot import fetch
from feedbot.fetch import Page, html_links
from feedbot.sources import Source, is_candidate, link_id
from feedbot.store import Store

ROOT = Path(__file__).resolve().parents[1]
SBI = Source(id="sbi", name="SBI", url="https://sbi.bank.in/web/careers/current-openings", category="banking")
DRDO = Source(id="drdo", name="DRDO", url="https://drdo.gov.in/drdo/en/offerings/vacancies", category="psu")
RRB = Source(id="rrb", name="RRB", url="https://rrb.indianrailways.gov.in/getdata?loc=chandigarh&category=Notification",
             category="railway", extra={"warmup": "https://rrb.indianrailways.gov.in/chandigarh"})


def test_generic_document_labels_take_the_heading_above():
    html = """<div class="acc"><div class="head">RECRUITMENT OF SPECIALIST CADRE OFFICERS ADVT NO: CRPD/SCO/2026-27/24</div>
    <div class="body"><style>.x{color:red}</style><a href="/documents/1/2/FINAL+ADVT.pdf/abc-uuid?t=17">English</a>
    <a href="/documents/1/2/FINAL+ADVT+HINDI.pdf/def-uuid?t=17">Hindi (530 KB)</a></div></div>"""
    links = html_links(html, SBI.url)
    assert links[0][0] == "RECRUITMENT OF SPECIALIST CADRE OFFICERS ADVT NO: CRPD/SCO/2026-27/24 - English"
    assert all(is_candidate(t, u, SBI) for t, u in links)


def test_liferay_document_links_count_as_documents():
    url = "https://sbi.bank.in/documents/77530/57941334/FINAL+ADVT+OF+2026-27_24.pdf/0c1d-uuid?t=1759"
    assert is_candidate("ENGAGEMENT OF SPECIALIST CADRE OFFICER ON CONTRACT BASIS", url, SBI)
    assert link_id(url) == link_id(url.replace("t=1759", "t=1800")), "SBI re-uploads change only ?t="


def test_aria_label_titles_https_upgrade_and_pagers():
    html = """<a href="http://drdo.gov.in/drdo/en/offerings/vacancies/dmrl-jrf"
                  aria-label="View More DMRL invites application from candidates for the post of JRF">View More</a>
              <a href="/drdo/en/offerings/vacancies?page=1">Current page 1</a>
              <a href="/drdo/en/offerings/vacancies?page=2">Page 2</a>
              <a href="https://other.gov.in/x">Next</a>"""
    links = html_links(html, DRDO.url)
    assert links[0] == ("DMRL invites application from candidates for the post of JRF",
                        "https://drdo.gov.in/drdo/en/offerings/vacancies/dmrl-jrf")
    assert [t for t, u in links if is_candidate(t, u, DRDO)] == [links[0][0]]


def test_documents_offered_in_a_select_are_links():
    html = """<table><tr><td>1</td><td>06/2026</td><td>Detailed Centralised Employment Notification</td>
    <td><select onchange="opendoc(this.value)"><option value="">Select Language</option>
    <option value="/-/image/1791CEN_06_2026_NTPC_G.pdf/examsDocuments">English</option></select></td></tr></table>"""
    links = html_links(html, RRB.url)
    assert links == [("06/2026 - Detailed Centralised Employment Notification - English",
                      "https://rrb.indianrailways.gov.in/-/image/1791CEN_06_2026_NTPC_G.pdf/examsDocuments")]
    assert is_candidate(*links[0], RRB)


def test_ssc_notice_board_json():
    text = json.dumps({"data": [
        {"headline": "Tentative Vacancies of Junior Engineer Examination, 2026.", "redirectUrl": "",
         "attachments": [{"path": "NoticeBoards\\JE_Tentative_Vacancy_08102026.pdf"}]},
        {"headline": "CHSL 2025: Declaration of Final Result", "redirectUrl": "https://ssc.gov.in/x/result",
         "attachments": []}]})
    assert main_mod.ssc_links(text) == [
        ("Tentative Vacancies of Junior Engineer Examination, 2026.",
         "https://ssc.gov.in/api/attachment/NoticeBoards/JE_Tentative_Vacancy_08102026.pdf"),
        ("CHSL 2025: Declaration of Final Result", "https://ssc.gov.in/x/result")]


class SeqFetcher:
    def __init__(self, pages):
        self.pages, self.got = pages, []

    def get(self, url):
        self.got.append(url)
        return self.pages[url]


def test_warmup_page_is_fetched_first(tmp_path):
    f = SeqFetcher({RRB.extra["warmup"]: Page(RRB.extra["warmup"], 200, "text/html", b"<html></html>"),
                    RRB.url: Page(RRB.url, 200, "text/html", b"<html></html>")})
    status = {"sources": {}}
    main_mod.run_jobs(Store(tmp_path), None, f, [RRB], NOW, status)
    assert f.got == [RRB.extra["warmup"], RRB.url] and status["sources"]["rrb"]["ok"]


class FlakySession:
    def __init__(self, failures):
        self.failures, self.headers, self.calls = failures, {}, 0

    def get(self, url, **kw):
        self.calls += 1
        if self.calls <= self.failures:
            raise requests.exceptions.SSLError("[SSL: UNEXPECTED_MESSAGE] unexpected message")
        r = requests.Response()
        r.status_code, r.url, r._content = 200, url, b"ok"
        r.headers["content-type"] = "text/html"
        r.raw = None
        r.iter_content = lambda n: iter([b"ok"])
        return r


def test_tls_handshake_failures_get_more_tries(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    s = FlakySession(failures=3)
    assert fetch.Fetcher(session=s, retries=1, delay=0).get("https://www.ibps.in/").body == b"ok"
    assert s.calls == 4
    with pytest.raises(requests.RequestException):
        fetch.Fetcher(session=FlakySession(failures=fetch.SSL_ATTEMPTS), retries=1, delay=0).get("https://www.ibps.in/")


def test_intermediates_are_the_two_expected_cas_and_identical_for_the_relay():
    pipeline, relay = ROOT / "feedbot" / "intermediates.pem", ROOT / "relay" / "intermediates.pem"
    assert pipeline.read_bytes() == relay.read_bytes()
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pipeline.read_text(), re.S)
    ders = [ssl.PEM_cert_to_DER_cert(b) for b in blocks]
    assert len(ders) == 2


def test_ca_bundle_adds_intermediates_to_certifi():
    import certifi
    bundle = Path(fetch.ca_bundle()).read_text(encoding="utf-8")
    assert bundle.startswith(Path(certifi.where()).read_text(encoding="utf-8")[:200])
    assert bundle.count("BEGIN CERTIFICATE") == Path(certifi.where()).read_text().count("BEGIN CERTIFICATE") + 2
    assert fetch.Fetcher()._http.verify == fetch.ca_bundle()


def test_sources_have_no_removed_or_dead_entries():
    data = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    by_id = {j["id"]: j for j in data["jobs"]}
    assert "rbi" not in by_id and "army" not in by_id
    assert by_id["ssc"]["kind"] == "ssc_api" and by_id["rrb-cdg"]["warmup"].startswith("https://")
    assert by_id["sbi"]["url"].startswith("https://sbi.bank.in/")
