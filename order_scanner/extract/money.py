"""Parsing Indian money expressions out of free text.

Handles: Rs. 125.50 Crore | INR 1,25,50,000 | ₹ 1255 Lakhs | USD 15 Mn |
Rs 100-120 crore | Rs. 1,234.56 Lakhs (excluding GST) | 125 crore rupees
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import CONFIG

MULTIPLIERS = {
    "crore": 1e7, "crores": 1e7, "cr": 1e7, "crs": 1e7, "cr.": 1e7,
    "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "lakhs.": 1e5,
    "million": 1e6, "millions": 1e6, "mn": 1e6, "mio": 1e6, "m": 1e6,
    "billion": 1e9, "billions": 1e9, "bn": 1e9, "b": 1e9,
    "thousand": 1e3, "k": 1e3,
    "arab": 1e9,
}

CURRENCY_SYMBOLS = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupees": "INR",
    "rupee": "INR", "re.": "INR",
    "$": "USD", "us$": "USD", "usd": "USD", "us $": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP",
}

_CUR = r"(?:₹|Rs\.?|INR|US\s?\$|USD|\$|EUR|€|GBP|£)"
_NUM = r"\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT = (
    r"(?:crores?|crs?\.?|lakhs?|lacs?|millions?|mn\.?|mio|billions?|bn\.?|"
    r"thousand|arab)"
)

# Main pattern: optional currency, number, optional unit, optional trailing currency word.
AMOUNT_RE = re.compile(
    rf"(?P<cur>{_CUR})?\s*"
    rf"(?P<num>{_NUM})\s*"
    rf"(?P<unit>{_UNIT})?"
    rf"(?:\s*(?P<cur2>rupees?|INR))?",
    re.IGNORECASE,
)

# "Rs 100 - 120 crore"  /  "between Rs 50 crore and Rs 60 crore"
RANGE_RE = re.compile(
    rf"(?P<cur>{_CUR})?\s*(?P<lo>{_NUM})\s*(?P<lounit>{_UNIT})?\s*"
    rf"(?:-|–|—|to|and)\s*"
    rf"(?P<cur2>{_CUR})?\s*(?P<hi>{_NUM})\s*(?P<hiunit>{_UNIT})",
    re.IGNORECASE,
)

# Phrases that mean "this number is the order value".
VALUE_CUES = [
    "order value", "contract value", "value of the order", "value of the contract",
    "total value", "aggregate value", "aggregating to", "amounting to",
    "worth", "valued at", "for a value of", "order worth", "contract worth",
    "order of", "consideration of", "project cost", "total order value",
    "estimated value", "approximate value", "order size", "tender value",
    "deal value", "contract price", "value (excluding", "value (including",
    "order aggregating", "of the order is", "order amount",
    # BSE Regulation 30 table labels, which read backwards from the usual
    # phrasing and so were not covered by the cues above.
    "size of the order", "size of the contract", "value of the work order",
    "total consideration", "order value (in", "value (in rs",
    "monetary value", "approximate monetary value",
]

# Phrases whose nearby numbers are NOT the order value.
VALUE_ANTI_CUES = [
    "paid-up", "paid up capital", "face value", "share capital",
    "authorised capital", "authorized capital", "net worth", "turnover",
    "gst", "goods and services tax", "emd", "earnest money",
    "security deposit", "bank guarantee", "performance guarantee",
    "retention money", "penalty", "liquidated damages", "per share",
    "per equity share", "dividend", "fine of", "fees of", "stamp duty",
    "market capitalisation", "market capitalization", "revenue for the",
    "profit", "borrowing", "loan of", "credit facility", "equity shares of",
    "nominal value", "premium of", "warrant", "isin", "cin",
    "registered office", "pin", "phone", "telephone", "tel:",
    # The company's own spending, not an order it received.
    "capital expenditure", "capex", "funded through", "funded by",
    "internal accruals", "internal resources", "project entails",
    "estimated cost", "investment of", "outlay",
    # Forward-looking projections. Accuracy Shipping's filing quoted
    # Rs 175-200 crore of revenue "expected to generate ... at peak
    # operations and full utilization" and the extractor took the midpoint,
    # Rs 187.50 Cr, as the order value.
    "expected to generate", "expected to clock", "at peak", "peak operations",
    "full utilisation", "full utilization", "revenue potential",
    "estimated revenue", "annual revenue", "revenue of approximately",
    "once operational", "on full ramp", "at maturity",
    # Management guidance, not an order: "we are expecting Total Income of
    # Rs 156 crores with PAT of minimum Rs 9 - 11 crores". Veerhealth's Rs 10.07
    # lakh sample order was stored as Rs 156 Cr from exactly this sentence.
    "total income of", "expecting total income", "expected total income",
    "expecting turnover", "expected turnover", "additional turnover",
    "expecting revenue", "expected revenue", "revenue guidance", "pat of",
    "profit after tax of", "net profit of", "we are expecting", "targeting",
]


# BSE's Regulation 30 table puts the figure in a bare cell with no currency
# marker and no unit:
#
#     "6. Size of the order            4,23,00,000"
#
# find_amounts() deliberately drops bare numbers, because most of them are
# dates, clause numbers and percentages. This reads one only when a value
# label sits immediately in front of it and the magnitude could plausibly be
# an order, and it is used strictly as a fallback.
_LABELLED_BARE = re.compile(
    r"(?:size|value|amount|consideration|cost)\s+(?:of\s+)?(?:the\s+)?"
    r"(?:order|contract|work\s*order|purchase\s*order|project|award)s?\s*"
    r"(?:\([^)]{0,40}\))?\s*[:\-]?\s*"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d{6,}(?:\.\d+)?)",
    re.IGNORECASE,
)


@dataclass
class Amount:
    value_inr: float
    currency: str
    raw: str
    start: int
    end: int
    is_range: bool = False
    low_inr: float | None = None
    high_inr: float | None = None
    confidence: float = 0.5


def _to_float(num: str) -> float | None:
    try:
        return float(num.replace(",", "").strip())
    except ValueError:
        return None


def _normalise_currency(tok: str | None) -> str:
    if not tok:
        return "INR"
    return CURRENCY_SYMBOLS.get(tok.strip().lower().replace(" ", ""), "INR")


def _mult(unit: str | None) -> float:
    if not unit:
        return 1.0
    return MULTIPLIERS.get(unit.strip().lower().rstrip("."), 1.0)


def _to_inr(value: float, currency: str) -> float:
    return value * CONFIG.fx_rates.get(currency, 1.0)


def _occurrences(low_text: str, phrase: str) -> list[int]:
    out, idx = [], 0
    while True:
        i = low_text.find(phrase, idx)
        if i < 0:
            return out
        out.append(i)
        idx = i + 1


def _proximity(low_text: str, amt: "Amount", phrases: list[str],
               max_dist: int, max_weight: float) -> float:
    """Strength of the nearest phrase from `phrases` to this amount.

    Distance-weighted, and a phrase that sits immediately *before* the number
    counts for more: filings read "earnest money deposit of Rs 2.5 crore",
    "order value is Rs 425 crore" -- the qualifier leads.
    """
    best = 0.0
    for p in phrases:
        for i in _occurrences(low_text, p):
            cue_start, cue_end = i, i + len(p)
            if cue_end <= amt.start:
                dist, leads = amt.start - cue_end, True
            elif cue_start >= amt.end:
                dist, leads = cue_start - amt.end, False
            else:
                dist, leads = 0, True
            if dist > max_dist:
                continue
            w = max_weight * (1.0 - dist / (max_dist + 1.0))
            if leads:
                w *= 1.15
            best = max(best, w)
    return best


# OCR and broken rupee fonts render the rupee sign as %, ~, ¢, =, ? or a stray
# letter: Veerhealth's PDF reads "worth n 0.07 Lacs" in its text layer and
# "%10.07 Lacs", "~ 105 crores", "¢ 5 - 7 crores" through Tesseract. In front of a
# figure followed by lakh or crore -- units only rupees use -- such a glyph can
# only be the rupee sign. Every repair below swaps one character for one, so
# offsets into the text stay valid.
_INDIAN_UNIT_AHEAD = (r"(?=\s?\d[\d,]*(?:\.\d+)?\s*(?:[-–]\s*\d[\d,]*(?:\.\d+)?\s*)?"
                      r"(?:lacs?|lakhs?|crores?|crs?\b|cr\.))")
_RUPEE_SYMBOL = re.compile(r"(?<!\d)[%~¢=?]" + _INDIAN_UNIT_AHEAD, re.IGNORECASE)
_RUPEE_LETTER = re.compile(r"(?<![\w.,])[ntz]" + _INDIAN_UNIT_AHEAD, re.IGNORECASE)


# Tesseract also reads the rupee sign as a 7: Veerhealth's covering letter OCRs
# as "worth %10.07 Lacs" and its press release as "worth 710.07 Lacs". A leading
# 7 cannot be told from a real one by the figure alone, so it is dropped only
# when the same figure appears elsewhere in the document behind a rupee sign.
_SEVEN_AS_RUPEE = re.compile(
    r"(?<![\w.,₹])7(\d[\d,]*(?:\.\d+)?)(?=\s*(?:lacs?|lakhs?|crores?)\b)", re.IGNORECASE)
_RUPEE_FIGURE = re.compile(
    r"₹\s?(\d[\d,]*(?:\.\d+)?)(?=\s*(?:lacs?|lakhs?|crores?)\b)", re.IGNORECASE)
# ... a 1 as I, l or | ("PAT of minimum 9 - I1 crores", "9- II crores"), and
# "crores" as "crorcs".
_OCR_ONES_IN_FIGURE = re.compile(
    r"(?<![\w.])(?=[\d.,Il|]*\d)[\d.,]*[Il|][\d.,Il|]*(?=\s*(?:lacs?|lakhs?|crores?)\b)")
_OCR_ONES_AFTER_DASH = re.compile(r"(\d\s?[-–]\s?)([Il|]{1,3})(?=\s*(?:lacs?|lakhs?|crores?)\b)")
_OCR_CRORES = re.compile(r"\bcrorcs\b", re.IGNORECASE)


def normalize_rupee_glyphs(text: str) -> str:
    if not text:
        return text
    text = _OCR_CRORES.sub("crores", text)
    text = _OCR_ONES_IN_FIGURE.sub(lambda m: re.sub(r"[Il|]", "1", m.group(0)), text)
    text = _OCR_ONES_AFTER_DASH.sub(lambda m: m.group(1) + "1" * len(m.group(2)), text)
    text = _RUPEE_LETTER.sub("₹", _RUPEE_SYMBOL.sub("₹", text))
    known = set(_RUPEE_FIGURE.findall(text))
    if known:
        text = _SEVEN_AS_RUPEE.sub(
            lambda m: "₹" + m.group(1) if m.group(1) in known else m.group(0), text)
    return text


def find_amounts(text: str) -> list[Amount]:
    """Every money-looking token in the text, currency-normalised to INR."""
    out: list[Amount] = []
    if not text:
        return out
    text = normalize_rupee_glyphs(text)

    consumed: list[tuple[int, int]] = []

    # Ranges first so "100-120 crore" isn't split into two amounts.
    for m in RANGE_RE.finditer(text):
        lo, hi = _to_float(m.group("lo")), _to_float(m.group("hi"))
        if lo is None or hi is None or hi <= lo:
            continue
        hi_unit = _mult(m.group("hiunit"))
        lo_unit = _mult(m.group("lounit")) if m.group("lounit") else hi_unit
        cur = _normalise_currency(m.group("cur") or m.group("cur2"))
        lo_inr, hi_inr = _to_inr(lo * lo_unit, cur), _to_inr(hi * hi_unit, cur)
        out.append(Amount(
            value_inr=(lo_inr + hi_inr) / 2, currency=cur, raw=m.group(0),
            start=m.start(), end=m.end(), is_range=True,
            low_inr=lo_inr, high_inr=hi_inr, confidence=0.45,
        ))
        consumed.append((m.start(), m.end()))

    for m in AMOUNT_RE.finditer(text):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        num = _to_float(m.group("num"))
        if num is None:
            continue
        cur_tok = m.group("cur") or m.group("cur2")
        unit = m.group("unit")

        # A bare number with no currency marker and no unit is almost always
        # noise (dates, clause numbers, percentages).  Drop it.
        if not cur_tok and not unit:
            continue
        # Bare "5 m"/"5 b"/"5 k" without a currency is too ambiguous.
        if not cur_tok and unit and unit.lower() in {"m", "b", "k"}:
            continue

        cur = _normalise_currency(cur_tok)
        value_inr = _to_inr(num * _mult(unit), cur)
        if value_inr <= 0:
            continue
        out.append(Amount(
            value_inr=value_inr, currency=cur, raw=m.group(0).strip(),
            start=m.start(), end=m.end(),
            confidence=0.6 if (cur_tok and unit) else 0.4,
        ))

    return out


def _labelled_bare(text: str) -> Amount | None:
    """An unmarked number directly behind an explicit value label."""
    for m in _LABELLED_BARE.finditer(text):
        val = _to_float(m.group("num"))
        if val is None or not (1e6 <= val <= 2e12):
            continue
        return Amount(value_inr=val, currency="INR", raw=m.group(0).strip(),
                      start=m.start("num"), end=m.end("num"), confidence=0.5)
    return None


def pick_order_value(text: str) -> Amount | None:
    """Choose which of the amounts in `text` is the order value.

    Scored by proximity to value cues, penalised by proximity to anti-cues.
    Ties break toward the larger amount, which is the usual convention in
    exchange filings (headline value first, sub-components after).
    """
    text = normalize_rupee_glyphs(text)
    amounts = find_amounts(text)
    if not amounts:
        return _labelled_bare(text)

    low = text.lower()
    scored: list[tuple[float, Amount]] = []
    for amt in amounts:
        # Cues reach further than anti-cues: "order value ... is Rs X" can be
        # a clause apart, but "EMD of Rs X" is always adjacent.
        cue = _proximity(low, amt, VALUE_CUES, max_dist=110, max_weight=3.0)
        anti = _proximity(low, amt, VALUE_ANTI_CUES, max_dist=45, max_weight=4.5)
        score = cue - anti

        # A tax qualifier right after the number is a strong tell.
        tail = low[amt.end:amt.end + 60]
        if any(t in tail for t in ("excluding gst", "including gst", "plus gst",
                                   "exclusive of tax", "inclusive of tax",
                                   "excluding taxes", "plus taxes", "excl. gst")):
            score += 2.0

        # Sanity band: real listed-company orders sit between ~Rs 10 lakh and
        # ~Rs 2 lakh crore. Outside that, heavily discount.
        if not (1e6 <= amt.value_inr <= 2e12):
            score -= 4.0
        scored.append((score, amt))

    scored.sort(key=lambda t: (round(t[0], 3), t[1].value_inr), reverse=True)
    best_score, best = scored[0]
    if best_score < -0.5:
        # Every candidate was anti-cued (an EMD, a bank guarantee). The real
        # order value may still be sitting in a bare Reg-30 table cell.
        return _labelled_bare(text)

    best.confidence = min(0.95, max(0.25, 0.35 + best_score * 0.12))
    if best.is_range:
        best.confidence *= 0.8
    return best


def fmt_inr(value: float | None) -> str:
    """Human-readable INR in the units an Indian market participant thinks in."""
    if value is None:
        return "n/a"
    if value >= 1e7:
        return f"Rs {value / 1e7:,.2f} Cr"
    if value >= 1e5:
        return f"Rs {value / 1e5:,.2f} L"
    return f"Rs {value:,.0f}"
