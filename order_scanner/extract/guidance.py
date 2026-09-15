"""Forward-looking statements: management's own numbers for a coming year.

A filing that says "In Current F.Y. 2026-27 we are expecting Total Income of
Rs 105 crores with PAT of minimum Rs 5 - 7 crores" is telling the market that
income will roughly triple and profit rise ninefold. That can matter far more
than the order the filing is nominally about -- in Veerhealth's case a
Rs 10.07 lakh sample order -- so the guidance is extracted and scored on its own
(see `_guidance` in score.py).

A figure is kept only when it has a year, a metric and an amount (or a growth
rate) in a sentence that looks ahead. Projections that are not company-wide
guidance are dropped clause by clause: a client's incremental business
("additional turnover of around Rs 3-4 crores from above client"), a single
plant at full utilisation, an order book. Clause by clause, because OCR turns
full stops into commas and runs the company's guidance and the client's
projection into one sentence.
"""
from __future__ import annotations

import bisect
import re
from datetime import date, datetime

from .money import find_amounts, normalize_rupee_glyphs

_FORWARD = re.compile(
    r"\b(?:expect\w*|target\w*|guidance|guided|aim(?:s|ing)?\s+to|projected|projects|"
    r"outlook|anticipat\w*|envisag\w*|on\s+track\s+to|estimate[sd]?|"
    r"plan(?:s|ning)?\s+to\s+(?:achieve|reach|clock|touch))\b", re.IGNORECASE)
_NOT_COMPANY_WIDE = re.compile(
    r"\badditional\s+(?:turnover|revenue|income|sales|business)\b|"
    r"\bfrom\s+(?:the\s+)?(?:above|this|said|such|that)\s+(?:client|customer|order)\b|"
    r"\bat\s+peak\b|\bfull\s+(?:capacity|utili[sz]ation)\b|\bplant\b|\bcapex\b|"
    r"\border\s*(?:book|inflow|pipeline)\b", re.IGNORECASE)
_FY = re.compile(r"\bFY\s?'?(\d{4}|\d{2})(?:\s*[-–/]\s*'?(\d{4}|\d{2}))?\b", re.IGNORECASE)
_CURRENT_FY = re.compile(r"\b(?:current|this|ongoing)\s+(?:financial|fiscal)\s+year\b", re.IGNORECASE)
_NEXT_FY = re.compile(r"\b(?:next|coming)\s+(?:financial|fiscal)\s+year\b", re.IGNORECASE)
_METRICS = [
    ("pat", re.compile(r"\b(?:pat|profit\s+after\s+tax|net\s+profit|bottom[\s-]?line)\b",
                       re.IGNORECASE)),
    ("revenue", re.compile(r"\b(?:total\s+income|revenues?(?:\s+from\s+operations)?|turnover|"
                           r"top[\s-]?line|net\s+sales|sales|income)\b", re.IGNORECASE)),
]
_GROWTH = re.compile(
    r"(?:grow\w*|growth|increase\w*|rise|jump)\s+(?:of\s+|by\s+)?(?:around\s+|about\s+|"
    r"approximately\s+|over\s+)?(\d{1,3}(?:\.\d+)?)\s*%|"
    r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:(?:[-–]|to)\s*\d{1,3}(?:\.\d+)?\s*%\s*)?(?:y-?o-?y\s+)?"
    r"(?:growth|increase|rise|jump)", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.;!?])\s+(?=[A-Z(])")
# A comma or semicolon before a word ends a clause; "1,050" has no space.
_CLAUSE = re.compile(r"[,;:]\s+(?=[A-Za-z])")


def _as_date(when) -> date:
    if isinstance(when, datetime):
        return when.date()
    if isinstance(when, date):
        return when
    try:
        return datetime.fromisoformat(str(when)).date()
    except (TypeError, ValueError):
        return date.today()


def fiscal_year_end(when) -> int:
    """Indian financial years run April to March: 11 Sep 2026 is in FY27."""
    d = _as_date(when)
    return d.year + 1 if d.month >= 4 else d.year


def _year(tok: str) -> int:
    y = int(tok)
    return y + 2000 if y < 100 else y


def _years_mentioned(sentence: str, current: int) -> list[tuple[int, int]]:
    """(position, year ended) for each financial year named, from this one on."""
    out = []
    for m in _FY.finditer(sentence):
        # "FY 2026-27" -> 2027; "FY27" and "FY2027" -> 2027
        end = _year(m.group(2)) if m.group(2) else _year(m.group(1))
        if current <= end <= current + 5:
            out.append((m.start(), end))
    out += [(m.start(), current) for m in _CURRENT_FY.finditer(sentence)]
    out += [(m.start(), current + 1) for m in _NEXT_FY.finditer(sentence)]
    return sorted(out)


def extract_guidance(text: str | None, filed_at=None) -> list[dict]:
    """Company-wide guidance found in the text.

    Each item: {fy_end, metric ("revenue" | "pat"), label, low_inr, high_inr,
    or growth_low, sentence}. A range keeps its lower bound as `low_inr`, which
    is what the scorer uses: "PAT of minimum Rs 5 - 7 crores" counts as Rs 5 Cr.
    """
    if not text:
        return []
    t = normalize_rupee_glyphs(re.sub(r"\s+", " ", text))
    t = re.sub(r"\bF\.?\s*Y\.?(?=\s*'?\d)", "FY", t)       # "F.Y. 2026-27", "F. Y. 2027-28"
    current = fiscal_year_end(filed_at)
    out: list[dict] = []
    seen: set = set()
    for sentence in _SENTENCE.split(t):
        if not _FORWARD.search(sentence):
            continue
        years = _years_mentioned(sentence, current)
        if not years:
            continue
        cuts = [0] + [m.end() for m in _CLAUSE.finditer(sentence)] + [len(sentence)]
        hits = sorted(((m.start(), m.end(), metric, m.group(0))
                       for metric, rx in _METRICS for m in rx.finditer(sentence)),
                      key=lambda h: h[0])
        metrics: list[tuple] = []
        for h in hits:
            if metrics and h[0] < metrics[-1][1]:
                continue                                  # overlapping match
            if sentence[max(0, h[0] - 6):h[0]].lower().endswith("other "):
                continue                                  # "other income" is not revenue
            metrics.append(h)
        for i, (start, end, metric, label) in enumerate(metrics):
            c = bisect.bisect_right(cuts, start) - 1
            clause_start, clause_end = cuts[c], cuts[c + 1]
            if _NOT_COMPANY_WIDE.search(sentence[clause_start:clause_end]):
                continue
            # the year named nearest before the metric, else the first one after
            before = [y for pos, y in years if pos < start]
            fy = before[-1] if before else years[0][1]
            if (fy, metric) in seen:
                continue
            stop = min(metrics[i + 1][0] if i + 1 < len(metrics) else len(sentence),
                       clause_end, end + 80)
            window = sentence[end:stop]
            amounts = [a for a in find_amounts(window) if a.value_inr >= 1e5]
            item = {"fy_end": fy, "metric": metric, "label": label.lower(),
                    "sentence": sentence.strip()[:240]}
            if amounts:
                a = amounts[0]
                item["low_inr"] = a.low_inr if a.is_range else a.value_inr
                item["high_inr"] = a.high_inr if a.is_range else None
            else:
                g = _GROWTH.search(window) or _GROWTH.search(sentence[max(clause_start, start - 40):start])
                if not g:
                    continue
                item["growth_low"] = float(g.group(1) or g.group(2)) / 100
            seen.add((fy, metric))
            out.append(item)
    return out
