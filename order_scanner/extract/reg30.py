"""The Regulation 30 order-disclosure table, read from the PDF itself.

Almost every "Bagging/Receiving of orders" filing carries the same SEBI table:

    Name of the entity awarding the order(s)/contract(s)       CSTE Con, Eastern Railway
    Significant terms and conditions ... in brief              Commissioning of ...
    Whether ... awarded by domestic/international entity        Domestic
    Nature of order(s)/contract(s)                              Signalling works
    Time period by which the order(s)/contract(s) is executed  12 months from LOA
    Broad consideration or size of the order(s)/contract(s)    Rs 85.53 Crore incl. GST
    Whether the promoter/promoter group ... have any interest   No
    Whether ... would fall within related party transactions    No

It answers exactly the questions the scorer needs. But flattened to plain text
the two columns interleave ("Name of the entity awarding the Ingka Centres
Private Limited order(s)/contract(s)"), so the free-text regexes missed the
customer in half the filings, the period in more than half, and read the
related-party QUESTION as a related-party answer on 50 of 65 orders.

So the table is read structurally, from the PDF:

  1. pdfplumber's ruled-table extraction -- clean label/answer cells for most
     filings;
  2. word positions, for tables drawn without rules (or whose cells come out
     scrambled): words left of the answer column's x position form the label,
     words right of it the answer, row by row.
"""
from __future__ import annotations

import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- fields
# Checked in this order; the first matching label claims a row.
_FIELDS: list[tuple[str, re.Pattern]] = [
    ("customer", re.compile(r"name\s*(?:\(\s*s\s*\))?\s+of\s+the\s+entit", re.I)),
    ("terms", re.compile(r"significant\s+terms", re.I)),
    ("period", re.compile(r"time\s*period|period\s+by\s+which", re.I)),
    ("size", re.compile(r"broad\s+consideration|size\s+of\s+the\s+order|value\s+of\s+the\s+order", re.I)),
    ("related_party", re.compile(r"related\s+party", re.I)),
    ("promoter_interest", re.compile(r"promoter", re.I)),
    ("domestic", re.compile(r"domestic\s*(?:/|or)\s*international", re.I)),
    ("nature", re.compile(r"nature\s+of", re.I)),
    ("other", re.compile(r"any\s+other", re.I)),
]
# A line that opens a new row of the table.
_ROW_START = re.compile(
    r"^\s*(?:\(?(?:\d{1,2}|[a-i])\s*[.)|:]?\s*)?\|?\s*"
    r"(?:name|significant|whether|nature|time|broad|size|any\s+other|details|particulars)\b",
    re.I)
# Some tables repeat the question inside the answer cell:
#   "Name of the entity awarding the order(s)/contract(s) Hindalco Industries Limited"
_QUESTION_PREFIX = re.compile(
    r"^.*?(?:order\s*\(\s*s\s*\)\s*/?\s*contract\s*\(\s*s\s*\)|contracts?\b)\s*[;:,.?]?\s*", re.I)


@dataclass
class Reg30:
    customer: str | None = None
    customer_is_awardee: bool = False   # the label asked "to which" it was awarded
    terms: str | None = None
    nature: str | None = None
    domestic: str | None = None
    period: str | None = None
    size: str | None = None
    promoter_interest: str | None = None
    related_party: str | None = None
    method: str = ""
    labels: dict = field(default_factory=dict)

    @property
    def n_fields(self) -> int:
        return sum(1 for k in ("customer", "terms", "nature", "domestic", "period",
                               "size", "promoter_interest", "related_party")
                   if getattr(self, k))


