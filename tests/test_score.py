"""Scoring sanity checks — does the model rank the way a human analyst would?"""
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from order_scanner.config import CONFIG
from order_scanner.extract.guidance import fiscal_year_end
from order_scanner.db import now_ist
from order_scanner.score import score_order

CR = 1e7


def order(value_cr, months=None, firmness=1.0, order_type="loa",
          related=False, quality=0.0, conf=0.8, amendment=False, age_h=1,
          guidance=None):
    return {
        "order_value_inr": value_cr * CR,
        "execution_months": months,
        "firmness": firmness,
        "order_type": order_type,
        "is_amendment": amendment,
        "is_related_party": related,
        "quality_adj": quality,
        "extraction_conf": conf,
        "filed_at": (now_ist() - timedelta(hours=age_h)).isoformat(),
        "guidance": guidance or [],
    }


def funds(q_cr, ttm_cr=None, margin=0.12, mcap_cr=None,
          fy_rev_cr=None, fy_other_cr=None, fy_pat_cr=None):
    return {"q_revenue_inr": q_cr * CR,
            "ttm_revenue_inr": (ttm_cr * CR) if ttm_cr else None,
            "ebitda_margin": margin,
            "market_cap_inr": (mcap_cr * CR) if mcap_cr else None,
            "last_fy": (FY - 1) if fy_rev_cr else None,
            "last_fy_revenue_inr": (fy_rev_cr * CR) if fy_rev_cr else None,
            "last_fy_other_income_inr": (fy_other_cr * CR) if fy_other_cr else None,
            "last_fy_pat_inr": (fy_pat_cr * CR) if fy_pat_cr is not None else None}


FY = fiscal_year_end(now_ist())           # the financial year in progress


def guide(income_cr, pat_low_cr=None, pat_high_cr=None, fy=None):
    """Guidance items as extract_guidance returns them, for the current FY."""
    items = [{"fy_end": fy or FY, "metric": "revenue", "label": "total income",
              "low_inr": income_cr * CR, "high_inr": None}]
    if pat_low_cr is not None:
        items.append({"fy_end": fy or FY, "metric": "pat", "label": "pat",
                      "low_inr": pat_low_cr * CR,
                      "high_inr": pat_high_cr * CR if pat_high_cr else None})
    return items


VEER_FUNDS = dict(q_cr=24.93, ttm_cr=52.92, margin=0.083, mcap_cr=101,
                  fy_rev_cr=32.48, fy_other_cr=0.49, fy_pat_cr=0.54)


