"""Reading a company's REPORTED order book out of a filing.

An order book is not the sum of the order wins on record. It is the value of
contracted work not yet executed:

    closing book = opening book + new orders - revenue executed

Bagged sees the "+ new orders" leg, and only the part companies choose to
announce, so summing stored orders would overstate the book of a company that
executes quickly and understate the book of one that announces only its
largest wins. The figure has to come from the company, which states it in:

  * order-win press releases  -- "the total unexecuted order book stands at
    approximately Rs.4,100 Crores";
  * results press releases    -- "As of June 30, 2026, the Company's unexecuted
    order book reached a record Rs. 6,630 crore";
  * investor presentations    -- a highlights slide, "Order book of Rs 19,455
    million as on 30 June 2026";
  * earnings call transcripts and monthly order-position updates.

This module finds those statements, reads the figure and the date it is stated
as of, and decides whether it describes the whole company or one segment.
Every figure keeps the sentence it came from, so the dashboard can show the
evidence beside the number.

Precision over recall. A figure is read only when it sits in the same sentence
as "order book" (or a synonym), with nothing between them that names another
quantity -- an inflow, a revenue line, a bid pipeline, a table of other
numbers. Targets ("expecting to build its order book to Rs 2,500 crore by
FY27") are not books and are skipped. Tables listing several quarters side by
side are skipped rather than guessed at.

Tuned against the 405 filings in the database that mention an order book; the
misreads that shaped each rule are noted beside it.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from ..config import CONFIG
from .money import normalize_rupee_glyphs

# --------------------------------------------------------------------------
# Anchors. "order booking" (Jash Engineering's monthly intake) and "booked"
# are inflow, not book, hence the lookaheads.
# --------------------------------------------------------------------------
ANCHOR = re.compile(
    r"\border[\s-]?books?\b(?!\s*(?:ing|ed)\b)"
    r"|\border\s+backlog\b"
    r"|\bunexecuted\s+(?:orders?|order\s+value|projects?\s+value)\b(?!\s*(?:book|backlog))"
    r"|\borders?\s+(?:in|on)\s+hand\b"
    r"|\boutstanding\s+orders\b"
    r"|\bpending\s+orders\b",
    re.IGNORECASE,
)

# Uses of the phrase that are not a company's order book.
NOT_A_BOOK = re.compile(
    r"international order book\s*\(iob\)|london stock exchange"
    r"|book[\s-]?building|book running|oversubscri|bids? received"
    r"|order[\s-]?book\s*(?:to|/)\s*(?:bill|revenue|sales)",   # a ratio
    re.IGNORECASE,
)

# A target or a forecast, not a book. Jupiter Wagons: "targeting Rs 1,000
# crore order book"; Brahmaputra: "expecting to build its order book to
# approximately Rs. 2,500 Crores by the close of the Financial Year 2026-27".
FORWARD = re.compile(
    r"\b(?:expect\w*|target\w*|aim(?:s|ing)?|plan(?:s|ning)?|aspir\w*|anticipat\w*|"
    r"envisag\w*|projected|guid\w+|outlook|will\s+(?:build|reach|touch|cross)|"
    r"would|potential|by\s+(?:the\s+)?(?:close|end)\s+of|by\s+fy)\b",
    re.IGNORECASE,
)

# A reduction is a change, not a level. Ceinsys: "the overall reduction in the
# value of the pending orders to be executed is Rs.112.42 Crores".
CHANGE_NOT_LEVEL = re.compile(
    r"\b(?:reduction|reduced|decrease\w*|cancell?\w*|short[\s-]?clos\w*|impact)\b",
    re.IGNORECASE)

# --------------------------------------------------------------------------
# Money. Narrower than money.find_amounts on purpose: a figure needs a
# currency marker or an Indian unit (crore / lakh), so "9.9 GW" (Emmvee),
# "24.6 million meters" (Adani Energy) and "3.1 GW" (Inox Wind) -- real
# order-book statements, all capacity rather than money -- are never rupees.
# --------------------------------------------------------------------------
_CUR = r"(?:₹|Rs\.?|INR|US\s?\$|USD|\$|EUR|€|`)"
_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT = (r"(?:lakh\s+crores?|crores?|crs?\b\.?|cr\b|lakhs?|lacs?|millions?|mn\b|m\b|"
         r"billions?|bn\b|b\b|k\b)")
MONEY = re.compile(
    rf"(?P<approx>(?:approximately|approx\.?|about|around|nearly|over|above|beyond|"
    rf"more than|close to|exceeds?|~)\s*)?"
    rf"(?P<cur>{_CUR})?\s*(?P<num>{_NUM})\s*(?P<plus>\+)?\s*(?P<unit>{_UNIT})?",
    re.IGNORECASE,
)
_MULT = {"crore": 1e7, "crores": 1e7, "cr": 1e7, "crs": 1e7,
         "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5,
         "lakh crore": 1e12, "lakh crores": 1e12,
         "million": 1e6, "millions": 1e6, "mn": 1e6, "m": 1e6,
         "billion": 1e9, "billions": 1e9, "bn": 1e9, "b": 1e9, "k": 1e3}
_INDIAN_UNITS = {"crore", "crores", "cr", "crs", "lakh", "lakhs", "lac", "lacs",
                 "lakh crore", "lakh crores"}

# Words between the anchor and a figure that mean the figure is something
# else. VA Tech Wabag: "Rs. 194 Bn order book in Q1 FY 2026-27 Historic High
# results with Consolidated Revenues at Rs. 8,868 Mn" -- the Rs 8,868 Mn is
# revenue. Ceigall: "Strengthens Order Book with Fifth Win of INR 274 Crore".
# Data Patterns: "Including orders received and negotiated as on date - INR
# 2,654 Cr" sits next to the real "Order Book as on June 30, 2026 - INR 928 Cr".
OTHER_QUANTITY = re.compile(
    r"\b(?:inflows?|intake|new\s+orders?|wins?|won|addition|added|adds|"
    r"orders?\s+(?:received|secured|negotiated)|negotiated|excluding\s+orders|"
    r"revenues?|turnover|ebitda|pat|profits?|margins?|income|debt|cash|capex|"
    r"guidance|pipeline|bids?|bidding|enquir\w*|tenders?|l-?1|"
    r"market\s+cap\w*|valuation|share\s+capital|net\s+worth|reduction|"
    # a different slice of the book: Taurian "73% of the total order book,
    # while export orders stood at Rs 22.75 crore"; Aastha "Order Book
    # Visibility: Falcon Yarns ... has secured confirmed orders aggregating to"
    r"while|whereas|of\s+which|out\s+of\s+which|comprising|representing|accounting|"
    r"secured|bagged|received)\b",
    re.IGNORECASE,
)
# "...Strengthens Order Book with Rs 375 Crores of New Orders": the figure is
# followed by what it actually measures.
FIGURE_IS_INFLOW = re.compile(
    r"^\s*(?:\(?[^)]{0,30}\)\s*)?(?:of|in|worth\s+of)?\s*"
    r"(?:new|fresh|additional)?\s*(?:orders?\b(?!\s*book)|order\s+(?:inflow|intake|wins?))",
    re.IGNORECASE,
)
# What follows a figure can say it is not the book: "Of the Rs 45 crore order
# book open on 1 April 2026, Rs 23 Crores is billed" (63 moons); or that it is a
# wider measure than one -- Data Patterns' "Rs 2,654 Crore (including the
# orders negotiated and pending receipt)" next to a Rs 928 Cr book.
FIGURE_IS_NOT_BOOK = re.compile(
    r"^\W{0,3}(?:crores?|cr\.?)?\s*(?:\(?\s*)?(?:(?:is|was|has\s+been|have\s+been)\s+)?"
    r"(?:billed|executed|completed|recognised|recognized|invoiced|delivered)\b"
    r"|^[^.;]{0,25}\bincluding\s+(?:the\s+)?(?:\w+\s+){0,3}(?:negotiat\w*|l-?1|pending\s+receipt)",
    re.IGNORECASE)

# The figure in front of the label is an order's value, not the book. INOX
# India: "secures multiple orders worth Rs 939 Cr Current order book boosted";
# Airfloa: "Adds Rs 180 crore order book".
LEADS_AS_ORDER_VALUE = re.compile(
    r"\b(?:worth|valued|value\s+of|orders?\s+of|secur\w*|bag\w*|wins?|won|adds?|added|"
    r"received|targeting|target\s+of(?:\s+a)?|reiterat\w*)\b[^.]{0,22}$",
    re.IGNORECASE)

# Scope. A segment, division or subsidiary book is real, but it is not the
# company's book. Immediately adjacent qualifiers ("the railway order book",
# "our nuclear order book", "EPC Order Book") mark a segment even when the
# sentence also says "total" (IC Electricals: "total unexecuted export order
# book"); looser ones only when nothing says total/consolidated.
ADJACENT_SEGMENT = re.compile(
    r"\b(?:division|segment|vertical|(?<!key\s)(?<!our\s)business|export|domestic|overseas|"
    r"international|transmission|smart\s+meter\w*|o&m|railway|nuclear|defen[cs]e|toll|bess|cng|omc|"
    r"epc|manufacturing|telecom|ict|energy|data\s+cent(?:er|re)|dg\s+sets?|"
    r"aftermarket|digital|advisory|ads)\b(?:\s+business)?(?:\s*\([^)]{0,12}\))?"
    r"[^\w]{0,3}(?:&\s*\w+\s*)?$",
    re.IGNORECASE)
SEGMENT_WORDS = re.compile(
    r"\b(?:division|segment|vertical|export|domestic|overseas|"
    r"transmission|smart\s+meter\w*|o&m)\b",
    re.IGNORECASE)
REGIONAL_AFTER = re.compile(
    r"^\s+(?:in|for)\s+(?!(?i:(?:the\s+)?(?:company|group|fiscal|fy|financial|year|quarter|"
    r"current|next|coming|period)\b))"
    r"(?:the\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b")
SUBSIDIARY = re.compile(r"\bsubsidiar(?:y|ies)\b", re.IGNORECASE)
# Jindal Saw: "UAE Entity has an order book of ~USD 195 million"
OTHER_ENTITY = re.compile(r"\b(?:entity|arm|jv|joint\s+venture)\b[^.]{0,25}$", re.IGNORECASE)
TOTAL_WORDS = re.compile(
    r"\b(?:total|overall|consolidated|group(?:'s|’s)?|company(?:'s|’s)|aggregate|entire)\b",
    re.IGNORECASE)

# Verbs that bind the anchor to the figure that follows it.
BINDING = re.compile(
    r"\b(?:stands?|stood|is|was|of|at|reached|reaches|crossed|crosses|"
    r"increased|grew|rose|touched|totals?|totalled|amounts?|aggregat\w*|"
    r"valued|worth|to|now|currently|approximately|record|robust|healthy|"
    r"strong|position|remains?|swells?|exceeds?|beyond)\b|[:–\-]",
    re.IGNORECASE)

# --------------------------------------------------------------------------
# Dates.
# --------------------------------------------------------------------------
_MON = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_PATTERNS = [
    # 30th June 2026 / 30-Jun-2026 / 30 June, 2026 / 30th Jun'26
    ("dmy", re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[\s\-/.,]*(?P<m>{_MON})[a-z]*\.?"
                       rf"[\s\-/.,’']*(?P<y>20\d{{2}}|\d{{2}})\b", re.I)),
    # June 30, 2026
    ("mdy", re.compile(rf"\b(?P<m>{_MON})[a-z]*\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+"
                       rf"(?P<y>20\d{{2}})\b", re.I)),
    # 30.06.2026 / 30/06/2026 / 30-06-2026
    ("num", re.compile(r"\b(?P<d>\d{1,2})[./-](?P<m>\d{1,2})[./-](?P<y>20\d{2})\b")),
    # Q1 FY27 / Q1FY2027 / Q2 FY 2026-27 / Q1’27 / Q4FY26
    ("qfy", re.compile(r"\bQ(?P<q>[1-4])\s*(?:FY\s*)?['’]?(?P<fy>20\d{2}(?:\s*[-–]\s*\d{2,4})?|\d{2})\b",
                       re.I)),
    # June 2026 / Jun'26 / June, 2026 / July,'26  -> month end
    ("my", re.compile(rf"\b(?P<m>{_MON})[a-z]*\.?[\s,’']+(?P<y>20\d{{2}}|\d{{2}})\b", re.I)),
    # July-end / month-end of July  -> the latest such month end before filing
    ("me", re.compile(rf"\b(?P<m>{_MON})[a-z]*[\s-]+end\b", re.I)),
]
AS_OF_CUE = re.compile(r"\b(?:as\s+(?:on|of|at)|ended|ending|till|until|upto|up\s+to|on|"
                       r"at\s+the\s+end\s+of|closed\s+the\s+quarter|for\s+the\s+(?:year|quarter))"
                       r"[\s:,]*$", re.I)
AS_OF_TODAY = re.compile(r"\bas\s+(?:on|of)\s+(?:date|today)\b|\btill\s+date\b|\bto\s+date\b"
                         r"|\bnow\b|\bcurrent(?:ly)?\b|\bpresent\b", re.I)
QUARTER_TALK = re.compile(r"\bquarter\b|\bQ[1-4]\b|\bH[12]\b", re.I)
YEAR_END_TALK = re.compile(r"\b(?:closed|end(?:ed)?)\s+(?:of\s+)?the\s+(?:financial\s+)?year\b"
                           r"|\bfor\s+the\s+year\b|\bat\s+the\s+end\s+of\s+fy", re.I)


@dataclass
class OrderBookFigure:
    value_inr: float
    currency: str = "INR"
    as_of: str | None = None          # YYYY-MM-DD
    as_of_basis: str = "filed"        # stated | quarter | year | filed
    is_approx: bool = False
    scope: str = "total"              # total | segment | subsidiary
    confidence: float = 0.5
    quote: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["notes"] = "; ".join(self.notes)
        return d


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------
_ABBREV = {"rs", "approx", "no", "nos", "mr", "ms", "mrs", "dr", "ltd", "pvt", "co",
           "inc", "st", "viz", "vs", "sr", "jr", "ref", "sec", "reg", "regn", "u.s",
           "e.g", "i.e", "etc", "fig", "corp", "bros", "govt", "dept", "est", "excl", "incl"}
# Sentence breaks: full stops (not after an abbreviation), bullets, the " || "
# the scanner joins headline/body/PDF with, blank lines, and the " o " that
# PDF text extraction leaves where a hollow bullet was (Ashoka Buildcon, Data
# Patterns).
_SENT_BREAK = re.compile(
    r"\.\s+(?=[A-Z“\"(₹\uf000-\uf8ff])|\.\s*\n|[•●▪■◆❖➢✓►\uf000-\uf8ff]|\s\|\|\s|\n\s*\n|;\s+"
    r"|\s\?\s(?=[A-Z])"
    r"|\s[o]\s(?=[A-Z])|\s›\s")


def _sentences(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in _SENT_BREAK.finditer(text):
        if m.group(0).startswith("."):
            word = re.search(r"([A-Za-z.]+)$", text[max(0, m.start() - 8):m.start()])
            if word and word.group(1).lower().strip(".") in _ABBREV:
                continue
        spans.append((start, m.start() + (1 if m.group(0).startswith(".") else 0)))
        start = m.end()
    spans.append((start, len(text)))
    return [(a, b) for a, b in spans if b > a]


def _prep(text: str) -> str:
    text = (text or "").replace("&#8377;", "₹").replace("&#x20B9;", "₹")
    text = text.replace("&amp;", "&").replace("\u00a0", " ")
    # A rupee sign lost to "?" in the text layer: "?5,100 Cr" (Bluspring)
    text = re.sub(r"(?<![\w?])\?\s?(?=\d[\d,]*(?:\.\d+)?\+?\s*(?:cr|crore|lakh|lac|mn|bn)\b)",
                  "₹", text, flags=re.I)
    # "Crosses₹1,214 Cr" (Innovision): a symbol glued to the word before it
    text = re.sub(r"(?<=[A-Za-z])(?=₹)", " ", text)
    text = normalize_rupee_glyphs(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def _money(m: re.Match) -> tuple[float, str] | None:
    """(value_inr, currency) for a MONEY match, or None if it is not money."""
    cur_tok = (m.group("cur") or "").strip()
    unit = re.sub(r"\s+", " ", (m.group("unit") or "").strip().lower().rstrip("."))
    if not cur_tok and unit not in _INDIAN_UNITS:
        return None                     # "9.9 GW", "24.6 million meters", "2026"
    raw = m.group("num").replace(",", "")
    if cur_tok and not unit and len(raw.split(".")[0]) < 6:
        return None                     # "Rs 2026": a year glued to a symbol
    try:
        num = float(raw)
    except ValueError:
        return None
    low = cur_tok.lower().replace(" ", "")
    currency = ("USD" if low in ("$", "us$", "usd") else
                "EUR" if low in ("€", "eur") else "INR")
    return num * _MULT.get(unit, 1.0) * CONFIG.fx_rates.get(currency, 1.0), currency


def _iter_money(s: str):
    for m in MONEY.finditer(s):
        if m.group("num"):
            val = _money(m)
            if val is not None:
                yield m, val


_FY_TOKEN = re.compile(r"\bFY\s*['’]?\d{2,4}(?:\s*[-–]\s*\d{2,4})?\b|\b20\d{2}(?:\s*[-–]\s*\d{2,4})?\b",
                       re.I)


def _strip_dates(s: str) -> str:
    """Remove dates, years and FY labels, so the digits left mean something."""
    for _, rx in DATE_PATTERNS:
        s = rx.sub(" ", s)
    return _FY_TOKEN.sub(" ", s)


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------
def _owned_by_figure_before(sent: str, date_start: int) -> bool:
    """Is the date glued to a figure in front of it -- "Rs 2,044 Crore as at
    31st March 2025"? Then it dates that figure, not a later one."""
    lo = max(0, date_start - 45)
    for mm, _ in _iter_money(sent[lo:date_start]):
        if re.fullmatch(r"[\s,]*(?:\(?\s*(?:as\s+(?:on|of|at)|on|at|till|upto)\s*)?[\s,:]*",
                        sent[lo + mm.end():date_start], re.I):
            return True
    return False


