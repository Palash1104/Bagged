"""Central configuration. Everything tunable lives here or in .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load the project-root .env by absolute path, so a scheduled run (Task
# Scheduler / cron) picks it up regardless of its working directory.
# DOTENV_STATUS is surfaced by `cli config` and `cli test-alert` so a missing
# or unreadable .env is visible instead of silently yielding empty secrets.
DOTENV_PATH = ROOT / ".env"
DOTENV_STATUS = ""
try:
    from dotenv import load_dotenv
except ImportError:  # dotenv optional
    DOTENV_STATUS = ("python-dotenv is NOT installed, so .env is ignored "
                     "— run: pip install python-dotenv")
else:
    if DOTENV_PATH.exists():
        load_dotenv(DOTENV_PATH, override=False)
        DOTENV_STATUS = f"loaded {DOTENV_PATH}"
    else:
        DOTENV_STATUS = f"no .env file at {DOTENV_PATH}"
    load_dotenv(override=False)  # also honour a .env in the cwd / further up

DATA_DIR = Path(os.getenv("ORDER_SCANNER_DATA", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "orders.db"
PDF_CACHE = DATA_DIR / "pdf_cache"
PDF_CACHE.mkdir(parents=True, exist_ok=True)

CRORE = 1e7
LAKH = 1e5


# --------------------------------------------------------------------------
# Scoring weights.  Total of the six components = 100 before multipliers.
# --------------------------------------------------------------------------
@dataclass
class ScoreWeights:
    annualised_impact: float = 40.0   # annualised order value / TTM revenue
    quarterly_ratio: float = 20.0     # raw order value / latest quarterly revenue
    firmness: float = 15.0            # LOA vs LOI vs MoU vs framework
    margin_quality: float = 10.0      # EBITDA margin + order-mix keywords
    novelty: float = 10.0             # vs this company's own order history
    capacity: float = 5.0             # can they plausibly execute it


@dataclass
class Thresholds:
    """Alert tiers on the 0-100 composite score."""
    high: float = 75.0        # -> Telegram, red
    medium: float = 60.0      # -> Telegram, amber
    watch: float = 40.0       # -> dashboard + daily digest only
    # Everything below `watch` is stored silently.

    # Hard floor: never alert on an order below this absolute value,
    # regardless of ratio.  Stops nano-cap noise.
    min_order_value_inr: float = 1.0 * CRORE

    # Your original rule, kept as an explicit override: if the order is
    # >= this multiple of the latest quarterly revenue, always alert even
    # if the composite score is low (e.g. because revenue data is thin).
    always_alert_quarterly_multiple: float = 0.50

    # Upper bound on believability. A listed company does not win an order
    # worth 25x its last quarter's revenue; when the ratio is that extreme
    # the likely cause is a misparsed figure (aggregate turnover, a rated
    # facility, a cumulative order book) or wrong/stale revenue. Above this
    # the order is treated as a data error and held at WATCH for review
    # rather than promoted. Observed: a broker with Rs 27 Cr quarterly
    # revenue scored MEDIUM on a "Rs 9,000 Cr order" -- 327x.
    max_plausible_quarterly_multiple: float = 25.0


@dataclass
class Materiality:
    """How big an order must be before anything else about it counts.

    Firmness, margin, novelty and capacity describe the QUALITY of an order;
    on their own they say nothing about whether it moves the business. Left
    additive they handed a Rs 3 Cr LOA 39 of their 40 points for being firm,
    first-ever and from a high-margin company, and a Rs 450 Cr order at a
    Rs 40,000 Cr company 36 points -- one short of WATCH -- with no size
    points at all. So those four components are multiplied by a gate from
    0 to 1, read off whichever lens shows the order as bigger:

      annualised value / TTM revenue:   0 at annualised_floor, 1 at annualised_full
      order value / quarterly revenue:  0 at quarterly_floor,  1 at quarterly_full

    log-interpolated in between.
    """
    annualised_floor: float = 0.02
    annualised_full: float = 0.10     # adds a tenth to annual revenue
    quarterly_floor: float = 0.10
    quarterly_full: float = 0.50      # your original 50%-of-a-quarter rule
    # Significance cannot be measured without an order value AND some revenue
    # figure. Such orders keep a sliver of their quality points so the
    # dashboard still ranks them, but 0.25 x 40 cannot reach WATCH.
    unknown_gate: float = 0.25


@dataclass
class GuidanceScoring:
    """Management's forward-looking numbers, scored on the growth they imply.

    Veerhealth guided FY27 total income of Rs 105 Cr against FY26's Rs 32.97 Cr
    (3.2x) and PAT of at least Rs 5 Cr against Rs 0.54 Cr (9x), in a filing whose
    order was a Rs 10.07 lakh sample. Guidance like that deserves an alert on its
    own; a 10% growth outlook does not. Growth is taken per year, so FY28
    guidance measured against FY26 is not credited as one year's growth.
    """
    revenue_floor: float = 1.15   # per-year revenue multiple below +15%: nothing
    revenue_full: float = 2.0     # doubling in a year: full marks
    pat_floor: float = 1.25
    pat_full: float = 3.0
    revenue_weight: float = 0.6   # PAT takes the rest
    pat_only_factor: float = 0.8  # PAT guidance with no revenue figure beside it
    horizon_factors: tuple = (1.0, 0.85, 0.65)   # current FY, next FY, later
    # Guided revenue above this multiple of last year reads as a misread figure
    # or a pump, not a plan: discounted and held at WATCH.
    implausible_multiple: float = 8.0
    implausible_factor: float = 0.3
    # The final score is the stronger of order and guidance plus this share of
    # the weaker, so a strong order WITH strong guidance ranks above either.
    combine_bonus: float = 0.25


@dataclass
class Penalties:
    """Multiplicative haircuts applied to the composite score."""
    order_exceeds_5x_mcap: float = 0.40      # classic micro-cap pump pattern
    order_exceeds_2x_mcap_sme: float = 0.60
    related_party_customer: float = 0.50
    stale_announcement: float = 0.70          # older than `stale_hours`
    stale_hours: int = 48
    no_revenue_data_score_cap: float = 60.0   # cap, not multiplier
    implausible_quarterly_ratio: float = 0.35  # over max_plausible_quarterly_multiple


@dataclass
class Config:
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    thresholds: Thresholds = field(default_factory=Thresholds)
    penalties: Penalties = field(default_factory=Penalties)
    materiality: Materiality = field(default_factory=Materiality)
    guidance: GuidanceScoring = field(default_factory=GuidanceScoring)

    # --- scan window -------------------------------------------------
    lookback_minutes: int = 60       # each scheduled run re-reads this window (hourly task)
    overlap_minutes: int = 10        # overlap so nothing slips between runs
    # A scheduled scan also resumes from the end of the last recorded run, so
    # hours the PC was off or asleep are caught up on the next run -- up to
    # this far back (a whole weekend fits); longer gaps need `backfill`.
    max_catchup_hours: int = 96

    # --- networking --------------------------------------------------
    request_timeout: int = 20
    max_retries: int = 3
    retry_backoff: float = 2.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )

    # --- extraction --------------------------------------------------
    download_pdfs: bool = True
    fetch_sectors: bool = True     # BSE / yfinance lookup, once per company
    ocr_pdfs: bool = True          # Tesseract OCR for scanned filings with no text layer
    fetch_annuals: bool = True     # screener.in annual P&L, the base for guidance
    max_pdf_mb: float = 12.0
    # Look inside the PDFs of generic-headline filings ("General Updates",
    # "Press Release") to catch order wins the headline hides. Cheap on a
    # 30-minute live window, expensive on a backfill -- run_scan takes a
    # probe_pdfs override and backfill() passes False.
    probe_generic_pdfs: bool = True
    max_pdf_probes_per_run: int = 60   # bandwidth guard
    llm_fallback: bool = True
    llm_provider: str = os.getenv("LLM_PROVIDER", "anthropic")
    llm_model: str = os.getenv("LLM_MODEL", "claude-sonnet-4-5")
    llm_api_key: str = os.getenv("ANTHROPIC_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    llm_max_calls_per_run: int = 40   # cost guard

    # --- fx (used when an order is quoted in a foreign currency) ------
    fx_rates: dict = field(default_factory=lambda: {
        "INR": 1.0,
        "USD": float(os.getenv("FX_USD_INR", 88.0)),
        "EUR": float(os.getenv("FX_EUR_INR", 96.0)),
        "GBP": float(os.getenv("FX_GBP_INR", 112.0)),
    })

    # --- alerting ----------------------------------------------------
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # --- exchanges ---------------------------------------------------
    scan_nse: bool = True
    scan_bse: bool = True
    include_sme: bool = True          # store SME, but they get a score haircut

    # --- market hours (IST).  Outside these, scan runs are no-ops
    # unless --force is passed.  Companies do file after hours, so the
    # window is deliberately generous.
    market_open_hhmm: str = "08:00"
    market_close_hhmm: str = "22:00"
    scan_weekends: bool = False


CONFIG = Config()


# --------------------------------------------------------------------------
# Keyword dictionaries used by the rule-based extractor.
# --------------------------------------------------------------------------

# An announcement must hit one of these to be considered order-related.
ORDER_TRIGGERS = [
    "letter of award", "letter of intent", "loa", "lol", "work order",
    "purchase order", "supply order", "order received", "receipt of order",
    "award of order", "bagged", "secured an order", "secured order",
    "wins order", "won order", "awarded", "award of contract", "contract won",
    "new order", "order book", "order intake", "emerged as l1",
    "lowest bidder", "l-1 bidder", "declared l1", "notice to proceed",
    "contract agreement", "framework agreement", "rate contract",
    "order win", "receipt of work order", "bags order", "bags contract",
    "development agreement", "epc contract", "o&m contract",
    "export order", "domestic order", "order worth", "order valued",
    "received an order", "receipt of an order", "supply contract",
    "memorandum of understanding", "mou", "notification of award",
    "confirmed order", "order confirmation", "repeat order",
]

# Regex triggers for phrasings the substring list can't cover, e.g.
# "has received a Rs 40 crore order", "wins Rs 100 cr contract from".
#
# NOTE the `s?` on every noun. Without it \border\b cannot match "orders"
# (there is no word boundary between "order" and "s"), and BSE files its
# most on-target announcements under the exact headline
# "Bagging/Receiving of orders/contracts" -- all of which were being
# classified as not-order-like, so no PDF was fetched and they never
# reached the dashboard at all.
ORDER_TRIGGER_PATTERNS = [
    r"\b(?:receiv|secur|bagg?|won|win[s]?|award|obtain|procur|land)\w*\b"
    r"[^.\n]{0,60}?\b(?:orders?|contracts?|loa|tenders?|projects?)\b",
    r"\b(?:orders?|contracts?)\b[^.\n]{0,40}?"
    r"\b(?:worth|valued at|value of|aggregating|amounting)\b",
    r"\borders?\s+(?:book|intake|inflow)\b",
]

# Firmness tiers -> fraction of the firmness component awarded.
FIRMNESS_TIERS = {
    1.00: ["letter of award", "loa", "work order", "purchase order",
           "supply order", "contract agreement", "notice to proceed",
           "definitive agreement", "signed contract", "executed contract",
           "notification of award",
           "letter of commencement", "award of contract", "works contract",
           "supply contract", "service contract", "commercial order",
           "service order"],
    0.60: ["letter of intent", "loi", "lol", "in-principle", "in principle"],
    0.40: ["emerged as l1", "lowest bidder", "l-1 bidder", "declared l1",
           "preferred bidder", "selected as"],
    0.30: ["memorandum of understanding", "mou", "framework agreement",
           "rate contract", "empanel", "strategic partnership"],
}

# Quality bumps / dings on the margin-mix component.
QUALITY_BUMPS = {
    "export": 0.15, "overseas": 0.12, "international": 0.10,
    "defence": 0.20, "defense": 0.20, "aerospace": 0.15,
    "repeat order": 0.10, "operation and maintenance": 0.12,
    "o&m": 0.12, "annuity": 0.15, "ham": 0.10,
    "semiconductor": 0.15, "data centre": 0.12, "data center": 0.12,
    "railway": 0.05, "metro": 0.05, "transmission": 0.08,
}
QUALITY_DINGS = {
    "lowest bidder": -0.10, "l1": -0.05,
    "road": -0.05, "civil work": -0.08, "building construction": -0.08,
    "subject to": -0.05, "tentative": -0.10, "estimated": -0.05,
}

# If any of these appear, it is NOT a fresh order — classify separately
# and never fire a new-order alert.
AMENDMENT_MARKERS = [
    "cancellation", "cancelled", "termination", "terminated", "foreclosure",
    "withdrawal of order", "revision in order", "revised order value",
    "amendment to", "amended order", "short closure", "short-closure",
    "deferment", "put on hold", "rescinded", "clarification on",
    "corrigendum", "reduction in order",
]

# Filing topics that are never an order received. Matched against the exchange
# subcategory, the headline and the PDF's own "Sub:" line -- never the body,
# where a genuine order filing may well mention last quarter's results.
# Observed: a reply to the exchange about a results discrepancy, an annual
# report, an investor presentation and a working-capital loan agreement were
# all stored as orders.
NOT_ORDER_TOPICS = [
    "financial result", "results for the quarter", "limited review",
    "review report", "discrepanc", "annual report", "investor presentation",
    "analyst", "institutional investor", "con. call", "conference call",
    "earnings call", "credit rating", "raising of funds", "rights issue",
    "preferential issue", "trading window", "postal ballot", "registered office",
    "working capital", "loan agreement", "newspaper publication",
    "shareholding pattern",
]

RELATED_PARTY_MARKERS = [
    "wholly owned subsidiary", "holding company", "group company",
    "associate company", "related party", "promoter group",
]

# --------------------------------------------------------------------------
# Filings that use the word "order" but are not commercial order wins.
#
# In Indian filings "order" routinely means a regulatory, tax or court order
# -- an observed false positive was headlined "Intimation of Receipt of an
# Order from BSE", i.e. a directive from the exchange. Credit-rating releases
# are the other big class: they quote a large rupee figure that is the rated
# bank facility, not an order (a Rs 2,516 Cr "order" from "CRISIL Ratings
# Limited" was scored into alert range).
#
# IMPORTANT: these are matched against the HEADLINE and SUBCATEGORY only,
# never the PDF body. A genuine order-win PDF often mentions the company's
# credit rating or a pending tax matter in the boilerplate, and matching on
# the body would silently drop real orders.
# --------------------------------------------------------------------------
NON_ORDER_MARKERS = [
    # regulator / exchange / court / insolvency
    "order from bse", "order from nse", "order from sebi",
    "order from the exchange", "order from the stock exchange",
    "adjudication", "adjudicating officer", "sebi order", "show cause",
    "penalty order", "penalty notice", "nclt", "nclat", "sat order",
    "insolvency", "liquidation", "moratorium", "resolution professional",
    "high court", "supreme court", "tribunal", "court order",
    "arbitration award", "arbitral award", "recovery order",
    "attachment order", "garnishee", "debarment", "freezing order",
    # tax
    "assessment order", "reassessment", "income tax", "tax demand",
    "demand order", "demand notice", "refund order",
    # credit rating -- the rupee figure is a rated facility, not an order
    "credit rating", "rating action", "rating assigned", "rating revision",
    "revision in rating", "reaffirm", "icra", "crisil", "care ratings",
    "india ratings", "brickwork", "acuite", "infomerics",
    # market chatter about an order, not the order itself
    "rumour verification", "rumor verification", "regulation 30(11)",
    "clarification sought", "price movement", "newspaper publication",
]

NON_ORDER_PATTERNS = [
    # "order passed by the Hon'ble ..." / "orders passed by SEBI"
    # (plural matters here too -- BSE's own subcategory is
    #  "Action(s) taken or orders passed")
    r"\borders?\s+(?:passed|issued|received|served)\s+(?:by|from)\s+"
    r"(?:the\s+)?(?:hon|sebi|bse|nse|nclt|nclat|cci|rbi|dgft|commissioner|"
    r"assessing|adjudicat|income[-\s]?tax|gst|customs|excise|tribunal|court)",
    # a rating agency as the counterparty
    r"\b(?:icra|crisil|care|india|acuite|infomerics)\s+ratings?\b",
]

# --------------------------------------------------------------------------
# The PDF probe.
#
# Plenty of genuine order wins are filed under a headline that says nothing:
# BSE's "General Updates", "Updates" and "Press Release" carry the actual
# news only inside the attachment. Keyword-matching the headline and body
# therefore misses them entirely -- and because the PDF is only fetched for
# filings already judged order-like, nothing ever looks inside.
#
# The probe fetches the PDF for these and re-runs classification on its text.
# It is enabled for live scans (a 30-minute window costs ~9 extra PDFs) and
# disabled during backfill, where it would mean ~412 extra PDFs per day.
#
# NEVER_ORDER_HEADLINES is checked first so the filing types that can never
# be an order win cost nothing at all.
# --------------------------------------------------------------------------
GENERIC_HEADLINES = [
    "general update", "updates", "press release", "announcement",
    "disclosure", "intimation", "corporate announcement", "other",
    "submission", "regulation 30", "reg. 30", "reg 30",
]

NEVER_ORDER_HEADLINES = [
    "analyst", "institutional investor", "shareholders meeting", "newspaper",
    "board meeting", "credit rating", "esop", "esos", "esps",
    "price movement", "spurt in volume", "resignation", "appointment",
    "trading window", "annual report", "agm", "egm", "postal ballot",
    "dividend", "buyback", "share transfer", "compliance certificate",
    "shareholding pattern", "financial result", "investor presentation",
    "earnings call", "conference call", "duplicate share", "loss of share",
    "sub-division", "record date", "book closure", "allotment", "warrant",
    "pledge", "encumbrance", "related party transaction", "secretarial",
    "auditor", "cessation", "regulation 74", "reg. 74", "certificate under",
    "monitoring agency", "utilisation of funds", "rumour", "rumor",
    "clarification", "change in management", "change in director",
    "insolvency resolution", "amalgamation", "merger", "corrigendum",
]

# --------------------------------------------------------------------------
# The company's OWN project is not an order received.
#
# Observed: Accuracy Shipping's subsidiary received a Letter of Intent from
# the CBIC -- a tax authority -- to set up a Container Freight Station it
# would own and operate. The filing quotes Rs 25 crore of capital expenditure
# and Rs 175-200 crore of revenue "expected at peak operations and full
# utilization". The extractor called it a Rs 187.50 Cr order: the midpoint of
# a forward-looking revenue projection for the company's own capex project.
#
# A blanket rule on the counterparty will not work -- government bodies,
# ports and municipal corporations place plenty of real orders. The reliable
# signal is the direction of the money: here the company is the one SPENDING
# and BUILDING, not supplying.
#
# Matched over the FULL text (PDF included), because that is where the capex
# language lives. A threshold of two matches is the safety margin: a genuine
# EPC order to build something for a customer may trip the licence pattern
# alone, but it will not also talk about its own capital expenditure being
# funded through internal accruals.
# --------------------------------------------------------------------------
OWN_PROJECT_PATTERNS = [
    # a permission to build something the company will own -- note that
    # "letter of award" is deliberately absent, being the firmest order type
    r"\b(?:letter of intent|loi|lol|in-principle approval|approval|licen[cs]e|"
    r"permission|allotment|concession|clearance)\b[^.\n]{0,90}?\bfor\b"
    r"[^.\n]{0,40}?\b(?:setting up|set up|establishment|establishing|"
    r"development|construction|installation|commissioning)\b",
    r"\bestimated capital expenditure\b",
    r"\bcapital expenditure of\b",
    r"\bcapex of\b",
    r"\bfunded (?:through|by)\b[^.\n]{0,70}?"
    r"\b(?:debt|internal accruals|internal resources|equity|ipo proceeds)\b",
    r"\b(?:capacity|strategic|brownfield|greenfield)\s+expansion\b",
    r"\bexpansion of (?:its|our|the compan(?:y|y's))\b",
    r"\bexpected to generate revenue\b",
    r"\bat peak (?:operations|utilisation|utilization|capacity)\b",
    r"\bfull (?:utilisation|utilization)\b",
    r"\bcommencement of commercial (?:production|operations)\b",
    r"\bown (?:plant|facility|unit|manufacturing|premises)\b",
    r"\bproposed (?:plant|facility|unit|project)\b",
]

# How many of the above must fire before a filing is treated as the company's
# own project rather than an order received.
OWN_PROJECT_MIN_SIGNALS = 2

# Customer candidates the regex sometimes grabs that are not company names.
# "Company having Ind..." was extracted as the customer for a real filing.
JUNK_CUSTOMER_STARTS = (
    "the compan", "compan", "a compan", "one of", "its ", "our ", "the said",
    "the customer", "the client", "the above", "the same", "an entity",
    "a customer", "a client", "a party", "the party", "us ", "we ", "time to",
    "such ", "this ", "that ", "which ", "whom ", "them ", "various ",
    "certain ", "other ", "multiple ", "domestic ", "overseas custom",
)
