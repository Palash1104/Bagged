"""Order book extraction, against sentences taken from real NSE/BSE filings.

Each case pins the figure, the date it is stated as of, and whether it is the
company's whole book. The "must NOT read" cases are misreads the extractor
made at some point while it was being tuned on the 405 stored filings that
mention an order book.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ORDER_SCANNER_DATA", tempfile.mkdtemp(prefix="ob-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from order_scanner.extract.orderbook import (best_order_book, classify_source,
                                             find_order_books, quarter_end_before)
from datetime import date

CR = 1e7

# (label, text, filed, kind, want_value_cr | None, want_as_of | None, want_scope | None)
CASES = [
    ("Man Industries: plain 'stands at'",
     "These orders are expected to be delivered within 6 - 9 months. The total unexecuted order "
     "book stands at approximately Rs.4100 Crores (Rupees Four Thousand One Hundred Crores only).",
     "2026-09-10", "order_win", 4100, "2026-09-10", "total"),
    ("Abha Power: date in front of the anchor",
     "As on June 15, 2026, the Company’s order book stands at approximately ₹ 21.50 Cr, showcasing "
     "our strong presence in the railway sector.", "2026-06-16", "order_win", 21.50, "2026-06-15", "total"),
    ("T&R: results release, 'reached a record'",
     "As of June 30, 2026, the Company's unexecuted order book reached a record Rs. 6,630 crore, "
     "representing a 26% year-on-year increase.", "2026-07-20", "results", 6630, "2026-06-30", "total"),
    ("Zen: 'as at 30 June 2026'",
     "We closed the quarter with a consolidated order book of ₹1,239.02 crore as at 30 June 2026 and "
     "subsequently received a further simulator order of ₹177.50 crore in July 2026.",
     "2026-07-26", "results", 1239.02, "2026-06-30", "total"),
    ("Tejas: no date, quarter talk -> quarter end",
     "We ended the quarter with an order book of Rs. 1,529 Cr.", "2026-07-27", "results",
     1529, "2026-06-30", "total"),
    ("Shakti Pumps: Rs Mn",
     "As on 22nd July 2026, the order book stood at approximately Rs. 10,000 Mn", "2026-07-24",
     "results", 1000, "2026-07-22", "total"),
    ("L&T-style bn", "Order book stood at ₹ 21.80 billion as of June 30, 2026, as compared to "
     "₹ 20.74 billion a year ago.", "2026-08-10", "results", 2180, "2026-06-30", "total"),
    ("Ramco: USD converted", "Unexecuted Order Book stood at USD 152.3m, providing visibility for "
     "future execution", "2026-07-23", "results", 152.3 * 88 / 10, None, "total"),
    ("Welspun Corp: company's own rupee figure beats our FX",
     "With this order, the Company's global order book stands at a record USD 4.4 billion "
     "(~₹42,100 Crore) — the highest in its history.", "2026-08-20", "order_win", 42100, None, "total"),
    ("Konstelec: from/to keeps the closing figure",
     "With these additions, our order book has grown from approximately ₹550 crore to nearly "
     "₹560+ crore, providing strong revenue visibility.", "2026-07-28", "order_win", 560, None, "total"),
    ("INOX India: 'taking total order book to'",
     "Order Inflow at ₹ 532 Cr for the quarter, taking total order book to ₹ 1,686 Cr",
     "2026-08-03", "results", 1686, "2026-06-30", "total"),
    ("PDS: 'increasing 23% YoY to'",
     "Built a stronger growth pipeline, with the order book increasing 23% YoY to ₹6,095 crore.",
     "2026-08-07", "results", 6095, "2026-06-30", "total"),
    ("Pace Digitek: slide layout, figure before the label",
     "Manufacturing of BESS & PCS Rs. 11,338 Cr Order book (As of May 25, 2026) 33 Manufacturing",
     "2026-06-22", "presentation", 11338, "2026-05-25", "total"),
    ("Ceigall: INR million", "Our order book stood at INR 185,683 million as on June 30, 2026, "
     "providing strong visibility.", "2026-08-08", "results", 18568.3, "2026-06-30", "total"),
    ("Omfurn: lakh", "Following the receipt of these orders, the Company’s unexecuted order book, "
     "as of July 31, 2026, has increased over ₹17,000 lakh, which is expected to be executed over "
     "the next 24 months.", "2026-08-05", "order_win", 170, "2026-07-31", "total"),
    ("Kalpataru boilerplate: dated to the year end",
     "with a consolidated revenue of ₹ 27,143 crore (US$ 2.9 billion), net profit of ₹ 1,031 crore "
     "(US$ 109 million) and order book of ₹ 65,457 crore (US$ 6.9 billion) for the year ended "
     "March 31, 2026.", "2026-06-30", "order_win", 65457, "2026-03-31", "total"),
    ("Glass Wall: Jul'26 month end", "Orderbook: As of July,’26 is Rs. 982 crores",
     "2026-10-06", "results", 982, "2026-07-31", "total"),
    ("Bluspring: '?' as the rupee sign, subsidiary",
     'Press Release titled "A Bluspring subsidiary, STEAG India s order book stands at ?5,100 Cr '
     'with new deals" dated July 06, 2026', "2026-07-06", "update", 5100, None, "subsidiary"),
    ("Texmaco: division book is a segment",
     "With this new order, the outstanding order book of the Company's Infra Rail & Green Energy "
     "division stands at Rs. 1,159.94 Crores (inclusive of taxes).", "2026-07-02", "order_win",
     1159.94, None, "segment"),
    ("IC Electricals: 'total ... export order book' is still export",
     "With this order, the Company's total unexecuted export order book has increased to "
     "USD 1,800,000 (approximately ₹17.20 Crore).", "2026-07-14", "order_win", 17.20, None, "segment"),
    ("MTAR: nuclear order book", "Our closing nuclear order book now stands at more than 775 Crs, "
     "the highest in the Company’s history.", "2026-08-26", "order_win", 775, None, "segment"),
    ("Sansera: ADS business", "The total unexecuted order backlog for the ADS business stood at "
     "INR 44,368 million as on 30th June 2026", "2026-08-12", "results", 4436.8, "2026-06-30", "segment"),
    ("GPT Infra: 'for the Company' is not a region",
     "The outstanding order book for the Company now stands at ₹ 4,992 Crore with total order "
     "inflow for Fiscal 2027 of ₹ 818 Crore.", "2026-09-17", "order_win", 4992, "2026-09-17", "total"),
    ("Data Patterns: negotiated orders are not the book",
     "Order book position o Including orders received and negotiated as on date - INR 2,654 Cr "
     "o Order Book as on June 30, 2026 - INR 928 Cr o Order book as on June 30, 2025 - INR 730 Cr",
     "2026-07-30", "results", 928, "2026-06-30", "total"),
    ("Vishnu Prakash: backtick as the rupee sign",
     "The order book stood at `43,910.9 Million as on 31st March 2026, spread across water supply",
     "2026-09-08", "order_win", 4391.09, "2026-03-31", "total"),
    ("Dynacons: results dateline is not the as-of date",
     "Order Book at ₹31,040 million Thursday, August 13, 2026", "2026-08-13", "results",
     3104, "2026-06-30", "total"),

    # ---- must NOT read a figure -------------------------------------------
    ("Jupiter Wagons: a target is not a book", 'targeting ₹1,000 crore order book".',
     "2026-08-10", "order_win", None, None, None),
    ("Brahmaputra: a forecast is not a book",
     "the Company is expecting to build its order book to approximately Rs. 2,500 Crores by the "
     "close of the Financial Year 2026-27", "2026-09-04", "update", None, None, None),
    ("INOX India: the figure in front is an order's value",
     "INOX India Ltd secures multiple orders worth ₹939 Cr Current order book boosted by another "
     "‘Mega’ order", "2026-07-08", "order_win", None, None, None),
    ("Ceigall: a win, not the book",
     "Ceigall India Strengthens Order Book with Fifth Win of INR 274 Crore in Arunachal Pradesh",
     "2026-08-19", "order_win", None, None, None),
    ("Interarch: new orders, not the book",
     "Interarch Building Solutions Strengthens Order Book with ₹ 375 Crores of New Orders in June",
     "2026-06-23", "order_win", None, None, None),
    ("Ceinsys: a reduction is not a level",
     "The overall reduction in the value of the pending orders to be executed is Rs.112.42 Crores "
     "with no impact on the revenue already booked.", "2026-08-22", "order_win", None, None, None),
    ("Ashoka Buildcon: table footnote",
     "Power T&D 5,066 33.2% Total Order Book 15,251* 100.0% *Excluding orders received post 30th "
     "June 2026 of Rs. 451 crores", "2026-08-11", "results", None, None, None),
    ("VA Tech Wabag: revenue after the anchor",
     "WABAG records all-time high order book in Q1 FY 2026-27 Historic High results with "
     "Consolidated Revenues at Rs. 8,868 Mn", "2026-08-12", "results", None, None, None),
    ("Emmvee: capacity, not money", "taking its order book to an all-time high of approximately "
     "9.9 GW, providing strong revenue visibility", "2026-07-15", "results", None, None, None),
    ("Adani Energy: meters, not money", "an order book of over 24.6 million meters.",
     "2026-07-21", "results", None, None, None),
    ("Mahindra: the LSE's International Order Book",
     "admitted for trading on International Order Book (IOB) of the London Stock Exchange. "
     "Paid-up Capital Rs. 6,21 crore", "2026-07-04", "other", None, None, None),
    ("Tata Capital: a bond book", "the final order book oversubscribed by 4 times",
     "2026-07-15", "other", None, None, None),
    ("RMC: no figure at all", "The receipt of this LOA further strengthens the Company’s order "
     "book and is expected to contribute positively.", "2026-08-13", "order_win", None, None, None),
]


def test_cases():
    passed = failed = 0
    for label, text, filed, kind, want_cr, want_asof, want_scope in CASES:
        fig = best_order_book(text, filed, kind, min_confidence=0.3)
        if want_cr is None:
            ok = fig is None
            got = "none" if fig is None else f"₹{fig.value_inr / CR:,.2f} Cr"
        else:
            ok = fig is not None and abs(fig.value_inr / CR - want_cr) <= max(0.02, want_cr * 0.002)
            if ok and want_asof:
                ok = fig.as_of == want_asof
            if ok and want_scope:
                ok = fig.scope == want_scope
            got = ("none" if fig is None else
                   f"₹{fig.value_inr / CR:,.2f} Cr as of {fig.as_of} ({fig.as_of_basis}) {fig.scope}")
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {label:<52} {got}")
    return passed, failed


def test_history_from_one_sentence():
    """A from/to statement with both ends dated yields two points of history."""
    text = ("The consolidated order book increased from approximately Rs.2,044 Crore as at 31st "
            "March 2025 to approximately Rs.5,206 Crore as at 31st March 2026.")
    figs = {(round(f.value_inr / CR), f.as_of) for f in find_order_books(text, "2026-09-15", "results")}
    checks = [
        ("closing figure dated by its own date", (5206, "2026-03-31") in figs),
        ("opening figure kept with its own date", (2044, "2025-03-31") in figs),
    ]
    p = f = 0
    for label, ok in checks:
        p, f = (p + 1, f) if ok else (p, f + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    return p, f


def test_helpers():
    checks = [
        ("quarter end before 3 Aug", quarter_end_before(date(2026, 8, 3)) == date(2026, 6, 30)),
        ("quarter end before 1 Jul", quarter_end_before(date(2026, 7, 1)) == date(2026, 6, 30)),
        ("quarter end before 15 Jan", quarter_end_before(date(2027, 1, 15)) == date(2026, 12, 31)),
        ("presentation by subcategory",
         classify_source("Company Update", "Investor Presentation", "x") == "presentation"),
        ("results by the text itself",
         classify_source("Company Update", "Press Release / Media Release", "Press Release",
                         True, "Financial results for the quarter ended June 30, 2026") == "results"),
        ("order win otherwise",
         classify_source("Company Update", "Award of Order", "LOA", True, "received an LOA") == "order_win"),
    ]
    p = f = 0
    for label, ok in checks:
        p, f = (p + 1, f) if ok else (p, f + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    return p, f


def test_series():
    """The Orderbook view's series and growth, on the shapes that produced
    nonsense top gainers on 18 Oct (figures as stored, quotes left out)."""
    from order_scanner.orderbook import build_series, growth

    def snap(ann, as_of, cr, kind="results", conf=0.7, scope="total", basis="stated", flags=None):
        return {"ann_id": ann, "as_of": as_of, "order_book_inr": cr * CR, "source_kind": kind,
                "confidence": conf, "scope": scope, "as_of_basis": basis, "filed_at": as_of,
                "flags": flags}

    # BEL: one call transcript, "projects around INR 20,000 crores" (inferred 30 Jun)
    # beside "order book position as on 1st July 2026 is INR 72,258 crores".
    bel, _ = build_series([snap("t1", "2026-06-30", 20000, "transcript", 0.57, basis="quarter"),
                           snap("t1", "2026-07-01", 72258, "transcript", 0.8)])
    # Jash: a presentation (559) and a transcript (1,080 and parts 369, 31) for one quarter.
    jash, _ = build_series([snap("p1", "2026-06-30", 559, "presentation", 0.63, basis="quarter"),
                            snap("t2", "2026-06-30", 1080, "transcript", 0.57, basis="quarter"),
                            snap("t2", "2026-06-30", 369, "transcript", 0.72, basis="quarter"),
                            snap("t2", "2026-06-30", 31, "transcript", 0.55, basis="quarter")])
    # Aastha: 76.8 on 13 Jul, then 251.5 three weeks later: no growth figure.
    aastha, _ = build_series([snap("a", "2026-07-13", 76.8, "order_win"),
                              snap("b", "2026-08-03", 251.5, "press_release")])
    # Jindal Saw: 352 (a transcript aside) then 9,988: growth kept but suspect.
    jsaw, _ = build_series([snap("c", "2026-06-30", 352, "transcript", basis="quarter"),
                            snap("d", "2026-09-30", 9988)])
    # A steady book, one statement a quarter.
    steady, _ = build_series([snap("e", "2025-09-30", 800), snap("f", "2025-12-31", 900),
                              snap("g", "2026-03-31", 1000), snap("h", "2026-06-30", 1250)])
    g_aastha, g_jsaw, g_steady = growth(aastha, "1y"), growth(jsaw, "1y"), growth(steady, "1y")
    checks = [
        ("one filing, dates a day apart: only its largest figure counts (BEL)",
         [round(p["order_book_inr"] / CR) for p in bel] == [72258]),
        ("one point per quarter, a presentation preferred over a transcript (Jash)",
         [round(p["order_book_inr"] / CR) for p in jash] == [559]),
        ("no growth from two statements three weeks apart (Aastha)", g_aastha is None),
        ("a 28x step keeps its growth figure but is marked suspect (Jindal Saw)",
         g_jsaw is not None and g_jsaw["suspect"]),
        ("steady quarterly growth: 800 to 1,250 over the year, not suspect",
         g_steady is not None and abs(g_steady["pct"] - 0.5625) < 1e-9 and not g_steady["suspect"]),
        ("a figure flagged 'not the whole book' is left out of the series",
         len(build_series([snap("i", "2026-06-30", 30, flags='["0.008x annual revenue: likely not the whole book"]'),
                           snap("j", "2026-09-30", 1000)])[0]) == 1),
    ]
    p = f = 0
    for label, ok in checks:
        p, f = (p + 1, f) if ok else (p, f + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    return p, f


if __name__ == "__main__":
    tp = tf = 0
    for fn in (test_cases, test_history_from_one_sentence, test_helpers, test_series):
        p, f = fn()
        tp, tf = tp + p, tf + f
    print(f"\n{'':-^88}\n{tp} passed, {tf} failed")
    raise SystemExit(1 if tf else 0)
