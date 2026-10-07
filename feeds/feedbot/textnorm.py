"""Text normalisation and fact matching used by every check.

A fact counts as supported only when its evidence quote is found in the
source text and every number or date in the fact is found in that quote.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
import unicodedata

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_SPACE = re.compile(r"\s+")
_DASHES = str.maketrans({"–": "-", "—": "-", "‒": "-", "−": "-", "‐": "-", "‑": "-"})
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def normalize(text: str | None) -> str:
    """Lower-cased text with unified digits, dashes, quotes and spaces."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = t.translate(_DEVANAGARI_DIGITS).translate(_DASHES).translate(_QUOTES)
    t = t.replace("​", "").replace(" ", " ")
    t = _SPACE.sub(" ", t).strip().lower()
    return t


def compact(text: str | None) -> str:
    """Normalised text without spaces or punctuation, for layout-proof matching."""
    return re.sub(r"[^0-9a-zऀ-ॿ]+", "", normalize(text))


def quote_in_source(quote: str | None, source: str, *, min_ratio: float = 0.9) -> bool:
    """True when `quote` appears in `source` (PDF line breaks and spacing ignored).

    Exact compact match first; otherwise the best fuzzy window must reach
    `min_ratio`, which tolerates OCR slips but not invented sentences.
    """
    q = compact(quote)
    if len(q) < 4:
        return False
    s = compact(source)
    if q in s:
        return True
    if len(q) > 400:
        return False
    best = 0.0
    step = max(1, len(q) // 25)
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(q)
    for start in range(0, max(1, len(s) - len(q) + 1), step):
        window = s[start:start + len(q)]
        matcher.set_seq1(window)
        if matcher.real_quick_ratio() < min_ratio or matcher.quick_ratio() < min_ratio:
            continue
        r = matcher.ratio()
        if r >= min_ratio:
            # OCR slips are tolerated in letters only, never in numbers.
            wide = s[max(0, start - 8):start + len(q) + 8]
            if re.sub(r"\D", "", q) in re.sub(r"\D", "", wide):
                return True
        best = max(best, r)
    return False


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str | None) -> list[str]:
    """Numbers in `text` as digit strings: '1,51,100' -> '151100', '2.5' kept."""
    out = []
    for m in _NUM.finditer(normalize(text)):
        n = m.group().replace(",", "").rstrip(".")
        if "." in n:
            whole, frac = n.split(".", 1)
            frac = frac.rstrip("0")
            n = whole.lstrip("0") or "0"
            if frac:
                n = f"{n}.{frac}"
        else:
            n = n.lstrip("0") or "0"
        out.append(n)
    return out


def number_supported(value: int | float | str, quote: str | None) -> bool:
    """True when the number `value` is written in `quote` (Indian or plain grouping)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    target = numbers_in(str(value))
    if not target:
        return False
    return target[0] in numbers_in(quote)


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    "जनवरी": 1, "फरवरी": 2, "फ़रवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5,
    "जून": 6, "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9,
    "अक्टूबर": 10, "अक्तूबर": 10, "नवंबर": 11, "नवम्बर": 11, "दिसंबर": 12, "दिसम्बर": 12,
}
_MONTH_ALT = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))
_DMY = re.compile(r"(?<!\d)(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4}|\d{2})(?!\d)")
_YMD = re.compile(r"(?<!\d)(\d{4})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})(?!\d)")
_D_MON_Y = re.compile(
    r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?[\s.,-]*(" + _MONTH_ALT + r")\.?[\s.,-]*(\d{4})(?!\d)")
_MON_D_Y = re.compile(
    r"(" + _MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)")


def _mk(y: int, m: int, d: int) -> dt.date | None:
    if y < 100:
        y += 2000
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def dates_in(text: str | None) -> list[dt.date]:
    """All calendar dates written in `text` (Indian day-first convention)."""
    t = normalize(text)
    found: list[dt.date] = []
    for m in _YMD.finditer(t):
        d = _mk(int(m[1]), int(m[2]), int(m[3]))
        if d:
            found.append(d)
    t2 = _YMD.sub(" ", t)
    for m in _DMY.finditer(t2):
        d = _mk(int(m[3]), int(m[2]), int(m[1]))
        if d:
            found.append(d)
    for m in _D_MON_Y.finditer(t):
        d = _mk(int(m[3]), _MONTHS[m[2]], int(m[1]))
        if d:
            found.append(d)
    for m in _MON_D_Y.finditer(t):
        d = _mk(int(m[3]), _MONTHS[m[1]], int(m[2]))
        if d:
            found.append(d)
    return found


def parse_iso_date(value: str | None) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def date_supported(value: str | None, quote: str | None) -> bool:
    """True when the ISO date `value` is written (in any common form) in `quote`."""
    d = parse_iso_date(value)
    return d is not None and d in dates_in(quote)


def digits_match(en: str | None, hi: str | None) -> bool:
    """English and Hindi versions must carry the same numbers (translation check)."""
    return sorted(numbers_in(en)) == sorted(numbers_in(hi))


def has_devanagari(text: str | None) -> bool:
    return bool(text) and any("ऀ" <= ch <= "ॿ" for ch in text)


def slugify(text: str, *, max_len: int = 60) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:max_len].strip("-") or "item"