def _parse_date(kind: str, m: re.Match, filed: date) -> date | None:
    try:
        if kind == "qfy":
            q = int(m.group("q"))
            parts = re.split(r"\s*[-–]\s*", m.group("fy"))
            if len(parts) > 1:                        # "2026-27": FY ends in the later year
                last = parts[-1]
                end_year = int(last) if len(last) == 4 else int(parts[0][:2] + last[-2:])
            else:
                end_year = int(parts[0]) if len(parts[0]) == 4 else 2000 + int(parts[0][-2:])
            start = end_year - 1
            # Indian FY quarters: Q1 Apr-Jun, Q2 Jul-Sep, Q3 Oct-Dec, Q4 Jan-Mar
            return {1: date(start, 6, 30), 2: date(start, 9, 30),
                    3: date(start, 12, 31), 4: date(end_year, 3, 31)}[q]
        mo = (int(m.group("m")) if kind == "num"
              else _MONTHS[m.group("m")[:3].lower()])
        if kind == "me":
            y = filed.year if mo <= filed.month else filed.year - 1
            return date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)
        y = int(m.group("y"))
        y = y if y > 100 else 2000 + y
        d = int(m.group("d")) if kind in ("dmy", "mdy", "num") else 0
        if not d:
            return date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)
        return date(y, mo, d)
    except (ValueError, KeyError, TypeError, IndexError):
        return None