SCENARIOS = [
    ("Small-cap EPC, Rs450Cr LOA over 36 months",
     order(450, 36), funds(160, 600, 0.11, 1200), {}, []),

    ("Same order, 6-month execution",
     order(450, 6), funds(160, 600, 0.11, 1200), {}, []),

    ("Same order, execution period not stated",
     order(450, None), funds(160, 600, 0.11, 1200), {}, []),

    ("Large-cap, Rs450Cr order vs Rs40,000Cr TTM",
     order(450, 18), funds(10000, 40000, 0.18, 60000), {}, []),

    ("Micro-cap 'mega order': Rs800Cr vs Rs30Cr TTM, Rs50Cr mcap",
     order(800, 24), funds(8, 30, 0.09, 50), {"is_sme": 1}, []),

    ("MoU (not firm) Rs500Cr vs Rs300Cr TTM",
     order(500, 24, firmness=0.3, order_type="mou"), funds(80, 300, 0.14, 900), {}, []),

    ("Serial announcer: Rs25Cr, 20 prior orders median Rs22Cr",
     order(25, 12), funds(50, 200, 0.13, 400), {}, [22 * CR] * 20),

    ("First-ever order: Rs25Cr, no history",
     order(25, 12), funds(50, 200, 0.13, 400), {}, []),

    ("High-margin defence export Rs120Cr vs Rs400Cr TTM",
     order(120, 18, quality=0.35), funds(100, 400, 0.22, 2500), {}, []),

    ("Cancellation of a Rs300Cr order",
     order(300, 24, amendment=True, order_type="amendment"),
     funds(100, 400, 0.13, 900), {}, []),

    ("Related-party order Rs200Cr from subsidiary",
     order(200, 18, related=True), funds(90, 350, 0.12, 800), {}, []),

    ("No revenue data available, Rs180Cr LOA",
     order(180, 18), None, {}, []),

    ("Your rule exactly: order = 50% of last quarter, big company",
     order(5000, 18), funds(10000, 40000, 0.18, 60000), {}, []),

    ("Stale: Rs450Cr LOA filed 5 days ago",
     order(450, 12, age_h=120), funds(160, 600, 0.11, 1200), {}, []),

    # materiality: one high-margin company, three order sizes
    ("Tiny firm order: Rs3Cr defence LOA vs Rs400Cr TTM",
     order(3, 12, quality=0.35), funds(100, 400, 0.22, 2500), {}, []),

    ("Modest order: Rs12Cr LOA vs Rs400Cr TTM",
     order(12, 12), funds(100, 400, 0.22, 2500), {}, []),

    ("Material order: Rs60Cr LOA vs Rs400Cr TTM",
     order(60, 12), funds(100, 400, 0.22, 2500), {}, []),

    # guidance: Veerhealth guided FY27 total income Rs105Cr (FY26 Rs32.97Cr) and
    # PAT of at least Rs5Cr (FY26 Rs0.54Cr), on a Rs10.07 lakh sample order + MoU
    ("Guidance: 3.2x income, 9x PAT, Rs10 lakh sample MoU",
     order(0.1007, None, firmness=0.3, order_type="mou", guidance=guide(105, 5, 7)),
     funds(**VEER_FUNDS), {}, []),

    ("Guidance: a 10% growth outlook",
     order(0.1007, None, firmness=0.3, order_type="mou", guidance=guide(36)),
     funds(**VEER_FUNDS), {}, []),

    ("Guidance: 12x income, likely a misread",
     order(0.1007, None, firmness=0.3, order_type="mou", guidance=guide(400)),
     funds(**VEER_FUNDS), {}, []),
]