def _squash(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _clean_value(s: str) -> str:
    s = _squash(s).replace("|", " ")
    s = re.sub(r"^\s*[:;\-–]\s*", "", s)
    s = _squash(s)
    if any(rx.search(s[:80]) for _, rx in _FIELDS):
        s = _QUESTION_PREFIX.sub("", s, count=1)
    return _squash(s).strip(" ;:")


def _claim(out: Reg30, label: str, value: str, method: str) -> None:
    label, value = _squash(label), _clean_value(value)
    if not value or any(rx.search(value[:80]) for _, rx in _FIELDS):
        return      # empty, or still just the question: leave it for the next strategy
    for name, rx in _FIELDS:
        if rx.search(label):
            if name == "other" or getattr(out, name):
                return
            setattr(out, name, value)
            out.labels[name] = label[:120]
            if name == "customer" and re.search(r"\bto\s+which\b", label, re.I):
                out.customer_is_awardee = True
            if not out.method:
                out.method = method
            return


# ---------------------------------------------------------------- 1. ruled tables
def _from_tables(pdf, out: Reg30) -> None:
    for page in pdf.pages[:8]:
        try:
            tables = page.extract_tables()
        except Exception as exc:
            log.debug("table extraction failed: %s", exc)
            continue
        for table in tables:
            for row in table:
                cells = [_squash(c) for c in row]
                li = next((i for i, c in enumerate(cells)
                           if any(rx.search(c) for _, rx in _FIELDS)), None)
                if li is None:
                    continue
                answer = " ".join(c for c in cells[li + 1:] if c and c not in (":", "-", "|"))
                _claim(out, cells[li], answer, "table")


# ---------------------------------------------------------------- 2. word positions
def _lines(page) -> list[list[dict]]:
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2)
    words.sort(key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    for w in words:
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= 3:
            lines[-1].append(w)
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
    return lines


def _from_words(pdf, out: Reg30) -> None:
    for page in pdf.pages[:8]:
        lines = _lines(page)
        texts = [" ".join(w["text"] for w in ln) for ln in lines]
        start = next((i for i, t in enumerate(texts)
                      if re.search(r"name\s+of\s+the\s+entit|broad\s+consideration|particulars",
                                   t, re.I)), None)
        if start is None:
            continue
        region = lines[start:]
        # The answer column starts at the x where, line after line, text resumes
        # after a gutter (or where continuation lines begin).
        votes: Counter = Counter()
        for ln in region:
            if ln[0]["x0"] > page.width * 0.28:
                votes[round(ln[0]["x0"] / 6) * 6] += 1
            for a, b in zip(ln, ln[1:]):
                if b["x0"] - a["x1"] >= 6 and b["x0"] > page.width * 0.28:
                    votes[round(b["x0"] / 6) * 6] += 1
        if not votes:
            continue
        col_x, n = votes.most_common(1)[0]
        if n < 3:
            continue
        rows: list[list[str]] = []
        for ln in region:
            left = " ".join(w["text"] for w in ln if (w["x0"] + w["x1"]) / 2 < col_x - 3)
            right = " ".join(w["text"] for w in ln if (w["x0"] + w["x1"]) / 2 >= col_x - 3)
            if _ROW_START.search(left) or not rows:
                rows.append([left, right])
            else:
                rows[-1][0] += " " + left
                rows[-1][1] += " " + right
        for label, answer in rows:
            _claim(out, label, answer, "words")


def parse_reg30(pdf_bytes: bytes | None) -> Reg30 | None:
    """The disclosure table's answers, or None when the PDF has no such table."""
    if not pdf_bytes:
        return None
    try:
        import pdfplumber
    except ImportError:
        return None
    out = Reg30()
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            _from_tables(pdf, out)
            if out.n_fields < 4:
                # scrambled or unruled: fill the gaps from word positions
                _from_words(pdf, out)
    except Exception as exc:
        log.debug("reg30 parse failed: %s", exc)
        return None
    return out if out.n_fields >= 2 else None


# ---------------------------------------------------------------- answers
_NO = re.compile(r"^(?:no\b|none\b|nil\b|n\.?\s?a\.?(?:\s|$|\b)|not\s+applicable|not\s+a\s+related|"
                 r"does\s+not|do\s+not|not\s+related|neither|nothing)", re.I)
_YES = re.compile(r"^yes\b", re.I)


def yes_no(answer: str | None) -> bool | None:
    """True for a 'Yes' answer, False for No / None / NA, None when unclear."""
    a = _squash(answer).lstrip("-–:;. ")
    if not a:
        return None
    if _YES.match(a):
        return True
    if _NO.match(a):
        return False
    return None


_UNNAMED = re.compile(
    r"^(?:not\s+disclosed|confidential|undisclosed|n\.?\s?a\b|nil\b|none\b|not\s+applicable|"
    r"domestic\b|international\b|various\b|multiple\b|as\s+per\b|name\s+of\s+the\s+entity\s+is\s+not)",
    re.I)


def clean_customer(answer: str | None) -> str | None:
    """A usable counterparty name from the table's answer, or None."""
    a = _squash(answer).strip(" \"'“”‘’.,;:")
    if not a or _UNNAMED.match(a):
        return None
    # "One of the world's leading Cable manufacturing companies ... Name of the
    # entity is not specified" -> keep the description, drop the disclaimer.
    a = re.split(r"\.\s+(?=[A-Z])", a)[0]
    a = re.sub(r"\s*\((?:the\s+)?[\"“]?(?:authority|client|customer|employer)[\"”]?\)\s*$", "", a, flags=re.I)
    return a[:120].strip(" .,;\"'“”") or None


# ---------------------------------------------------------------- period
_MONTH_NAMES = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def _as_date(filed_at) -> date:
    if isinstance(filed_at, datetime):
        return filed_at.date()
    if isinstance(filed_at, date):
        return filed_at
    try:
        return datetime.fromisoformat(str(filed_at)).date()
    except (TypeError, ValueError):
        return date.today()


def _end_date(text: str) -> date | None:
    t = text.lower()
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d)
        except ValueError:
            pass
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON},?\s+(20\d{{2}})", t)
    if m:
        try:
            return date(int(m.group(3)), _MONTH_NAMES[m.group(2)], int(m.group(1)))
        except ValueError:
            pass
    m = re.search(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})", t)
    if m:
        try:
            return date(int(m.group(3)), _MONTH_NAMES[m.group(1)], int(m.group(2)))
        except ValueError:
            pass
    m = re.search(rf"\b{_MON},?\s+(20\d{{2}})", t)
    if m:
        return date(int(m.group(2)), _MONTH_NAMES[m.group(1)], 28)
    return None