def _filed_date(filed_at) -> date:
    if isinstance(filed_at, datetime):
        return filed_at.date()
    if isinstance(filed_at, date):
        return filed_at
    if filed_at:
        try:
            return datetime.fromisoformat(str(filed_at)).date()
        except ValueError:
            pass
    return date.today()


def quarter_end_before(d: date) -> date:
    """The last quarter end strictly before `d`."""
    for y in (d.year, d.year - 1):
        for mo, dd in ((12, 31), (9, 30), (6, 30), (3, 31)):
            q = date(y, mo, dd)
            if q < d:
                return q
    return date(d.year - 1, 12, 31)


def fiscal_year_end_before(d: date) -> date:
    y = d.year if (d.month, d.day) > (3, 31) else d.year - 1
    return date(y, 3, 31)


def _as_of(sent: str, fig_start: int, fig_end: int, anchor_start: int,
           filed: date, kind: str) -> tuple[date, str]:
    """When the figure is stated as of.

    The date nearest the FIGURE wins, cued dates first: Vikran's "increased
    from approximately Rs.2,044 Crore as at 31st March 2025 to approximately
    Rs.5,206 Crore as at 31st March 2026" dates each figure by the date that
    follows it, not by the first date in the sentence.
    """
    found: list[tuple[float, date]] = []
    for name, rx in DATE_PATTERNS:
        for m in rx.finditer(sent):
            d = _parse_date(name, m, filed)
            if not d:
                continue
            # Never after the filing; and an uncued date long before it is a
            # comparison period ("vs Q1 FY26"), a project start, or a slide
            # header from another page.
            cued = bool(AS_OF_CUE.search(sent[max(0, m.start() - 24):m.start()]))
            if d > filed + timedelta(days=1):
                continue
            if d < filed - timedelta(days=550 if cued else 200):
                continue
            if m.start() >= fig_end:
                dist = m.start() - fig_end
            elif m.end() <= fig_start:
                dist = fig_start - m.end()
                # a date in front of the anchor ("As of June 30, 2026, the
                # Company's order book...") is as good as one after the figure
                if m.end() <= anchor_start:
                    dist = min(dist, anchor_start - m.end())
            else:
                dist = 0
            # Another figure between the date and ours? Then it belongs to that one.
            lo, hi = sorted((m.start(), fig_start if m.start() < fig_start else fig_end))
            if m.start() >= fig_end and any(True for _ in _iter_money(sent[fig_end:m.start()])):
                dist += 2000
            elif m.end() <= fig_start and (
                    any(True for _ in _iter_money(sent[m.end():fig_start]))
                    # "Rs 2,044 Crore as at 31st March 2025 to Rs 5,206 Crore":
                    # that date already belongs to the figure in front of it
                    or _owned_by_figure_before(sent, m.start())):
                dist += 2000
            found.append((dist - (300 if cued else 0), d))
    if found:
        found.sort(key=lambda t: t[0])
        score, d = found[0]
        if score < 400:
            # "Order Book at Rs 31,040 million  Thursday, August 13, 2026": an
            # uncued date on the filing day is the release's dateline. On a
            # results release or presentation the book is as of the quarter end.
            if (score > -100 and abs((d - filed).days) <= 2
                    and kind in ("results", "presentation", "transcript")):
                return quarter_end_before(filed), "quarter"
            return d, "stated"
    if AS_OF_TODAY.search(sent):
        return filed, "filed"
    if YEAR_END_TALK.search(sent) or kind == "annual":
        return fiscal_year_end_before(filed), "year"
    if kind in ("presentation", "results", "transcript") or QUARTER_TALK.search(sent):
        return quarter_end_before(filed), "quarter"
    return filed, "filed"


