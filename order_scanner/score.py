"""The composite order score.

Why not just "order >= 50% of last quarter's revenue"?

That rule treats a Rs 500 Cr order executable over 5 years the same as one
executable over 6 months, even though the first adds Rs 100 Cr/yr of revenue
and the second Rs 1,000 Cr/yr annualised. It also ignores whether the order
is firm (an LOA) or aspirational (an MoU), whether the company announces an
order every other week, and whether the order is so large relative to the
company's market cap that it should raise suspicion rather than excitement.

So the 50% test survives as one component (worth 20 of 100 points), and five
other components carry the rest:

    A  annualised_impact  40   annualised order value / TTM revenue
    B  quarterly_ratio    20   order value / latest quarterly revenue  <- your rule
    C  firmness           15   LOA/work order > LOI > L1 > MoU
    D  margin_quality     10   company EBITDA margin + order-mix keywords
    E  novelty            10   vs this company's own trailing order history
    F  capacity            5   plausibility given company size

Size decides whether an order matters; C-F only decide how much. Those four
are multiplied by a materiality gate between 0 and 1, read off the order's
size against revenue, so a trivial order cannot score on being firm, novel
or from a high-margin company (see `Materiality` in config.py).

Management guidance in the same filing -- "we expect FY27 total income of
Rs 105 Cr" -- is scored separately on the growth it implies and combined with
the order score (see `_guidance`).

Then multiplicative penalties for pump-shaped filings, related-party
customers and stale news.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import CONFIG
from .db import IST, now_ist


@dataclass
class ScoreResult:
    score: float = 0.0
    tier: str = "STORE"
    data_quality: str = "good"
    components: dict = field(default_factory=dict)
    penalties: dict = field(default_factory=dict)
    ratio_to_quarter: float | None = None
    ratio_to_ttm: float | None = None
    annualised_ratio: float | None = None
    materiality: dict = field(default_factory=dict)
    guidance: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)

    def breakdown_json(self) -> str:
        return json.dumps({
            "components": self.components,
            "penalties": self.penalties,
            "ratios": {
                "to_quarter": self.ratio_to_quarter,
                "to_ttm": self.ratio_to_ttm,
                "annualised": self.annualised_ratio,
            },
            "materiality": self.materiality,
            "guidance": self.guidance,
            "reasons": self.reasons,
        }, default=float)


def _log_curve(x: float, lo: float, hi: float) -> float:
    """Map x in [lo, hi] to [0, 1] on a log scale, clamped.

    Log because the difference between a 2% and a 10% revenue impact matters
    far more than the difference between 100% and 108%.
    """
    if x is None or x <= 0:
        return 0.0
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    return math.log(x / lo) / math.log(hi / lo)


# ---------------------------------------------------------------- components
def _annualised_impact(order_value, ttm_revenue, execution_months) -> tuple[float, dict]:
    """Component A. The single most informative number in the whole model."""
    if not order_value or not ttm_revenue or ttm_revenue <= 0:
        return 0.0, {"reason": "missing order value or TTM revenue"}

    # Unstated execution period: assume 18 months. Most listed-company order
    # wins in India are 12-24 month jobs; 18 is the conservative middle.
    months = execution_months or 18.0
    months = max(3.0, min(120.0, months))
    years = months / 12.0

    annualised = order_value / years
    ratio = annualised / ttm_revenue
    # 1% of TTM revenue = floor, 60% = full marks.
    frac = _log_curve(ratio, 0.01, 0.60)
    return frac, {
        "annualised_inr": annualised,
        "execution_months": months,
        "execution_assumed": execution_months is None,
        "ratio": ratio,
    }


def _quarterly_ratio(order_value, q_revenue) -> tuple[float, dict]:
    """Component B. Your original rule, on a curve rather than a cliff."""
    if not order_value or not q_revenue or q_revenue <= 0:
        return 0.0, {"reason": "missing order value or quarterly revenue"}
    ratio = order_value / q_revenue
    # 10% of a quarter = floor, 300% = full marks. Your 50% lands at ~0.47.
    frac = _log_curve(ratio, 0.10, 3.00)
    return frac, {"ratio": ratio}


def _firmness(order) -> tuple[float, dict]:
    """Component C. An MoU is not an order."""
    frac = float(order.get("firmness", 0.5) or 0.5)
    if order.get("is_amendment"):
        frac = 0.0
    return frac, {"order_type": order.get("order_type"), "firmness": frac}


def _margin_quality(ebitda_margin, quality_adj) -> tuple[float, dict]:
    """Component D. Revenue you keep beats revenue you book."""
    if ebitda_margin is None:
        base = 0.45  # neutral prior
        note = "ebitda margin unknown, neutral prior"
    else:
        # 0% margin -> 0, 25%+ -> 1. Indian EPC sits at 8-12%, products 18-25%.
        base = max(0.0, min(1.0, ebitda_margin / 0.25))
        note = f"ebitda margin {ebitda_margin:.1%}"
    frac = max(0.0, min(1.0, base + (quality_adj or 0.0)))
    return frac, {"base": base, "keyword_adj": quality_adj, "note": note}


def _novelty(order_value, history: list[float]) -> tuple[float, dict]:
    """Component E. Paid for by the historical table.

    A company that announces a Rs 20 Cr order every fortnight should not get
    the same excitement for its 30th one as a company announcing its first.
    """
    if not order_value:
        return 0.0, {"reason": "no order value"}
    if not history:
        return 0.9, {"reason": "first order on record for this company",
                     "n_history": 0}
    median = statistics.median(history)
    if median <= 0:
        return 0.7, {"reason": "degenerate history", "n_history": len(history)}
    mult = order_value / median
    # 0.5x median -> 0, 5x median -> 1
    frac = _log_curve(mult, 0.5, 5.0)
    # A serial announcer gets a small extra haircut.
    if len(history) >= 12:
        frac *= 0.85
    return frac, {"median_prior_order_inr": median, "multiple_of_median": mult,
                  "n_history": len(history)}


def _capacity(order_value, ttm_revenue, market_cap) -> tuple[float, dict]:
    """Component F. Can they actually deliver this?

    Deliberately non-monotonic: a huge order relative to revenue scores well
    on A and B, but if the company has no visible ability to execute it, that
    is a risk, not a bonus.
    """
    if not order_value:
        return 0.0, {"reason": "no order value"}
    info = {}
    frac = 0.6
    if ttm_revenue and ttm_revenue > 0:
        x = order_value / ttm_revenue
        info["order_to_ttm"] = x
        if x <= 1.0:
            frac = 1.0          # comfortably within their run-rate
        elif x <= 3.0:
            frac = 0.7          # stretchy but plausible
        elif x <= 10.0:
            frac = 0.35         # needs working capital they may not have
        else:
            frac = 0.10         # extraordinary claim
    if market_cap and market_cap > 0:
        info["order_to_mcap"] = order_value / market_cap
    return frac, info


def _materiality(annualised_ratio, quarterly_ratio) -> tuple[float, dict]:
    """The gate on components C-F: is this order big enough to matter?

    Takes whichever lens shows the order as bigger, so a long contract that is
    small per year but large against a quarter still counts, and vice versa.
    """
    m = CONFIG.materiality
    lenses = []
    if annualised_ratio is not None:
        lenses.append((_log_curve(annualised_ratio, m.annualised_floor, m.annualised_full),
                       f"annualised value is {annualised_ratio:.1%} of TTM revenue"))
    if quarterly_ratio is not None:
        lenses.append((_log_curve(quarterly_ratio, m.quarterly_floor, m.quarterly_full),
                       f"order is {quarterly_ratio:.0%} of latest quarterly revenue"))
    if not lenses:
        return m.unknown_gate, {"gate": m.unknown_gate,
                                "basis": "unmeasurable: no order value or revenue data"}
    gate, basis = max(lenses, key=lambda lens: lens[0])
    return gate, {"gate": round(gate, 3), "basis": basis}


def _cr(v: float) -> str:
    return "₹" + f"{v / 1e7:,.2f}".rstrip("0").rstrip(".") + " Cr"


def _guidance(items, fundamentals, filed_at) -> tuple[float, dict]:
    """Forward-looking guidance, scored on the growth it implies over the last
    completed financial year. Returns (points 0-100, info).

    The nearest guided year still ahead is used. Revenue (or total income) is
    compared with last year's sales -- plus other income when the guidance is
    for total income -- and PAT with last year's net profit; each is turned
    into a per-year multiple and mapped on a log curve.
    """
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            items = []
    if not items:
        return 0.0, {}
    from .extract.guidance import fiscal_year_end

    g, f = CONFIG.guidance, fundamentals or {}
    current = fiscal_year_end(filed_at or now_ist())
    future = [i for i in items if (i.get("fy_end") or 0) >= current]
    if not future:
        return 0.0, {}
    fy = min(i["fy_end"] for i in future)
    rev = next((i for i in future if i["fy_end"] == fy and i["metric"] == "revenue"), None)
    pat = next((i for i in future if i["fy_end"] == fy and i["metric"] == "pat"), None)

    base_fy, base_rev, basis = f.get("last_fy"), f.get("last_fy_revenue_inr"), "annual"
    if not (base_fy and base_rev) and f.get("ttm_revenue_inr"):
        base_fy, base_rev, basis = current - 1, f["ttm_revenue_inr"], "TTM"
    years = max(1, fy - base_fy) if base_fy else 1
    name = lambda y: f"FY{y % 100:02d}"
    info = {"fy": name(fy), "base": name(base_fy) if base_fy else None, "basis": basis,
            "horizon_years": fy - current}
    parts, weights, said = [], [], []
    implausible = False

    if rev and base_rev:
        total = "total income" in (rev.get("label") or "")
        base = base_rev + ((f.get("last_fy_other_income_inr") or 0)
                           if total and basis == "annual" else 0)
        if rev.get("low_inr"):
            multiple = rev["low_inr"] / base
            said.append(f"{'total income' if total else 'revenue'} {_cr(rev['low_inr'])} "
                        f"vs {info['base']} {_cr(base)} ({multiple:.1f}×)")
        else:
            multiple = (1 + rev.get("growth_low", 0)) ** years
            said.append(f"revenue growth of {rev.get('growth_low', 0):.0%} a year")
        implausible = multiple > g.implausible_multiple
        parts.append(_log_curve(multiple ** (1 / years), g.revenue_floor, g.revenue_full))
        weights.append(g.revenue_weight)
        info.update(revenue_guided=rev.get("low_inr"), revenue_base=base,
                    revenue_multiple=round(multiple, 2))

    base_pat = f.get("last_fy_pat_inr") if basis == "annual" else None
    if pat and pat.get("low_inr") and base_pat is not None:
        if base_pat > 0:
            pm = pat["low_inr"] / base_pat
            parts.append(_log_curve(pm ** (1 / years), g.pat_floor, g.pat_full))
            said.append(f"PAT at least {_cr(pat['low_inr'])} vs {_cr(base_pat)} ({pm:.1f}×)")
            info["pat_multiple"] = round(pm, 2)
        else:
            parts.append(1.0)                           # a guided profit after a loss
            said.append(f"PAT of {_cr(pat['low_inr'])} after a loss")
        weights.append(1 - g.revenue_weight)
        info.update(pat_guided=pat["low_inr"], pat_base=base_pat)

    if not parts:
        info["summary"] = f"{info['fy']} guidance, with no past year to compare it against"
        return 0.0, info
    share = sum(p * w for p, w in zip(parts, weights)) / sum(weights)
    if not rev:
        share *= g.pat_only_factor
    if implausible:
        share *= g.implausible_factor
    horizon = g.horizon_factors[min(max(fy - current, 0), len(g.horizon_factors) - 1)]
    info.update(implausible=implausible, horizon_factor=horizon,
                summary=f"{info['fy']}: " + "; ".join(said))
    return 100 * share * horizon, info


# ---------------------------------------------------------------- penalties
def _penalties(order_value, market_cap, is_sme, is_related_party,
               filed_at) -> tuple[float, dict]:
    p = CONFIG.penalties
    mult, applied = 1.0, {}

    if order_value and market_cap and market_cap > 0:
        ratio = order_value / market_cap
        if ratio > 5.0:
            mult *= p.order_exceeds_5x_mcap
            applied["order>5x_mcap"] = p.order_exceeds_5x_mcap
        elif is_sme and ratio > 2.0:
            mult *= p.order_exceeds_2x_mcap_sme
            applied["sme_order>2x_mcap"] = p.order_exceeds_2x_mcap_sme

    if is_related_party:
        mult *= p.related_party_customer
        applied["related_party"] = p.related_party_customer

    if filed_at:
        try:
            dt = (filed_at if isinstance(filed_at, datetime)
                  else datetime.fromisoformat(str(filed_at)))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            if now_ist() - dt > timedelta(hours=p.stale_hours):
                mult *= p.stale_announcement
                applied["stale"] = p.stale_announcement
        except (ValueError, TypeError):
            pass

    return mult, applied


# ---------------------------------------------------------------- main entry
def score_order(order: dict, fundamentals: dict | None, company: dict,
                history: list[float]) -> ScoreResult:
    """`order` is the extracted-order dict; `fundamentals` may be None."""
    w = CONFIG.weights
    t = CONFIG.thresholds
    res = ScoreResult()

    value = order.get("order_value_inr")
    q_rev = (fundamentals or {}).get("q_revenue_inr")
    ttm_rev = (fundamentals or {}).get("ttm_revenue_inr")
    if ttm_rev is None and q_rev:
        ttm_rev = q_rev * 4          # crude but better than dropping the order
        res.reasons.append("TTM estimated as 4x latest quarter")
    ebitda = (fundamentals or {}).get("ebitda_margin")
    mcap = (fundamentals or {}).get("market_cap_inr") or company.get("market_cap_inr")

    res.ratio_to_quarter = value / q_rev if value and q_rev else None
    res.ratio_to_ttm = value / ttm_rev if value and ttm_rev else None

    fa, ia = _annualised_impact(value, ttm_rev, order.get("execution_months"))
    fb, ib = _quarterly_ratio(value, q_rev)
    fc, ic = _firmness(order)
    fd, idd = _margin_quality(ebitda, order.get("quality_adj"))
    fe, ie = _novelty(value, history)
    ff, if_ = _capacity(value, ttm_rev, mcap)

    res.annualised_ratio = ia.get("ratio")

    # Size decides whether the order matters; C-F only decide how much.
    gate, im = _materiality(res.annualised_ratio, res.ratio_to_quarter)
    res.materiality = im
    if gate < 1.0:
        res.reasons.append(f"materiality gate {gate:.0%}: {im['basis']}")

    res.components = {
        "annualised_impact": {"points": round(fa * w.annualised_impact, 2),
                              "max": w.annualised_impact, **ia},
        "quarterly_ratio": {"points": round(fb * w.quarterly_ratio, 2),
                            "max": w.quarterly_ratio, **ib},
        # C-F report the points they actually contributed, after the gate.
        "firmness": {"points": round(fc * w.firmness * gate, 2),
                     "max": w.firmness, **ic},
        "margin_quality": {"points": round(fd * w.margin_quality * gate, 2),
                           "max": w.margin_quality, **idd},
        "novelty": {"points": round(fe * w.novelty * gate, 2),
                    "max": w.novelty, **ie},
        "capacity": {"points": round(ff * w.capacity * gate, 2),
                     "max": w.capacity, **if_},
    }
    for k in ("firmness", "margin_quality", "novelty", "capacity"):
        res.components[k]["materiality_gate"] = round(gate, 3)

    # Firmness also discounts the two SIZE components, not just its own.
    # An MoU's revenue is probabilistic: booking it at face value is how you
    # end up excited about a press release. LOA (1.0) -> no discount;
    # MoU (0.3) -> size components count for ~69%.
    size_discount = 0.55 + 0.45 * fc
    res.components["annualised_impact"]["firmness_discount"] = round(size_discount, 3)
    res.components["quarterly_ratio"]["firmness_discount"] = round(size_discount, 3)

    raw = ((fa * w.annualised_impact + fb * w.quarterly_ratio) * size_discount
           + (fc * w.firmness + fd * w.margin_quality
              + fe * w.novelty + ff * w.capacity) * gate)

    mult, applied = _penalties(
        value, mcap, bool(company.get("is_sme")),
        bool(order.get("is_related_party")), order.get("filed_at"))

    # Believability gate. The pump penalties above only fire when market cap
    # is known, and for the BSE-only smallcaps where this matters most it
    # usually isn't -- so an absurd order/revenue ratio sailed through
    # unpenalised and the always-alert override then promoted it. Treat an
    # implausible ratio as evidence of a bad parse, not of a huge win.
    implausible = (res.ratio_to_quarter is not None
                   and res.ratio_to_quarter > t.max_plausible_quarterly_multiple)
    if implausible:
        mult *= CONFIG.penalties.implausible_quarterly_ratio
        applied["implausible_ratio"] = CONFIG.penalties.implausible_quarterly_ratio

    res.penalties = applied
    # Stale news discounts everything, guidance included; the order-specific
    # penalties (pump pattern, related party, implausible ratio) and the caps
    # below discount only the order's own score.
    stale = applied.get("stale", 1.0)
    score = raw * (mult / stale)

    # Extraction uncertainty shrinks the score toward the middle.
    conf = float(order.get("extraction_conf") or order.get("confidence") or 0.6)
    if conf < 0.6:
        score *= (0.7 + 0.5 * conf)
        res.reasons.append(f"low extraction confidence ({conf:.2f})")

    # Data-quality gate.
    if not value:
        res.data_quality = "low"
        score = min(score, 35.0)
        res.reasons.append("no order value extracted")
    elif not q_rev and not ttm_rev:
        res.data_quality = "low"
        score = min(score, CONFIG.penalties.no_revenue_data_score_cap)
        res.reasons.append("no revenue data; score capped")
    elif not ttm_rev or not order.get("execution_months"):
        res.data_quality = "partial"

    if order.get("is_amendment"):
        score = min(score, 25.0)
        res.reasons.append("amendment/cancellation, not a new order")

    if implausible:
        # Held at WATCH, not suppressed: it still reaches the dashboard and
        # the watchlist digest for a human look, but it cannot auto-alert.
        res.data_quality = "low"
        score = min(score, t.watch)
        res.reasons.append(
            f"implausible: order is {res.ratio_to_quarter:.0f}x last quarter's "
            f"revenue (>{t.max_plausible_quarterly_multiple:.0f}x); "
            "treating as an extraction or revenue error")

    # Forward-looking guidance, scored on its own and combined with the order:
    # the stronger of the two plus a share of the weaker.
    g_points, g_info = _guidance(order.get("guidance"), fundamentals, order.get("filed_at"))
    if g_info:
        res.guidance = g_info
        res.components["guidance"] = {"points": round(g_points, 2), "max": 100, **g_info}
        if g_info.get("implausible"):
            g_points = min(g_points, t.watch)
            res.reasons.append("guidance implausibly large against last year; held at WATCH")
    order_score = score
    score = (max(order_score, g_points)
             + CONFIG.guidance.combine_bonus * min(order_score, g_points)) * stale
    if g_points > order_score:
        res.reasons.append(f"guidance carries the score: {g_info.get('summary')}")
    if order.get("is_amendment"):
        score = min(score, 25.0)

    res.score = round(max(0.0, min(100.0, score)), 2)

    # --- tiering -----------------------------------------------------
    # The floor stops a tiny order alerting on its own ratios; it does not
    # silence guidance that reaches WATCH by itself.
    if value and value < t.min_order_value_inr and g_points < t.watch:
        res.tier = "STORE"
        res.reasons.append("below absolute order-value floor")
    elif res.score >= t.high:
        res.tier = "HIGH"
        # Safety rail: a non-binding commitment is never top tier, however
        # large. MoU / L1 / "preferred bidder" cap out at MEDIUM.
        # Guidance strong enough to reach HIGH on its own is not held back.
        if fc < 0.5 and g_points < t.high:
            res.tier = "MEDIUM"
            res.reasons.append(
                f"capped at MEDIUM: not a firm order ({order.get('order_type')})")
    elif res.score >= t.medium:
        res.tier = "MEDIUM"
    elif res.score >= t.watch:
        res.tier = "WATCH"
    else:
        res.tier = "STORE"

    # Your explicit override: a genuinely huge order relative to the last
    # quarter always surfaces, even if the model is unsure about the rest.
    # It deliberately does NOT fire when a penalty was applied -- the whole
    # point of the pump and related-party penalties is to stop exactly the
    # "order is 100x quarterly revenue" filings this override would promote.
    if (res.tier in ("WATCH", "STORE")
            and not res.penalties
            and res.ratio_to_quarter
            and res.ratio_to_quarter >= t.always_alert_quarterly_multiple
            and value and value >= t.min_order_value_inr
            and not order.get("is_amendment")):
        res.tier = "MEDIUM"
        res.reasons.append(
            f"override: order is {res.ratio_to_quarter:.0%} of last quarter's revenue")

    return res
