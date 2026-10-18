"""Order book, end to end, with the network stubbed out.

Proves: a scan reads the order book out of an order-win release from text in
hand; opens a results release's PDF (through the stubbed downloader) and reads
the book there; does NOT open PDFs for untracked companies or non-candidate
filings; never writes those PDFs to the cache; and that the Orderbook API
builds growth, revenue ratio and wins from it.
"""
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="orderbook-smoke-")
os.environ["ORDER_SCANNER_DATA"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from order_scanner import orderbook as ob  # noqa: E402
from order_scanner import pipeline  # noqa: E402
from order_scanner.config import CONFIG, PDF_CACHE  # noqa: E402
from order_scanner.db import connect, now_ist, upsert  # noqa: E402

CONFIG.llm_fallback = False
CONFIG.telegram_token = ""
CONFIG.fetch_sectors = False
CONFIG.fetch_company_meta = False
CONFIG.fetch_annuals = False

NOW = now_ist()
OPENED: list[str] = []

RESULTS_PDF_TEXT = ("Press Release. Alpha Infra Ltd announces results for the quarter ended "
                    "September 30, 2026. As of September 30, 2026, the Company's unexecuted "
                    "order book stood at Rs. 1,250 crore, up 25% year on year.")


def fake_pdf_text(session, url, kind):
    OPENED.append(url)
    return (RESULTS_PDF_TEXT if "results" in url else "Nothing about orders here."), None


def rec(native, name, isin, cat, sub, headline, body, pdf, minutes, target=False, nse=None):
    return {"exchange": "BSE", "native_id": native, "symbol": nse, "bse_code": native,
            "company_name": name, "isin": isin, "industry": "Construction",
            "category": cat, "subcategory": sub, "headline": headline, "body": body,
            "pdf_url": pdf, "filed_at": NOW - timedelta(minutes=minutes),
            "disseminated_at": NOW - timedelta(minutes=minutes), "is_sme": False,
            "targeted_order_subcat": target, "raw": {}}


FAKE = [
    # 1. an order win that restates the book: read from text in hand
    rec("100001", "Alpha Infra Ltd", "INE000A01001", "Company Update",
        "Award of Order / Receipt of Order", "Receipt of LoA from NHAI",
        "The Company has received a Letter of Award from NHAI worth Rs. 210 Crore, to be "
        "executed within 24 months. The Company's total unexecuted order book stood at "
        "approximately Rs. 1,000 Crore as on 30th June 2026.", None, 30, target=True),
    # 2. a results release for the same (now tracked) company: PDF is opened
    rec("100002", "Alpha Infra Ltd", "INE000A01001", "Company Update",
        "Press Release / Media Release", "Press Release", "Press release on results",
        "https://example.test/results-alpha.pdf", 20),
    # 3. an investor presentation for a company with no order on record: skipped
    rec("100003", "Gamma Untracked Ltd", "INE000C01003", "Company Update",
        "Investor Presentation", "Investor Presentation", "", "https://example.test/results-gamma.pdf", 15),
    # 4. a board meeting intimation for the tracked company: not a candidate
    rec("100004", "Alpha Infra Ltd", "INE000A01001", "Board Meeting", "Board Meeting",
        "Board Meeting Intimation", "to consider results", "https://example.test/bm.pdf", 10),
]

pipeline.collect = lambda *a, **k: [dict(r) for r in FAKE]
pipeline.nse_src.NSEClient = lambda *a, **k: None
pipeline.bse_src.BSEClient = type("S", (), {"s": None})
pipeline.fund_src.resolve = lambda conn, company, **k: None
pipeline.ob._pdf_text = fake_pdf_text
pipeline.download_pdf = lambda *a, **k: None       # the order PDF probe: nothing


def main():
    checks = []
    # The tracked set is computed at the start of a scan, so a company's first
    # order makes it tracked from the next scan on -- run two, as the
    # scheduler would.
    s1 = pipeline.run_scan(force=True, dry_run=True)
    FAKE[1]["native_id"] = "100002b"            # a fresh filing on the second scan
    s2 = pipeline.run_scan(force=True, dry_run=True)

    with connect() as conn:
        snaps = [dict(r) for r in conn.execute(
            "SELECT o.*, c.name FROM orderbook o JOIN companies c USING(company_id) ORDER BY as_of")]
        for s in snaps:
            print(f"  snapshot: {s['name']:<18} {s['as_of']} ({s['as_of_basis']}) "
                  f"{s['order_book_inr'] / 1e7:,.0f} Cr  {s['source_kind']}")
        alpha = [s for s in snaps if s["name"].startswith("Alpha")]
        checks += [
            ("book read from the order-win text in hand, with its stated date",
             any(abs(s["order_book_inr"] - 1000e7) < 1e5 and s["as_of"].endswith("06-30")
                 for s in alpha)),
            ("results PDF opened and its book read, dated to the quarter end",
             any(abs(s["order_book_inr"] - 1250e7) < 1e5 and s["as_of"].endswith("09-30")
                 for s in alpha)),
            ("untracked company's presentation not opened",
             not any("gamma" in u for u in OPENED)),
            ("board meeting PDF not opened", not any("bm.pdf" in u for u in OPENED)),
            ("order-book reads counted in run stats", s2.get("n_orderbook_reads", 0) >= 1),
            ("nothing written to the PDF cache",
             not any(PDF_CACHE.glob("*.pdf")) if PDF_CACHE.exists() else True),
        ]

        # Revenue for the ratio, as company_meta would store it.
        cid = alpha[0]["company_id"]
        upsert(conn, "fundamentals", {"company_id": cid, "last_fy": 2026,
                                      "last_fy_revenue_inr": 500e7,
                                      "revenue_basis": "consolidated"}, "company_id")
        conn.execute("UPDATE companies SET market_cap_inr=? WHERE company_id=?", (2000e7, cid))

        # Recheck is idempotent: same snapshots after a replay.
        before = conn.execute("SELECT COUNT(*) FROM orderbook").fetchone()[0]
        ob.recheck(conn)
        after = conn.execute("SELECT COUNT(*) FROM orderbook").fetchone()[0]
        checks.append(("recheck does not duplicate snapshots", after == before))

    from fastapi.testclient import TestClient
    from order_scanner.dashboard import app
    client = TestClient(app)
    data = client.get("/api/orderbook", params={"period": "1y"}).json()
    row = next((r for r in data["rows"] if r["name"].startswith("Alpha")), None)
    detail = client.get(f"/api/orderbook/{row['company_id']}").json() if row else {}
    page = client.get("/orderbook")
    checks += [
        ("API returns the company", row is not None),
        ("latest book is the results figure", row and abs(row["order_book"] - 1250e7) < 1e5),
        ("growth from 1,000 to 1,250 = +25%", row and row["growth"] is not None
         and abs(row["growth"] - 0.25) < 1e-6),
        ("book / revenue = 2.5x against FY2026 revenue",
         row and abs(row["book_to_revenue"] - 2.5) < 1e-6 and row["revenue_label"] == "FY2026, consolidated"),
        ("market cap passed through", row and abs(row["market_cap"] - 2000e7) < 1),
        ("order wins in the period counted", row and row["wins_count"] == 1
         and abs(row["wins_value"] - 210e7) < 1e5),
        ("detail lists every statement with its quote",
         len(detail.get("snapshots", [])) >= 2 and all(s.get("quote") for s in detail["snapshots"])),
        ("orderbook page renders with the view switch",
         page.status_code == 200 and 'href="/orderbook" aria-current="page"' in page.text),
        ("orders page links to the orderbook view",
         'href="/orderbook"' in client.get("/").text),
    ]

    print(f"\n{'CHECKS':-^78}")
    failed = 0
    for label, ok in checks:
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    print(f"\n{len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