RESULTS_TEXT = re.compile(
    r"(?:financial\s+)?results?\s+for\s+the\s+(?:first|second|third|fourth|quarter|half|period|year)"
    r"|\bq[1-4]\s*(?:&\s*h[12]\s*)?fy\s*['’]?\d{2,4}\s+(?:results|performance|earnings)"
    r"|quarter\s+(?:and\s+(?:half\s+)?year\s+)?ended|earnings\s+(?:update|release)",
    re.IGNORECASE)


def classify_source(category: str | None, subcategory: str | None,
                    headline: str | None, is_order_like: bool = False,
                    text_head: str | None = None) -> str:
    """What kind of document this is. Drives the date fallback: a results
    release or presentation states the book as of the quarter end, an order
    win press release as of the day it is filed."""
    blob = " ".join(x for x in (category, subcategory, headline) if x).lower()
    if "transcript" in blob:
        return "transcript"
    if "annual report" in blob or (text_head and re.search(r"\bannual\s+report\b", text_head[:600], re.I)):
        return "annual"
    if "presentation" in blob:
        return "presentation"
    if re.search(r"order\s*book|order position|unexecuted", blob):
        return "update"
    if re.search(r"result|quarter|q[1-4]\s*fy|financial performance|earnings", blob):
        return "results"
    if text_head and RESULTS_TEXT.search(text_head):
        return "results"
    if is_order_like:
        return "order_win"
    if "press" in blob or "media" in blob:
        return "press_release"
    return "other"


