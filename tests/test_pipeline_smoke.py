"""End-to-end smoke test with the network stubbed out.

Proves: dedupe, company merge, extraction, fundamentals fallback, scoring,
storage and the dashboard query all work together against a real SQLite file.
"""
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="order-scanner-test-")
os.environ["ORDER_SCANNER_DATA"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from order_scanner import pipeline  # noqa: E402
from order_scanner.config import CONFIG  # noqa: E402
from order_scanner.db import connect, now_ist  # noqa: E402

CONFIG.llm_fallback = False       # no API calls in the smoke test
CONFIG.download_pdfs = False      # no network
CONFIG.telegram_token = ""        # no alerts
CONFIG.fetch_sectors = False      # no sector lookups

NOW = now_ist()

FAKE = [
    {"exchange": "BSE", "native_id": "N1", "symbol": None, "bse_code": "500001",
     "company_name": "Alpha Infra Ltd", "isin": "INE000A01001", "industry": "Construction",
     "category": "Company Update", "subcategory": "Award of Order / Receipt of Order",
     "headline": "Receipt of Letter of Award from NTPC Limited",
     "body": ("The Company has received a Letter of Award (LoA) from NTPC Limited "
              "for balance of plant works. The order value is Rs. 420.50 Crore "
              "(excluding GST), to be executed within 24 months from the date of LoA."),
     "pdf_url": None, "filed_at": NOW - timedelta(minutes=5),
     "disseminated_at": NOW - timedelta(minutes=4), "is_sme": False,
     "targeted_order_subcat": True, "raw": {}},

    {"exchange": "NSE", "native_id": "N2", "symbol": "BETAENG",
     "company_name": "Beta Engineering Ltd", "isin": "INE000B01002",
     "industry": "Capital Goods", "category": "General",
     "subcategory": "Award of order", "headline": "Bags export order",
     "body": ("Beta Engineering has secured an export order worth USD 4.2 Million "
              "from a customer in Germany for supply of precision components, "
              "to be executed over a period of 18 months."),
     "pdf_url": None, "filed_at": NOW - timedelta(minutes=9),
     "disseminated_at": NOW - timedelta(minutes=9), "is_sme": False, "raw": {}},

    {"exchange": "NSE", "native_id": "N3", "symbol": "GAMMAQ",
     "company_name": "Gamma Quarterly Ltd", "isin": "INE000C01003",
     "industry": "IT", "category": "Board Meeting",
     "subcategory": "Board Meeting Intimation",
     "headline": "Board Meeting Intimation",
     "body": "Board will consider unaudited results for the quarter ended June 30, 2026.",
     "pdf_url": None, "filed_at": NOW - timedelta(minutes=12),
     "disseminated_at": NOW - timedelta(minutes=12), "is_sme": False, "raw": {}},

    {"exchange": "BSE", "native_id": "N4", "bse_code": "500004", "symbol": None,
     "company_name": "Delta Cancel Ltd", "isin": "INE000D01004", "industry": "Power",
     "category": "Company Update", "subcategory": "Award of Order / Receipt of Order",
     "headline": "Cancellation of earlier work order",
     "body": ("Intimation regarding cancellation of the work order of Rs 150 crore "
              "received earlier from the customer."),
     "pdf_url": None, "filed_at": NOW - timedelta(minutes=15),
     "disseminated_at": NOW - timedelta(minutes=15), "is_sme": False,
     "targeted_order_subcat": True, "raw": {}},
]

FUNDS = {
    "INE000A01001": {"q_revenue_inr": 150e7, "ttm_revenue_inr": 560e7,
                     "ebitda_margin": 0.115, "market_cap_inr": 1400e7,
                     "source": "stub", "latest_quarter": "2026-06-30"},
    "INE000B01002": {"q_revenue_inr": 95e7, "ttm_revenue_inr": 380e7,
                     "ebitda_margin": 0.21, "market_cap_inr": 2200e7,
                     "source": "stub", "latest_quarter": "2026-06-30"},
    "INE000D01004": {"q_revenue_inr": 80e7, "ttm_revenue_inr": 310e7,
                     "ebitda_margin": 0.13, "market_cap_inr": 700e7,
                     "source": "stub", "latest_quarter": "2026-06-30"},
}

pipeline.collect = lambda *a, **k: [dict(r) for r in FAKE]
pipeline.nse_src.NSEClient = lambda *a, **k: None
pipeline.bse_src.BSEClient = type("S", (), {"s": None})
pipeline.fund_src.resolve = lambda conn, company, **k: FUNDS.get(company["company_id"])


def main():
    print(f"\ndata dir: {TMP}")
    stats = pipeline.run_scan(force=True, dry_run=True)
    print("run 1:", {k: v for k, v in stats.items() if k != "errors"})
    if stats.get("errors"):
        print("ERRORS:", stats["errors"])

    stats2 = pipeline.run_scan(force=True, dry_run=True)
    print("run 2 (should find 0 new):",
          {k: v for k, v in stats2.items() if k != "errors"})

    checks = []
    with connect() as conn:
        anns = conn.execute("SELECT COUNT(*) c FROM announcements").fetchone()["c"]
        orders = [dict(r) for r in conn.execute(
            "SELECT * FROM orders ORDER BY score DESC")]
        print(f"\nstored: {anns} announcements, {len(orders)} orders")
        print(f"{'company':<26}{'value(Cr)':>11}{'Q%':>7}{'ann%':>7}"
              f"{'score':>7}{'tier':>8}  {'type':<11}{'customer'}")
        for o in orders:
            print(f"{o['company_name']:<26}"
                  f"{(o['order_value_inr'] or 0) / 1e7:>11.2f}"
                  f"{(o['ratio_to_quarter'] or 0):>7.0%}"
                  f"{(o['annualised_ratio'] or 0):>7.0%}"
                  f"{o['score']:>7.1f}{o['tier']:>8}  "
                  f"{(o['order_type'] or ''):<11}{o['customer'] or '—'}")

        by = {o["company_name"]: o for o in orders}
        checks = [
            ("all 4 announcements stored", anns == 4),
            ("board meeting not stored as an order",
             "Gamma Quarterly Ltd" not in by),
            ("3 order rows extracted", len(orders) == 3),
            ("re-run found nothing new (dedupe works)",
             stats2["n_order_like"] == 0),
            ("Alpha order value parsed as Rs 420.50 Cr",
             abs(by["Alpha Infra Ltd"]["order_value_inr"] - 420.5e7) < 1e5),
            ("Alpha execution period parsed as 24 months",
             by["Alpha Infra Ltd"]["execution_months"] == 24),
            ("Alpha customer identified as PSU",
             by["Alpha Infra Ltd"]["customer_type"] == "psu"),
            ("Alpha alerts", by["Alpha Infra Ltd"]["tier"] in ("HIGH", "MEDIUM")),
            ("Beta USD converted to INR",
             by["Beta Engineering Ltd"]["order_value_inr"] > 30e7),
            ("Beta flagged as export",
             by["Beta Engineering Ltd"]["customer_type"] == "export"),
            ("Delta cancellation flagged as amendment",
             by["Delta Cancel Ltd"]["is_amendment"] == 1),
            ("Delta cancellation does not alert",
             by["Delta Cancel Ltd"]["tier"] in ("STORE", "WATCH")),
            ("run bookkeeping written",
             conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"] == 2),
            # the hourly task: a PC that was off catches up from the last run
            ("scheduled scan resumes from the end of the last run",
             (pipeline._catch_up_start(now_ist())
              + timedelta(minutes=CONFIG.overlap_minutes)).isoformat()
             == conn.execute("SELECT MAX(window_to) w FROM runs").fetchone()["w"]),
            ("fundamentals snapshotted onto the order row",
             by["Alpha Infra Ltd"]["ttm_revenue_inr"] == 560e7),
        ]

    # Exercise the dashboard over real HTTP, not by calling the handler
    # directly (which would leave FastAPI's Query defaults unresolved).
    try:
        from fastapi.testclient import TestClient

        from order_scanner.dashboard import app
        client = TestClient(app)

        all_rows = client.get("/api/orders", params={"days": 3650}).json()
        high = client.get("/api/orders",
                          params={"days": 3650, "tier": "HIGH"}).json()
        amd = client.get("/api/orders",
                         params={"days": 3650, "include_amendments": True}).json()
        search = client.get("/api/orders",
                            params={"days": 3650, "q": "Alpha"}).json()
        ratio = client.get("/api/orders",
                           params={"days": 3650, "min_ratio": 2.0}).json()
        stats_resp = client.get("/api/stats").json()
        page = client.get("/")

        checks += [
            ("dashboard /api/orders returns rows", len(all_rows) == 2),
            ("dashboard tier filter works",
             len(high) == 1 and high[0]["company_name"] == "Alpha Infra Ltd"),
            ("dashboard hides amendments by default, shows them on request",
             len(amd) == 3),
            ("dashboard company search works",
             len(search) == 1 and search[0]["company_name"] == "Alpha Infra Ltd"),
            ("dashboard ratio filter works", len(ratio) == 1),
            ("dashboard joins the announcement headline",
             all_rows[0].get("headline") is not None),
            ("dashboard /api/stats counts non-amendment orders only",
             stats_resp["totals"]["n_orders"] == 2
             and stats_resp["totals"]["n_high"] == 1),
            ("dashboard page renders", page.status_code == 200
             and "Order Scanner" in page.text),
        ]
    except Exception as exc:
        checks.append((f"dashboard query failed: {exc}", False))

    print(f"\n{'CHECKS':-^78}")
    failed = 0
    for label, ok in checks:
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    print(f"\n{len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
