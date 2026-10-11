import datetime as dt

import pytest

from feedbot.textnorm import (compact, date_supported, dates_in, digits_match, has_devanagari,
                              normalize, number_supported, numbers_in, parse_iso_date,
                              quote_in_source, slugify)

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
HI_MONTHS = ["जनवरी", "फरवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]

SAMPLE_DATES = [dt.date(2026, m, d) for m in range(1, 13) for d in (1, 9, 15, 28)]


def forms(d: dt.date) -> list[str]:
    mon = MONTHS[d.month - 1]
    return [
        f"{d.day:02d}.{d.month:02d}.{d.year}",
        f"{d.day:02d}/{d.month:02d}/{d.year}",
        f"{d.day}-{d.month}-{d.year}",
        f"{d.day:02d}-{d.month:02d}-{d.year % 100:02d}",
        f"{d.year}-{d.month:02d}-{d.day:02d}",
        f"{d.day} {mon} {d.year}",
        f"{d.day}th {mon[:3]} {d.year}",
        f"{mon} {d.day}, {d.year}",
        f"{d.day} {HI_MONTHS[d.month - 1]} {d.year}",
    ]


@pytest.mark.parametrize("d", SAMPLE_DATES, ids=str)
@pytest.mark.parametrize("form", range(9))
def test_every_date_form_is_read(d, form):
    text = f"Last date: {forms(d)[form]} (11 PM)"
    assert d in dates_in(text)
    assert date_supported(d.isoformat(), text)


@pytest.mark.parametrize("d", SAMPLE_DATES, ids=str)
def test_wrong_day_is_not_supported(d):
    other = d + dt.timedelta(days=1)
    assert not date_supported(other.isoformat(), f"Closing date {d.day:02d}.{d.month:02d}.{d.year}")


@pytest.mark.parametrize("d", SAMPLE_DATES, ids=str)
def test_month_day_swap_is_not_accepted(d):
    if d.day > 12 or d.day == d.month:
        pytest.skip("not swappable")
    swapped = dt.date(d.year, d.day, d.month)
    assert not date_supported(swapped.isoformat(), f"on {d.day:02d}/{d.month:02d}/{d.year}")


@pytest.mark.parametrize("text", ["31.02.2026", "00.10.2026", "15.13.2026", "no date here", ""])
def test_impossible_dates_are_ignored(text):
    assert dates_in(text) == []


def test_devanagari_digits_in_dates():
    assert dt.date(2026, 10, 5) in dates_in("अंतिम तिथि: ०५.१०.२०२६")


@pytest.mark.parametrize("value,text", [
    ("2026-10-05", "05.10.2026"), ("2026-01-31", "31 January 2026"), ("2026-12-01", "1st Dec, 2026"),
])
def test_date_supported_positive(value, text):
    assert date_supported(value, text)


@pytest.mark.parametrize("value", [None, "", "05-10-2026", "2026/10/05", "tomorrow", 20261005])
def test_bad_iso_values(value):
    assert not date_supported(value, "05.10.2026")


@pytest.mark.parametrize("n", [1, 7, 12, 100, 999, 1000, 4200, 14582, 25500, 99999, 100000, 151100, 1234567])
@pytest.mark.parametrize("style", ["plain", "western", "indian", "rs", "hindi"])
def test_number_written_in_any_grouping(n, style):
    plain = str(n)
    western = f"{n:,}"
    s = plain
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        indian = ",".join(groups + [tail])
    else:
        indian = s
    text = {"plain": f"total {plain} posts", "western": f"total {western} posts",
            "indian": f"total {indian} posts", "rs": f"Rs. {indian}/-",
            "hindi": "कुल " + plain.translate(str.maketrans("0123456789", "०१२३४५६७८९")) + " पद"}[style]
    assert number_supported(n, text)
    assert not number_supported(n + 1, text)


@pytest.mark.parametrize("text,expected", [
    ("1,51,100", ["151100"]), ("25,500 to 81,100", ["25500", "81100"]), ("2.50 lakh", ["2.5"]),
    ("007", ["7"]), ("0", ["0"]), ("v1.0", ["1"]), ("", []), (None, []), ("१२३", ["123"]),
    ("Level-4 to Level-8", ["4", "8"]), ("10.", ["10"]), ("3.0", ["3"]),
])
def test_numbers_in(text, expected):
    assert numbers_in(text) == expected


def test_number_supported_handles_float_integers():
    assert number_supported(100.0, "Rs 100")
    assert not number_supported("abc", "Rs 100")


SOURCE = """Combined Graduate Level Examination, 2026
Last date for receipt of online applications: 05.10.2026 (23:00)
Application Fee: Rs 100/- (Rupees One Hundred only)."""


@pytest.mark.parametrize("quote", [
    "Last date for receipt of online applications: 05.10.2026 (23:00)",
    "last   date for receipt\nof online applications: 05.10.2026",
    "Application Fee: Rs 100/-",
    "APPLICATION FEE: RS 100/- (RUPEES ONE HUNDRED ONLY)",
    "Combined Graduate Level Examination, 2026",
])
def test_quotes_found_despite_layout(quote):
    assert quote_in_source(quote, SOURCE)


@pytest.mark.parametrize("quote", [
    "Last date for receipt of online applications: 15.10.2026",
    "Application Fee: Rs 500/-",
    "The examination is cancelled",
    "", None, "abc",
])
def test_invented_quotes_rejected(quote):
    assert not quote_in_source(quote, SOURCE)


def test_quote_with_small_ocr_slip_is_accepted():
    assert quote_in_source("Last date for receipt of onIine applications: 05.10.2026", SOURCE)


def test_long_unrelated_quote_rejected_quickly():
    assert not quote_in_source("x" * 500, SOURCE)


@pytest.mark.parametrize("en,hi,ok", [
    ("100 posts", "100 पद", True), ("100 posts", "१०० पद", True), ("Rs 25,500", "₹25500", True),
    ("100 posts", "1000 पद", False), ("18 to 32 years", "18 से 30 वर्ष", False), ("no numbers", "कोई नहीं", True),
    ("2 and 3", "3 और 2", True),
])
def test_digits_match(en, hi, ok):
    assert digits_match(en, hi) is ok


@pytest.mark.parametrize("text,ok", [("कर्मचारी", True), ("SSC", False), ("", False), (None, False), ("SSC भर्ती", True)])
def test_has_devanagari(text, ok):
    assert has_devanagari(text) is ok


@pytest.mark.parametrize("raw,expected", [
    ("  A B  ", "a b"), ("A–B", "a-b"), ("“x”", '"x"'), ("०१", "01"), (None, ""),
    ("Line\n\nbreak", "line break"), ("ﬁle", "file"),
])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_compact_strips_layout():
    assert compact("A-B c\nD!") == "abcd"


@pytest.mark.parametrize("raw,expected", [
    ("SSC CGL 2026 Recruitment", "ssc-cgl-2026-recruitment"), ("  ", "item"), ("एसएससी", "item"),
    ("A/B & C", "a-b-c"), ("x" * 100, "x" * 60),
])
def test_slugify(raw, expected):
    assert slugify(raw) == expected


@pytest.mark.parametrize("value,expected", [
    ("2026-10-05", dt.date(2026, 10, 5)), ("2026-10-05T10:00:00Z", dt.date(2026, 10, 5)),
    ("2026-02-30", None), (None, None), (5, None), ("", None),
])
def test_parse_iso_date(value, expected):
    assert parse_iso_date(value) == expected