# --------------------------------------------------------------------------
# the extractor
# --------------------------------------------------------------------------
def find_order_books(text: str | None, filed_at=None, kind: str = "other") -> list[OrderBookFigure]:
    """Every order-book figure stated in `text`, best first."""
    if not text:
        return []
    text = _prep(text)
    filed = _filed_date(filed_at)
    out: dict[tuple[int, str], OrderBookFigure] = {}

    for s_start, s_end in _sentences(text):
        sent = text[s_start:min(s_end, s_start + 1600)]
        for a in ANCHOR.finditer(sent):
            ctx = sent[max(0, a.start() - 40):a.end() + 60]
            if NOT_A_BOOK.search(ctx):
                continue
            pre = sent[max(0, a.start() - 80):a.start()]
            if FORWARD.search(pre) or CHANGE_NOT_LEVEL.search(pre[-50:]):
                continue
            reads = _read_after(sent, a) or _read_before(sent, a)
            for value, currency, approx, conf, (f0, f1), notes in reads:
                if not (1e6 <= value <= 2e13):
                    continue
                mid = sent[a.end():f0] if f0 > a.end() else ""
                if FORWARD.search(mid):
                    continue
                scope, conf = _scope(sent, a, pre, mid, conf, notes)
                as_of, basis = _as_of(sent, f0, f1, a.start(), filed, kind)
                if basis == "stated":
                    conf += 0.08
                elif basis in ("quarter", "year"):
                    notes.append("date inferred: "
                                 + ("last quarter end" if basis == "quarter" else "last year end")
                                 + " before the filing")
                if approx:
                    notes.append("stated as approximate or a lower bound")
                q0, q1 = min(f0, a.start()), max(f1, a.end())
                lo, hi = max(0, q0 - 70), min(len(sent), q1 + 70)
                quote = sent[lo:hi]
                # start and end on whole words
                if lo > 0 and " " in quote[:20]:
                    quote = quote[quote.index(" ") + 1:]
                if hi < len(sent) and " " in quote[-20:]:
                    quote = quote[:quote.rindex(" ")] + " …"
                quote = re.sub(r"\s+", " ", quote).strip()
                fig = OrderBookFigure(
                    value_inr=value, currency=currency, as_of=as_of.isoformat(),
                    as_of_basis=basis, is_approx=approx, scope=scope,
                    confidence=round(max(0.05, min(0.97, conf)), 3),
                    quote=quote[:340], notes=notes)
                key = (round(value / 1e5), as_of.isoformat())
                if key not in out or out[key].confidence < fig.confidence:
                    out[key] = fig

    figs = list(out.values())
    figs.sort(key=lambda f: (f.scope == "total", f.confidence, f.as_of or "", f.value_inr),
              reverse=True)
    return figs