def main():
    print(f"\n{'SCORING SCENARIOS':-^104}")
    print(f"{'scenario':<52}{'score':>7}{'tier':>9}{'Q%':>8}{'ann%':>8}"
          f"{'quality':>10}  penalties")
    print("-" * 104)
    results = []
    for name, o, f, company, hist in SCENARIOS:
        sr = score_order(o, f, company, hist)
        results.append((name, sr))
        q = f"{sr.ratio_to_quarter:.0%}" if sr.ratio_to_quarter is not None else "—"
        a = f"{sr.annualised_ratio:.0%}" if sr.annualised_ratio is not None else "—"
        pen = ",".join(sr.penalties) or "—"
        print(f"{name:<52}{sr.score:>7.1f}{sr.tier:>9}{q:>8}{a:>8}"
              f"{sr.data_quality:>10}  {pen}")

    # --- invariants a sane model must satisfy -------------------------
    by = {n: s for n, s in results}
    # the same firm, first-ever order at growing sizes against one company
    sweep = [score_order(order(v, 12), funds(100, 400, 0.22, 2500), {}, []).score
             for v in (2, 5, 10, 20, 40, 80)]
    print()
    print(f"size sweep, Rs2-80Cr vs Rs400Cr TTM: {sweep}")

    checks = [
        ("6-month execution outscores 36-month",
         by["Same order, 6-month execution"].score
         > by["Small-cap EPC, Rs450Cr LOA over 36 months"].score),
        ("small-cap beats large-cap for the same absolute order",
         by["Small-cap EPC, Rs450Cr LOA over 36 months"].score
         > by["Large-cap, Rs450Cr order vs Rs40,000Cr TTM"].score),
        ("micro-cap mega-order is penalised, not celebrated",
         "sme_order>2x_mcap" in by["Micro-cap 'mega order': Rs800Cr vs Rs30Cr TTM, Rs50Cr mcap"].penalties
         or "order>5x_mcap" in by["Micro-cap 'mega order': Rs800Cr vs Rs30Cr TTM, Rs50Cr mcap"].penalties),
        ("MoU scores below a firm LOA of similar size",
         by["MoU (not firm) Rs500Cr vs Rs300Cr TTM"].score
         < by["Small-cap EPC, Rs450Cr LOA over 36 months"].score),
        ("serial announcer scores below first-ever order",
         by["Serial announcer: Rs25Cr, 20 prior orders median Rs22Cr"].score
         < by["First-ever order: Rs25Cr, no history"].score),
        ("cancellation never alerts",
         by["Cancellation of a Rs300Cr order"].tier in ("STORE", "WATCH")),
        ("related-party order is discounted",
         "related_party" in by["Related-party order Rs200Cr from subsidiary"].penalties),
        ("missing revenue data is flagged and capped",
         by["No revenue data available, Rs180Cr LOA"].data_quality == "low"),
        ("stale filing is discounted",
         "stale" in by["Stale: Rs450Cr LOA filed 5 days ago"].penalties),
        ("unstated execution period flagged as partial data",
         by["Same order, execution period not stated"].data_quality == "partial"),
        ("MoU never reaches HIGH tier",
         by["MoU (not firm) Rs500Cr vs Rs300Cr TTM"].tier != "HIGH"),
        ("micro-cap pump never alerts above WATCH",
         by["Micro-cap 'mega order': Rs800Cr vs Rs30Cr TTM, Rs50Cr mcap"].tier
         in ("STORE", "WATCH")),
        ("related-party order does not get promoted by the override",
         by["Related-party order Rs200Cr from subsidiary"].tier
         in ("STORE", "WATCH")),
        ("your 50%-of-quarter rule still surfaces on a mega-cap",
         by["Your rule exactly: order = 50% of last quarter, big company"].tier
         in ("HIGH", "MEDIUM")),
        ("an immaterial order scores almost nothing, however firm or novel",
         by["Tiny firm order: Rs3Cr defence LOA vs Rs400Cr TTM"].score < 5),
        ("a large-cap's immaterial order earns no quality points",
         by["Large-cap, Rs450Cr order vs Rs40,000Cr TTM"].score < 10),
        ("a modest order scores below a clearly material one",
         by["Tiny firm order: Rs3Cr defence LOA vs Rs400Cr TTM"].score
         < by["Modest order: Rs12Cr LOA vs Rs400Cr TTM"].score
         < by["Material order: Rs60Cr LOA vs Rs400Cr TTM"].score),
        ("score never falls as the order grows, all else equal",
         all(a <= b for a, b in zip(sweep, sweep[1:]))),
        ("unmeasurable significance stays below WATCH",
         by["No revenue data available, Rs180Cr LOA"].score < CONFIG.thresholds.watch),
        ("materiality is recorded in the breakdown",
         "gate" in json.loads(
             by["Modest order: Rs12Cr LOA vs Rs400Cr TTM"].breakdown_json())["materiality"]),
        ("strong guidance earns HIGH and an alert, even on a tiny MoU order",
         by["Guidance: 3.2x income, 9x PAT, Rs10 lakh sample MoU"].tier == "HIGH"),
        ("a 10% growth outlook adds nothing",
         by["Guidance: a 10% growth outlook"].tier == "STORE"
         and by["Guidance: a 10% growth outlook"].score < 10),
        ("implausible guidance is held at WATCH or below",
         by["Guidance: 12x income, likely a misread"].tier in ("STORE", "WATCH")),
    ]
    print(f"\n{'INVARIANTS':-^104}")
    failed = 0
    for label, ok in checks:
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    print(f"\n{len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
