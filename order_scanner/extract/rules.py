"""Rule-based extraction of a structured order from announcement text.

This is the cheap first pass.  Anything it can't resolve confidently gets
handed to the LLM fallback in `llm.py`.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..config import (AMENDMENT_MARKERS, FIRMNESS_TIERS, GENERIC_HEADLINES,
                      JUNK_CUSTOMER_STARTS, NEVER_ORDER_HEADLINES,
                      NON_ORDER_MARKERS, NON_ORDER_PATTERNS,
                      ORDER_TRIGGER_PATTERNS, ORDER_TRIGGERS,
                      OWN_PROJECT_MIN_SIGNALS, OWN_PROJECT_PATTERNS,
                      NOT_ORDER_TOPICS, QUALITY_BUMPS, QUALITY_DINGS,
                      RELATED_PARTY_MARKERS)

_TRIGGER_RES = [re.compile(p, re.IGNORECASE) for p in ORDER_TRIGGER_PATTERNS]
_NON_ORDER_RES = [re.compile(p, re.IGNORECASE) for p in NON_ORDER_PATTERNS]
_OWN_PROJECT_RES = [re.compile(p, re.IGNORECASE) for p in OWN_PROJECT_PATTERNS]
# Whole words only. As substrings, "loa" matched "uploaded" and "loan" and "mou"
# matched "amount" -- which is how a results reply, an annual report and a
# working-capital loan all became orders.
_TRIGGER_WORDS = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in ORDER_TRIGGERS) + r")\b")
from .money import Amount, pick_order_value
from .guidance import extract_guidance
from .summary import describe

# ---------------------------------------------------------------- duration
# Filings write periods out in words at least as often as in digits
# ("One Month", "Eighteen Months", "Initially one year"), and the old
# digit-only pattern missed every one of them.
_WORD_NUMBERS = {
    "half": 0.5, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "twenty-four": 24, "twentyfour": 24, "thirty": 30,
    "thirty-six": 36, "thirtysix": 36, "forty": 40, "forty-five": 45,
    "fortyfive": 45, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100,
}
# Longest first, so "twenty-four" wins over "twenty".
_NUMTOK = r"\d{1,3}(?:\.\d+)?|" + "|".join(
    re.escape(w) for w in sorted(_WORD_NUMBERS, key=len, reverse=True))
_UNITS = r"months?|years?|yrs?|weeks?|days?"

_DUR = re.compile(
    r"(?:with(?:in)?|over|period of|completion (?:period|time|timeline) of|"
    r"execution period of|executed? (?:with)?in|spread over|duration of|"
    r"tenure of|complete[d]? (?:with)?in|to be (?:completed|executed) (?:with)?in|"
    r"stipulated (?:completion )?(?:timeline|period|time) of|"
    r"delivery schedule is for|operational within|initially|valid for|"
    r"term of|for a period of|(?:purchase )?(?:orders?|contracts?) is for)"
    r"\s*(?:a\s+period\s+of\s+)?(?:approximately\s+|about\s+|approx\.?\s+)?"
    rf"(?P<n>{_NUMTOK})[-\s]*"
    rf"(?P<u>{_UNITS})\b",
    re.IGNORECASE,
)
_DUR_ALT = re.compile(
    rf"(?P<n>{_NUMTOK})[-\s]*(?P<u>{_UNITS})\s*"
    r"(?:from the date of|from date of|execution period|completion period|"
    r"order period|contract period)",
    re.IGNORECASE,
)

# BSE's Regulation 30 disclosure is a two-column table. Flattened to text by
# the PDF extractor, the label and its value are separated and interleaved:
#
#   "Time period by which the        One Month"
#   "Time period by which the ... is to be executed     45 Days."
#   "Time period, if any, associated with the    Approx 9 months."
#
# No "within <n> months" phrasing survives that, so anchor on the label and
# take the first duration in the window after it.
_DUR_LABEL = re.compile(
    r"time\s*period[^.\n]{0,80}?(?:by\s+which|associated\s+with|"
    r"for\s+(?:the\s+)?(?:execution|completion)|of\s+(?:the\s+)?(?:order|contract))",
    re.IGNORECASE,
)
_DUR_BARE = re.compile(rf"(?P<n>{_NUMTOK})[-\s]*(?P<u>{_UNITS})\b", re.IGNORECASE)
_FY_END = re.compile(
    r"(?:by|before|on or before|latest by|complet\w+ by)\s+"
    r"(?:end of\s+)?(?:FY\s?)?(?P<m>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]*[,\s]+(?P<y>20\d{2})",
    re.IGNORECASE,
)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# ---------------------------------------------------------------- customer
_CUSTOMER = re.compile(
    r"(?:order|contract|loa|letter of award|letter of intent|work order|"
    r"purchase order)s?\s+(?:has been\s+)?(?:received\s+)?"
    r"(?:from|awarded by|by|placed by)\s+"
    r"(?:M/s\.?\s*)?(?P<name>[A-Z][A-Za-z0-9&.,''\-\(\) ]{3,80}?)"
    r"(?=\s*(?:,|\.|\sfor\s|\sto\s|\sworth|\svalued|\samounting|\sunder|\sas |$))",
    re.IGNORECASE | re.MULTILINE,
)
_CUSTOMER_ALT = re.compile(
    r"(?:from|by)\s+(?:M/s\.?\s*)?(?P<name>[A-Z][A-Za-z0-9&.,''\-\(\) ]{5,80}?"
    r"(?:Limited|Ltd\.?|Corporation|Corpn\.?|Board|Authority|Nigam|Council|"
    r"Commission|Department|Railways?|Ministry|Municipal|Institute|University|"
    r"Inc\.?|LLC|GmbH|PLC|Pvt\.?\s*Ltd\.?))",
)

GOVT_MARKERS = [
    "ministry", "government", "govt", "department of", "municipal",
    "corporation of", "authority", "nigam", "board", "council", "commission",
    "railways", "indian railways", "nhai", "pwd", "cpwd", "mes",
    "smart city", "jal nigam", "vidyut", "discom", "state of",
]
PSU_MARKERS = [
    "ntpc", "ongc", "bhel", "sail", "gail", "iocl", "indian oil", "bpcl",
    "hpcl", "powergrid", "power grid", "coal india", "nmdc", "hal",
    "bel ", "bharat electronics", "drdo", "isro", "nalco", "rites",
    "ircon", "rvnl", "irctc", "cochin shipyard", "mazagon", "garden reach",
    "oil india", "nhpc", "sjvn", "thdc", "neepco", "bsnl", "mtnl", "concor",
]
EXPORT_MARKERS = [
    "export", "overseas", "international", "usa", "u.s.a", "united states",
    "europe", "middle east", "africa", "uae", "saudi", "qatar", "oman",
    "kuwait", "bahrain", "singapore", "malaysia", "vietnam", "bangladesh",
    "nepal", "sri lanka", "australia", "canada", "germany", "france", "uk ",
    "united kingdom", "japan", "korea", "brazil", "mexico",
]


@dataclass
class ExtractedOrder:
    is_order: bool = False
    order_value_inr: float | None = None
    value_currency: str = "INR"
    value_is_range: bool = False
    value_low_inr: float | None = None
    value_high_inr: float | None = None
    customer: str | None = None
    customer_type: str = "unknown"
    execution_months: float | None = None
    order_type: str = "unknown"
    firmness: float = 0.5
    is_amendment: bool = False
    is_related_party: bool = False
    quality_adj: float = 0.0
    scope: str | None = None
    summary: str | None = None      # one readable sentence, see summary.py
    guidance: list = field(default_factory=list)   # forward-looking numbers, see guidance.py
    method: str = "regex"
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["notes"] = "; ".join(self.notes)
        return d

    @property
    def needs_llm(self) -> bool:
        """True when the cheap pass left something important unresolved."""
        if not self.is_order:
            return False
        if self.order_value_inr is None:
            return True
        if self.confidence < 0.55:
            return True
        if self.execution_months is None and self.order_value_inr > 5e8:
            return True
        return False


def looks_like_order(*texts: str | None) -> bool:
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return False
    if _TRIGGER_WORDS.search(blob):
        return True
    return any(rx.search(blob) for rx in _TRIGGER_RES)


def is_non_order(*texts: str | None) -> str | None:
    """Detect filings that say "order" but mean a regulatory/tax/court order,
    a credit-rating release, or press-rumour chatter.

    Pass ONLY the headline and subcategory (and at most a short lead of the
    body). Passing full PDF text produces false negatives: real order-win
    PDFs routinely mention the issuer's credit rating or a tax matter in
    boilerplate. Returns the marker that matched, or None.
    """
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return None
    for marker in NON_ORDER_MARKERS:
        if marker in blob:
            return marker
    for rx in _NON_ORDER_RES:
        m = rx.search(blob)
        if m:
            return m.group(0)[:60]
    return None


def _num_token(tok: str) -> float | None:
    tok = tok.strip().lower()
    if tok in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[tok])
    try:
        return float(tok)
    except ValueError:
        return None


def _to_months(n_tok: str, unit: str) -> float | None:
    n = _num_token(n_tok)
    if n is None or n <= 0:
        return None
    u = unit.lower()
    if u.startswith(("year", "yr")):
        months = n * 12
    elif u.startswith("month"):
        months = n
    elif u.startswith("week"):
        months = n / 4.33
    elif u.startswith("day"):
        months = n / 30.44
    else:
        return None
    # Anything outside this band is a misread, not an execution period.
    return months if 0.1 <= months <= 240 else None


def _duration_from_reg30(text: str) -> float | None:
    """The Reg-30 labelled field, read out of the flattened table."""
    for m in _DUR_LABEL.finditer(text):
        for dm in _DUR_BARE.finditer(text[m.end():m.end() + 320]):
            months = _to_months(dm.group("n"), dm.group("u"))
            if months is not None:
                return months
    return None


def own_project_signals(text: str | None) -> list[str]:
    """Evidence that this is the company's OWN project, not an order received.

    Unlike is_non_order() this reads the FULL text including the PDF, because
    the capex language only ever appears in the body. It is safe to do so
    because it needs OWN_PROJECT_MIN_SIGNALS corroborating matches before
    anything is concluded -- a genuine EPC order to build something for a
    customer can trip the licence-to-build pattern on its own, but it will
    not also discuss its own capital expenditure and funding mix.
    """
    if not text:
        return []
    hits: list[str] = []
    for rx in _OWN_PROJECT_RES:
        m = rx.search(text)
        if m:
            hits.append(re.sub(r"\s+", " ", m.group(0)).strip()[:48])
    return hits


def worth_pdf_probe(headline: str | None, subcategory: str | None) -> bool:
    """Should we open this filing's PDF even though it doesn't look like an
    order?

    Genuine wins hide behind "General Updates" / "Press Release" headlines
    with the news only in the attachment. Filing types that can never be an
    order win are rejected first so they cost nothing.
    """
    blob = f"{headline or ''} {subcategory or ''}".strip().lower()
    if not blob:
        return False
    if any(k in blob for k in NEVER_ORDER_HEADLINES):
        return False
    if is_non_order(headline, subcategory):
        return False
    return any(k in blob for k in GENERIC_HEADLINES)


def _duration_months(text: str) -> float | None:
    # The exchange's own structured field first, when the filing has one.
    months = _duration_from_reg30(text)
    if months is not None:
        return months
    for pat in (_DUR, _DUR_ALT):
        for m in pat.finditer(text):
            months = _to_months(m.group("n"), m.group("u"))
            if months is not None:
                return months
    m = _FY_END.search(text)
    if m:
        # Approximate: months from "now" is unknowable from text alone, so
        # treat a stated completion date as a rough 12-month default unless
        # the year is far out.
        from datetime import date
        y, mo = int(m.group("y")), _MONTHS[m.group("m")[:3].lower()]
        today = date.today()
        months = (y - today.year) * 12 + (mo - today.month)
        if 1 <= months <= 120:
            return float(months)
    return None


def _clean_customer(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" ,.;:-")
    # Strip leading filler the regex sometimes grabs.
    name = re.sub(r"^(?:the|a|an|our|its|their)\s+", "", name, flags=re.I)
    return name[:120]


_LEGAL_SUFFIX = re.compile(
    r"\b(?:limited|ltd|private|pvt|public|plc|incorporated|inc|corporation|"
    r"corpn|company|co)\b\.?", re.IGNORECASE)


def norm_company_name(name: str | None) -> str:
    """Canonical form for comparing two spellings of one company.

    NSE files as "Star Paper Mills Limited", BSE as "Star Paper Mills Ltd-$".
    Stripping the legal suffix, BSE's surveillance marker and all punctuation
    makes those equal.
    """
    n = (name or "").upper()
    n = re.sub(r"-\$+\s*$", " ", n)          # BSE surveillance suffix
    n = _LEGAL_SUFFIX.sub(" ", n)
    return re.sub(r"[^A-Z0-9]+", "", n)


def _is_junk_customer(cand: str) -> bool:
    """Reject descriptions the customer regex mistakes for a company name."""
    low = cand.lower()
    if len(cand) < 4 or low.startswith(JUNK_CUSTOMER_STARTS):
        return True
    # A real counterparty is a proper noun. Require a capitalised token or an
    # all-caps acronym; "having Ind" and friends have neither.
    return not (re.search(r"\b[A-Z][a-z]{2,}", cand) or cand.isupper())


def _customer(text: str, company_name: str | None = None) -> tuple[str | None, bool]:
    """Return (customer, is_self).

    `is_self` means the extracted counterparty is the filing company itself,
    which is never a real order win -- it is a parse error or a genuine
    related-party transaction. Either way it must not score as a customer
    order. Observed: a broker whose "customer" parsed as its own name.

    Every match is considered, not just the first: a junk leading match used
    to mask a good candidate later in the text.
    """
    self_hit = False
    for pat in (_CUSTOMER_ALT, _CUSTOMER):
        for m in pat.finditer(text):
            cand = _clean_customer(m.group("name"))
            if _is_junk_customer(cand):
                continue
            a, b = norm_company_name(cand), norm_company_name(company_name)
            if a and b and len(a) >= 5 and len(b) >= 5 and (
                    a == b or a in b or b in a):
                self_hit = True
                continue
            return cand, False
    return None, self_hit


def _customer_type(customer: str | None, text: str) -> str:
    blob = f"{customer or ''} {text}".lower()
    if any(k in blob for k in PSU_MARKERS):
        return "psu"
    if any(k in blob for k in GOVT_MARKERS):
        return "government"
    if any(k in blob for k in EXPORT_MARKERS):
        return "export"
    if customer:
        return "private"
    return "unknown"


# Amendment and cancellation markers are read from the lead of a filing only --
# headline, the PDF's "Sub:" line and the exchange's short body -- and as whole
# words. Across the full PDF they matched boilerplate: "Determination of
# Materiality" (termination), the table label "Special rights / termination /
# penalty", SEBI's "Amendment or termination of orders/contracts" citation --
# hiding real orders behind the dashboard's amendments filter.
_AMENDMENT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in AMENDMENT_MARKERS) + r")\b", re.IGNORECASE)


def _is_amendment(lead: str | None) -> bool:
    return bool(_AMENDMENT_RE.search(lead or ""))


def _order_type_and_firmness(text: str) -> tuple[str, float]:
    low = text.lower()
    for frac, keys in sorted(FIRMNESS_TIERS.items(), reverse=True):
        for k in keys:
            if re.search(rf"\b{re.escape(k)}s?\b", low):
                label = {
                    1.00: "work_order", 0.60: "loi", 0.40: "l1", 0.30: "mou",
                }[frac]
                if k in ("letter of award", "loa", "notification of award",
                         "letter of acceptance"):
                    label = "loa"
                return label, frac
    return "unknown", 0.5


def _quality_adjustment(text: str) -> tuple[float, list[str]]:
    low = text.lower()
    adj, notes = 0.0, []
    for k, v in QUALITY_BUMPS.items():
        if k in low:
            adj += v
            notes.append(f"+{v:.2f} {k}")
    for k, v in QUALITY_DINGS.items():
        if re.search(rf"\b{re.escape(k)}\b", low):
            adj += v
            notes.append(f"{v:.2f} {k}")
    return max(-0.4, min(0.5, adj)), notes


# The Reg-30 table ASKS, in every order filing, "Whether the promoter/ promoter
# group/ group companies have any interest in the entity that awarded the
# order(s)?" and "Whether the order(s) would fall within related party
# transactions? If yes, whether the same is done at arm's length". Matching the
# markers anywhere flagged 50 of 65 orders as related-party on the strength of
# the question alone. A marker sitting inside such a question is not evidence.
_RPT_QUESTION = re.compile(r"\bwhether\b|\bif\s+yes\b|\barm.?s[\s-]+length", re.IGNORECASE)


def _related_party_mentioned(text: str) -> bool:
    low = text.lower()
    for marker in RELATED_PARTY_MARKERS:
        for m in re.finditer(re.escape(marker), low):
            if not _RPT_QUESTION.search(low[max(0, m.start() - 160):m.start()]):
                return True
    return False


def _apply_reg30(res: ExtractedOrder, reg, text: str, company_name: str | None,
                 filed_at) -> None:
    """Let the disclosure table's answers override guesses from flattened text.

    The table is the filing's own structured statement of customer, period,
    size and related-party status, so where it gives an answer it wins.
    """
    from .reg30 import clean_customer, parse_period, parse_size, yes_no

    used = []
    amt = parse_size(reg.size)
    if amt is not None:
        res.order_value_inr = amt.value_inr
        res.value_currency = amt.currency
        res.value_is_range = amt.is_range
        res.value_low_inr, res.value_high_inr = amt.low_inr, amt.high_inr
        res.confidence = max(res.confidence, amt.confidence)
        used.append("value")

    customer = clean_customer(reg.customer)
    if customer and reg.customer_is_awardee:
        # "Name of the entity to which the order is awarded" sometimes names the
        # company's own SPV rather than the customer.
        a, b = norm_company_name(customer), norm_company_name(company_name)
        if (a and b and (a in b or b in a)) or re.search(
                r"\b(?:subsidiary|spv|special purpose|the company)\b", customer, re.IGNORECASE):
            customer = None
    if customer:
        res.customer = customer
        res.customer_type = _customer_type(customer, text)
        used.append("customer")
    if (reg.domestic and re.search(r"\binternational\b", reg.domestic, re.IGNORECASE)
            and not re.search(r"\bdomestic\b", reg.domestic, re.IGNORECASE)
            and res.customer_type in ("private", "unknown")):
        res.customer_type = "export"

    months = parse_period(reg.period, filed_at)
    if months is not None:
        res.execution_months = months
        used.append("period")

    answers = [yes_no(reg.related_party), yes_no(reg.promoter_interest)]
    if True in answers:
        res.is_related_party = True
        used.append("related party: yes")
    elif False in answers:
        res.is_related_party = False
        used.append("related party: no")

    if res.order_type == "unknown" and not res.is_amendment:
        label, frac = _order_type_and_firmness(" ".join(filter(None, [reg.nature, reg.terms])))
        if label == "unknown" and (reg.customer or reg.size or reg.period):
            # The table names a customer, size or period, so this is the SEBI
            # disclosure of an order received, and nothing says it is less than
            # firm (an LOI or MoU would have matched above). A table with only a
            # "nature" row -- a loan facility, say -- earns no such default.
            label, frac = "work_order", 0.8
        res.order_type, res.firmness = label, frac
        res.is_amendment = label == "amendment"
        used.append("type")

    res.notes.append(f"Reg-30 table ({reg.method}): "
                     + (", ".join(used) if used else "nothing usable"))


# ---------------------------------------------------------------- order received?
# A filing counts only when it says the company RECEIVED an order. Trigger words
# alone let through a regulator's "approval order", a partner contract the
# company signed as a buyer, a hospital management agreement and a bidder that
# merely emerged L1.
_RECEIPT_NOUN = (r"(?:(?:purchase|work|supply|service|export|repeat|new|pilot)\s+)?"
                 r"(?:orders?|contracts?|letters?\s+of\s+(?:award|intent|acceptance|commencement)|"
                 r"lo[ai]s?|lol|notifications?\s+of\s+award|awards?\s+of\s+contract)")
_RECEIPT_RES = [
    # "has received / secured / bagged / been awarded ... an order", and press
    # releases in the present tense: "secures landmark Order", "Wins ... Orders"
    re.compile(r"\b(?:received|receives|secured|secures|bagged|bags|won|wins|obtained|"
               r"awarded)\b[^.;:?]{0,140}?\b" + _RECEIPT_NOUN + r"\b", re.IGNORECASE),
    # "an order has been received / placed"
    re.compile(r"\b" + _RECEIPT_NOUN + r"\b[^.;:?]{0,80}?\b(?:has|have|was|were)\s+been\s+"
               r"(?:received|awarded|placed|issued)\b", re.IGNORECASE),
    # "Receipt of Purchase Order", "Intimation of receipt of LOI"
    re.compile(r"\breceipt\s+of\s+(?:an?\s+|the\s+)?" + _RECEIPT_NOUN + r"\b", re.IGNORECASE),
    # "Order Received from Godrej Properties", "contract win of Rs 85.53 Crore"
    re.compile(r"\b" + _RECEIPT_NOUN + r"\s+(?:received|bagged|secured|won)\b", re.IGNORECASE),
    re.compile(r"\b(?:contract|order)\s+wins?\b", re.IGNORECASE),
]
# Words that turn a receipt-shaped phrase into something else: a regulator's
# order, the Reg-30 table's own questions ("Whether order(s) have been awarded
# by domestic/international entity"), or a negation.
_NOT_RECEIPT_BEFORE = re.compile(r"\bwhether\b|\bif\s+yes\b|\bnot\b", re.IGNORECASE)
_REGULATOR_ORDER = re.compile(
    r"\bapproval\b|\bregional\s+director|\bregistrar\b|\btribunal\b|\bcourt\b|"
    r"\bshow\s+cause|\bsebi\b|\bstock\s+exchange|\bpenalt|\bassessment\b|\bincome[\s-]+tax\b|"
    r"domestic\b[^.;]{0,40}\binternational", re.IGNORECASE)
# "to be finalized after receipt of Letter of Acceptance": a receipt still to
# come, which is how an L1 bidder describes the award it is waiting for.
_PENDING = re.compile(
    r"\b(?:after|upon|on|subject\s+to|pending|awaiting|until|till|before|expecting|expected)"
    r"\s+(?:the\s+)?$", re.IGNORECASE)
_L1_ONLY = re.compile(
    r"\bemerge[sd]?\s+as\s+(?:the\s+)?(?:single\s+)?(?:lowest\s+bidder|l-?1)\b|"
    r"\blowest\s+bidder\b|\bl-?1\s+bidder\b|\bdeclared\s+(?:as\s+)?l-?1\b", re.IGNORECASE)
_SUBJECT = re.compile(
    r"\bsub(?:ject)?\s*[:.\-–]\s*(.{5,220}?)(?=\s+(?:dear|ref\b|respected|pursuant|"
    r"with\s+reference|in\s+terms\s+of|we\s)|$)", re.IGNORECASE)
_ORDER_CATEGORIES = ("bagging/receiving of order", "receipt of order", "awarding of order")


@dataclass
class OrderEvidence:
    received: bool
    reason: str


def _pdf_subject(pdf_text: str | None) -> str:
    m = _SUBJECT.search(re.sub(r"\s+", " ", (pdf_text or "")[:4000]))
    return m.group(1) if m else ""


def _receipt_statement(text: str) -> str | None:
    for rx in _RECEIPT_RES:
        for m in rx.finditer(text):
            # "Whether ... have been awarded", "has not received" just before;
            # "the approval order from the Regional Director" inside or just after.
            before = text[max(0, m.start() - 40):m.end()]
            after = text[m.start():m.end() + 40]
            if _NOT_RECEIPT_BEFORE.search(before) or _REGULATOR_ORDER.search(after):
                continue
            if _PENDING.search(text[max(0, m.start() - 30):m.start()]):
                continue
            return re.sub(r"\s+", " ", m.group(0))[:80]
    return None


def order_receipt_evidence(headline: str | None, body: str | None,
                           pdf_text: str | None, subcategory: str | None = None,
                           reg30=None) -> OrderEvidence:
    """Did this filing report that the company received an order?

    Rejected: a filing whose topic is results, a report, a presentation, fund
    raising, a loan or an office move; a lowest-bidder (L1) intimation with no
    order yet. Accepted: an explicit receipt statement, the SEBI order-disclosure
    table naming a customer or an order size, or the exchange's own order
    category. Amendments and cancellations of orders are kept -- they are
    flagged separately and hidden on the dashboard by default.
    """
    subject = _pdf_subject(pdf_text)
    topic_blob = " ".join(filter(None, [subcategory, headline, subject])).lower()
    topic = next((t for t in NOT_ORDER_TOPICS if t in topic_blob), None)
    if topic:
        return OrderEvidence(False, f"the filing is about '{topic}'")

    lead = " ".join(filter(None, [headline, subject, body]))
    if _is_amendment(lead) and re.search(r"\b(?:orders?|contracts?|lo[ai])\b", lead,
                                         re.IGNORECASE):
        return OrderEvidence(True, "an amendment or cancellation of an order")

    full = " ".join(filter(None, [headline, body, pdf_text]))
    statement = _receipt_statement(full)
    if statement:
        return OrderEvidence(True, f"states '{statement}'")
    if _L1_ONLY.search(full):
        return OrderEvidence(False, "lowest bidder (L1) only; no order received yet")
    if reg30 is not None:
        from .reg30 import clean_customer, parse_size
        if clean_customer(reg30.customer) or parse_size(reg30.size):
            return OrderEvidence(True, "the SEBI order-disclosure table names a customer or size")
    if subcategory and any(k in subcategory.lower() for k in _ORDER_CATEGORIES):
        return OrderEvidence(True, f"filed under '{subcategory}'")
    return OrderEvidence(False, "no statement that the company received an order")


def extract(headline: str | None, body: str | None,
            pdf_text: str | None = None,
            company_name: str | None = None,
            reg30=None, filed_at=None, subcategory=None) -> ExtractedOrder:
    """Run the rule pass over the best available text for one filing."""
    parts = [p for p in (headline, body, pdf_text) if p]
    text = "\n".join(parts)
    res = ExtractedOrder()

    if not text.strip():
        res.notes.append("empty text")
        return res

    if not looks_like_order(text):
        return res

    # Headline + a short lead of the body only -- see is_non_order().
    marker = is_non_order(headline, (body or "")[:400])
    if marker:
        res.notes.append(f"not a commercial order: matched '{marker}'")
        return res

    # The company building its own facility is not an order received, however
    # much it reads like one. Full text, and only on corroborating evidence.
    own = own_project_signals(text)
    if len(own) >= OWN_PROJECT_MIN_SIGNALS:
        res.notes.append(
            "the company's own project, not an order received "
            f"({len(own)} signals: {', '.join(own[:4])})")
        return res

    judged = order_receipt_evidence(headline, body, pdf_text, subcategory, reg30)
    if not judged.received:
        res.notes.append(f"not an order received: {judged.reason}")
        return res

    res.is_order = True
    res.notes.append(f"order evidence: {judged.reason}")
    if own:
        # One signal is not enough to reject, but it is worth recording.
        res.notes.append(f"possible own-project language: {own[0]}")

    # Prefer the headline + body for value extraction; PDFs carry a lot of
    # boilerplate money (capital, guarantees) that confuses the picker.
    primary = "\n".join(p for p in (headline, body) if p)
    amt: Amount | None = pick_order_value(primary) if primary.strip() else None
    if amt is None and pdf_text:
        amt = pick_order_value(pdf_text)
        if amt:
            res.notes.append("value from PDF")

    if amt:
        res.order_value_inr = amt.value_inr
        res.value_currency = amt.currency
        res.value_is_range = amt.is_range
        res.value_low_inr = amt.low_inr
        res.value_high_inr = amt.high_inr
        res.confidence = amt.confidence
        if amt.currency != "INR":
            res.notes.append(f"converted from {amt.currency} at configured FX")
    else:
        res.notes.append("no order value found by rules")
        res.confidence = 0.2

    res.execution_months = _duration_months(text)
    res.customer, customer_is_self = _customer(text, company_name)
    res.customer_type = _customer_type(res.customer, text)
    res.order_type, res.firmness = _order_type_and_firmness(text)
    if _is_amendment(" ".join(filter(None, [headline, _pdf_subject(pdf_text), body]))):
        res.order_type, res.firmness = "amendment", 0.0
    res.is_amendment = res.order_type == "amendment"
    res.is_related_party = _related_party_mentioned(text)
    # A subsidiary winning a real order is real group revenue, so this is not
    # a penalty -- but the order does not belong to the listed entity's own
    # P&L directly, which is worth saying out loud on the row.
    if re.search(r"\bsubsidiar(?:y|ies)\b[^.\n]{0,40}\bof\b|"
                 r"\bsubsidiary in which\b|\bstep[- ]down subsidiary\b",
                 text, re.IGNORECASE):
        res.notes.append("counterparty/awardee is a subsidiary, not the listed entity")

    if customer_is_self:
        # The only counterparty found was the issuer itself. Flagging this as
        # related-party both applies the score penalty and blocks the
        # always-alert override, which is exactly the desired handling.
        res.is_related_party = True
        res.notes.append("customer resolved to the filing company itself")
    if reg30 is not None:
        _apply_reg30(res, reg30, text, company_name, filed_at)
    res.guidance = extract_guidance(text, filed_at)
    res.quality_adj, qnotes = _quality_adjustment(text)
    res.notes.extend(qnotes)
    res.scope, res.summary = describe(
        headline, body, pdf_text, customer=res.customer,
        order_type=res.order_type, value_inr=res.order_value_inr,
        value_is_range=res.value_is_range, value_low_inr=res.value_low_inr,
        value_high_inr=res.value_high_inr,
        execution_months=res.execution_months)

    # Confidence bonuses for corroborating structure.
    if res.customer:
        res.confidence = min(0.95, res.confidence + 0.08)
    if res.execution_months:
        res.confidence = min(0.95, res.confidence + 0.05)
    if res.order_type in ("loa", "work_order"):
        res.confidence = min(0.95, res.confidence + 0.05)

    return res