def _scope(sent, a, pre, mid, conf, notes):
    scope = "total"
    if SUBSIDIARY.search(sent[:a.end()]) and not re.search(
            r"(?:including|with|along\s+with)\s+(?:its\s+)?subsidiar|consolidated", sent, re.I):
        scope = "subsidiary"
    elif OTHER_ENTITY.search(pre[-45:]):
        scope = "subsidiary"
    elif ADJACENT_SEGMENT.search(pre[-28:]) or re.match(
            r"\s*(?:of|for|in)\s+(?:the|its|our)?\s*(?:company['’]s\s+)?[\w&\s]{0,30}?"
            r"\b(?:business|division|segment|vertical)\b", mid, re.I):
        scope = "segment"
    elif (SEGMENT_WORDS.search(pre[-45:]) or SEGMENT_WORDS.search(mid)) and not \
            re.search(r"\b(?:total|overall|consolidated)\b", pre[-45:] + mid, re.I):
        scope = "segment"
    elif REGIONAL_AFTER.match(sent[a.end():a.end() + 40]):
        scope = "segment"
    if scope == "total" and TOTAL_WORDS.search(pre[-45:] + " " + a.group(0) + " " + mid):
        conf += 0.08
    elif scope != "total":
        conf -= 0.2
        notes.append(f"{scope}-level book, not the whole company")
    return scope, conf