def parse_period(answer: str | None, filed_at=None) -> float | None:
    """Execution period in months from the table's time-period answer.

    Handles durations ("12 months from LOA", "One (01) Year", "Approximately
    10 Weeks"), ranges ("Within 6 - 9 Months" -> 9), an execution term inside a
    longer one ("48 Months (24 Months Execution and 24 months Warranty)" -> 24)
    and completion dates ("By 30-09-2027", "on or before 31st December 2026"),
    measured from the filing date.
    """
    from .rules import _DUR_BARE, _to_months

    t = _squash(answer)
    if not t:
        return None
    t = re.sub(r"\(\s*\d{1,3}\s*\)", " ", t)                 # "One (01) Year"
    t = re.sub(r"\bmo(?:ths?|nts?)\b", "months", t, flags=re.I)  # "3 Moths"
    m = re.search(r"(\d{1,3})\s*months?\s*(?:of\s+)?execution", t, re.I)
    if m:
        return _to_months(m.group(1), "months")
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d{1,3}(?:\.\d+)?)\s*"
                  r"(months?|years?|weeks?|days?)\b", t, re.I)
    if m:
        return _to_months(m.group(2), m.group(3))
    for dm in _DUR_BARE.finditer(t):
        months = _to_months(dm.group("n"), dm.group("u"))
        if months is not None:
            return months
    end = _end_date(t)
    if end:
        months = (end - _as_date(filed_at)).days / 30.44
        if 0.2 <= months <= 120:
            return round(months, 1)
    return None


def parse_size(answer: str | None):
    """The order value from the table's size answer, as a money.Amount.

    The general value picker treats a figure next to "GST" as suspect, which is
    right in free text but wrong here: this cell IS the order size, and
    "Rs 85.53 Crore including GST (Rs 72.48 Crore excluding GST)" is the norm.
    So when the picker declines, the first plausible figure in the cell is used.
    """
    from .money import Amount, find_amounts, pick_order_value

    t = _squash(answer)
    if not t:
        return None
    t = re.sub(r"\bR\s+s\s*\.", "Rs.", t)                   # "R s. 6,91,22,294.10"
    amt = pick_order_value(t)
    if amt is None:
        amt = next((a for a in find_amounts(t) if 1e5 <= a.value_inr <= 2e12), None)
    if amt is None:
        m = re.search(r"\b\d{1,3}(?:,\d{2,3}){2,}(?:\.\d+)?\b", t)  # a bare Indian-grouped figure
        if m:
            v = float(m.group(0).replace(",", ""))
            if v >= 1e5:
                amt = Amount(value_inr=v, currency="INR", raw=m.group(0),
                             start=m.start(), end=m.end(), confidence=0.7)
    if amt is not None:
        amt.confidence = max(amt.confidence, 0.85)
    return amt