def _read_after(sent: str, a: re.Match) -> list:
    window = sent[a.end():a.end() + 190]
    hits = list(_iter_money(window))
    if not hits:
        return []
    # "...increased from Rs 550 crore to nearly Rs 560+ crore"; "taking its
    # order book to Rs 92 crore": a figure introduced by "to" after a change
    # verb is the closing book. The opening figure is kept too when the
    # sentence dates it -- a free point of history.
    changed = re.search(r"\b(?:from|increas\w*|grew|grow\w*|rose|rising|surg\w*|taking|takes|took|"
                        r"went\s+up|moved\s+up|strengthened|swell\w*|expand\w*|jump\w*)\b",
                        window, re.I)
    if changed:
        for i, (m, val) in enumerate(hits):
            lead = window[max(0, m.start() - 28):m.start()]
            if re.search(r"\b(?:compared|relative|as\s+against|against|versus|vs\.?)\s+(?:to\s+)?"
                         r"(?:\w+\s+)?$", lead, re.I):
                continue          # Triveni: "Rs 21.80 bn ... as compared to Rs 20.74 bn a year ago"
            if re.search(r"\bto\s+(?:a\s+record\s+(?:of\s+)?|approximately\s+|approx\.?\s+|"
                         r"about\s+|nearly\s+|over\s+|around\s+|~\s*)?$", lead, re.I):
                if OTHER_QUANTITY.search(_strip_dates(window[:changed.start()])):
                    break
                if FIGURE_IS_NOT_BOOK.search(window[m.end():m.end() + 70]):
                    break
                closing = _prefer_rupee(hits, i, _accept(m, val, a.end(), 0.82, lead), a.end(), lead)
                reads = [closing]
                if i > 0 and re.search(r"\bfrom\b", window[:hits[i - 1][0].start()], re.I):
                    pm, pval = hits[i - 1]
                    opening = _accept(pm, pval, a.end(), 0.7, window[:pm.start()])
                    opening[5].append("opening figure of a from/to statement")
                    reads.append(opening)
                return reads
    for idx, (m, val) in enumerate(hits):
        between = window[:m.start()]
        if len(between) > 130:
            break
        plain = _strip_dates(between)
        if ANCHOR.search(between):
            return []           # a nearer "order book" owns this figure
        if OTHER_QUANTITY.search(plain):
            return []
        # A bare number between the label and the figure means a table row
        # ("Total Order Book 15,251* 100.0% *Excluding orders received ... of
        # Rs. 451 crores", Ashoka Buildcon). Dates were stripped above.
        if re.search(r"\d", plain):
            return []
        if FIGURE_IS_INFLOW.match(window[m.end():m.end() + 40]):
            continue
        if FIGURE_IS_NOT_BOOK.search(window[m.end():m.end() + 70]):
            return []
        # A run of rupee figures is a table row: "Order Book (period) Rs 45 Cr
        # open Rs 288 Cr Rs 400 Cr". A parenthetical conversion is not.
        if idx + 1 < len(hits):
            nm, nval = hits[idx + 1]
            gap = window[m.end():nm.start()]
            words = re.findall(r"[A-Za-z]+", gap)
            if (val[1] == nval[1] == "INR" and len(gap) < 18 and "(" not in gap
                    and len(words) <= 1 and not (words and words[0].lower() in _CONNECTIVES)):
                return []
        if re.search(r"\bwith\b", between, re.I) and not BINDING.search(between):
            continue
        conf = 0.72 if BINDING.search(between) else 0.55
        if len(between) > 70:
            conf -= 0.25
        elif len(between) > 40:
            conf -= 0.15
        return [_prefer_rupee(hits, idx, _accept(m, val, a.end(), conf, between), a.end(), between)]
    return []


_CONNECTIVES = {"comprising", "of", "with", "and", "including", "against", "vs", "versus",
                "increasing", "up", "from", "to", "which", "where"}


def _prefer_rupee(hits, idx, fig, offset, lead):
    """"USD 4.4 billion (~Rs 42,100 Crore)": the company's own rupee figure
    beats our conversion at a configured FX rate."""
    m, val = hits[idx]
    if val[1] != "INR" and idx + 1 < len(hits):
        nm, nval = hits[idx + 1]
        if nval[1] == "INR" and nm.start() - m.end() < 25:
            value, _, _, conf, span, _ = _accept(nm, nval, offset, fig[3], lead)
            return (value, "INR", True, conf, span,
                    ["rupee figure as stated beside the foreign-currency one"])
    return fig


def _read_before(sent: str, a: re.Match) -> list:
    """Slide and tile layouts: 'Rs. 11,338 Cr Order book (As of May 25, 2026)'."""
    lo = max(0, a.start() - 60)
    window = sent[lo:a.start()]
    hits = list(_iter_money(window))
    if not hits:
        return []
    m, val = hits[-1]
    if len(hits) > 1:
        pm, pval = hits[-2]
        words = re.findall(r"[A-Za-z]+", window[pm.end():m.start()])
        if m.start() - pm.end() < 18 and len(words) <= 1:
            return []            # "Rs 7,634 Cr Rs 11,000+ Cr Order book": a table row
    gap = window[m.end():]
    if not re.fullmatch(r"[\s:\-–(]*(?:(?:total|consolidated|unexecuted|outstanding|current|"
                        r"healthy|robust|strong|record|executable|open|confirmed)\s+)*", gap, re.I):
        return []
    if LEADS_AS_ORDER_VALUE.search(sent[max(0, lo + m.start() - 40):lo + m.start()]):
        return []
    if FIGURE_IS_NOT_BOOK.search(sent[a.end():a.end() + 70]):
        return []
    fig = _accept(m, val, lo, 0.5, "")
    fig[5].append("figure printed before the label (slide layout)")
    return [fig]


def _accept(m: re.Match, val: tuple[float, str], offset: int, conf: float, lead: str):
    value, currency = val
    approx = bool(m.group("approx") or m.group("plus")) or bool(
        re.search(r"(?:approximately|approx|about|around|nearly|over|above|beyond|crosse?s|"
                  r"more than|close to|exceeds?|~)\s*$", lead or "", re.I))
    notes: list[str] = []
    if currency != "INR":
        notes.append(f"converted from {currency} at the configured FX rate")
    return value, currency, approx, conf, (offset + m.start(), offset + m.end()), notes


def best_order_book(text: str | None, filed_at=None, kind: str = "other",
                    min_confidence: float = 0.45) -> OrderBookFigure | None:
    """The single figure most likely to be the company's whole order book."""
    figs = [f for f in find_order_books(text, filed_at, kind) if f.confidence >= min_confidence]
    return figs[0] if figs else None


def plausibility(fig_value: float, scope: str, annual_revenue_inr: float | None,
                 order_value_inr: float | None = None) -> list[str]:
    """Reasons to distrust a figure, given what else is known. Empty = fine."""
    why = []
    if order_value_inr and scope == "total" and fig_value < order_value_inr * 0.98:
        why.append("smaller than the order announced in the same filing")
    if annual_revenue_inr and annual_revenue_inr > 0:
        ratio = fig_value / annual_revenue_inr
        if ratio > 40:
            why.append(f"{ratio:.0f}x annual revenue: likely a unit error")
        elif ratio < 0.02:
            why.append(f"{ratio:.3f}x annual revenue: likely not the whole book")
    return why
