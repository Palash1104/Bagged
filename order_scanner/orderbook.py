"""Collecting reported order books: from stored filings, from new filings during
a scan, and from historical filings on demand.

Three entry points, all idempotent:

    record_filing()   one filing's text -> its order-book snapshots
    recheck()         replay record_filing over every stored filing (offline)
    fetch()           open the PDFs of results releases, investor presentations
                      and order-book updates not read yet, and record them
    pull_history()    add older presentation / press-release filings from BSE
                      so the History column reaches back a year or more

and one reader, series(), used by the dashboard.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta

from .config import CONFIG
from .db import now_ist, upsert
from .extract.orderbook import classify_source, find_order_books, plausibility
from .extract.pdf import download_pdf, pdf_to_text

log = logging.getLogger(__name__)

# Filings worth opening for an order book. Investor presentations and results
# releases state it every quarter; "order book"/"order position" headlines are
# companies' own periodic updates (Dee Development, Solarium, Susan
# Electricals); earnings call transcripts usually say it in the opening remarks.
CANDIDATE = re.compile(
    r"investor\s+presentation|earnings\s+call\s+transcript|transcript|"
    r"press\s+release|media\s+release|order\s*book|order\s+position|unexecuted|"
    r"financial\s+results?\s+(?:press|release|update)|earnings\s+(?:update|release)|"
    r"results?\s+press",
    re.IGNORECASE)
NOT_CANDIDATE = re.compile(r"newspaper|intimation\s+of\s+(?:analyst|investor)\s+meet|schedule\s+of",
                           re.IGNORECASE)
# Opening order: the documents most likely to carry the book first.
PRIORITY = {"presentation": 0, "results": 1, "update": 2, "transcript": 3,
            "press_release": 4, "order_win": 5, "annual": 6, "other": 7}
# A stored filing's text is worth re-reading only if it mentions a book.
MENTIONS = ("order book", "orderbook", "order-book", "order backlog", "unexecuted order",
            "orders in hand", "outstanding orders", "pending orders")

BSE_HISTORY_SUBCATS = ["Investor Presentation", "Press Release / Media Release",
                       "Earnings Call Transcript"]


def is_candidate(category, subcategory, headline) -> bool:
    blob = " ".join(x for x in (category, subcategory, headline) if x)
    return bool(CANDIDATE.search(blob)) and not NOT_CANDIDATE.search(blob)


def _snap_id(ann_id: str, as_of: str, value: float) -> str:
    return hashlib.sha1(f"{ann_id}|{as_of}|{round(value)}".encode()).hexdigest()


def _annual_revenue(conn, company_id: str) -> float | None:
    r = conn.execute("SELECT last_fy_revenue_inr, ttm_revenue_inr FROM fundamentals "
                     "WHERE company_id=?", (company_id,)).fetchone()
    if not r:
        return None
    return r["last_fy_revenue_inr"] or r["ttm_revenue_inr"]


def record_filing(conn, ann: dict, text: str | None, source: str = "stored-text",
                  order_value_inr: float | None = None,
                  pdf_was_read: bool | None = None) -> int:
    """Store every order-book figure in one filing. Replaces that filing's
    previous snapshots, so re-running is safe. Returns how many were kept."""
    kind = classify_source(ann.get("category"), ann.get("subcategory"), ann.get("headline"),
                           bool(ann.get("is_order_like")), (text or "")[:1500])
    figs = [f for f in find_order_books(text, ann.get("filed_at"), kind)
            if f.confidence >= CONFIG.orderbook_min_confidence]
    conn.execute("DELETE FROM orderbook WHERE ann_id=?", (ann["ann_id"],))
    revenue = _annual_revenue(conn, ann["company_id"]) if figs else None
    kept = 0
    for f in figs[:6]:
        flags = plausibility(f.value_inr, f.scope, revenue, order_value_inr)
        upsert(conn, "orderbook", {
            "snap_id": _snap_id(ann["ann_id"], f.as_of, f.value_inr),
            "ann_id": ann["ann_id"],
            "company_id": ann["company_id"],
            "exchange": ann.get("exchange"),
            "filed_at": ann.get("filed_at"),
            "as_of": f.as_of,
            "as_of_basis": f.as_of_basis,
            "order_book_inr": f.value_inr,
            "currency": f.currency,
            "is_approx": 1 if f.is_approx else 0,
            "scope": f.scope,
            "source_kind": kind,
            "method": "regex",
            "confidence": f.confidence,
            "quote": f.quote,
            "notes": "; ".join(f.notes)[:500],
            "flags": json.dumps(flags) if flags else None,
            "created_at": now_ist().isoformat(),
        }, "snap_id")
        kept += 1
    # Mark it read only when its PDF was part of the text: a filing read from
    # its headline alone may still carry the book inside the attachment.
    if pdf_was_read is None:
        pdf_was_read = source == "pdf" or bool(ann.get("pdf_text"))
    if pdf_was_read:
        upsert(conn, "orderbook_checked", {
            "ann_id": ann["ann_id"], "checked_at": now_ist().isoformat(),
            "n_found": kept, "source": source, "error": None}, "ann_id")
    return kept


def tracked_company_ids(conn) -> set[str]:
    rows = conn.execute("SELECT DISTINCT company_id FROM orders UNION "
                        "SELECT DISTINCT company_id FROM orderbook").fetchall()
    return {r[0] for r in rows if r[0]}


# --------------------------------------------------------------------------
# offline replay
# --------------------------------------------------------------------------
def recheck(conn) -> dict:
    """Re-read every stored filing that mentions an order book. No network."""
    like = " OR ".join(["lower(coalesce(headline,'')||' '||coalesce(body,'')||' '||"
                        "coalesce(pdf_text,'')) LIKE ?"] * len(MENTIONS))
    rows = conn.execute(f"SELECT * FROM announcements WHERE {like}",
                        tuple(f"%{m}%" for m in MENTIONS)).fetchall()
    st = {"filings": 0, "with_book": 0, "snapshots": 0}
    for r in rows:
        a = dict(r)
        text = "\n\n".join(x for x in (a.get("headline"), a.get("body"), a.get("pdf_text")) if x)
        ov = conn.execute("SELECT order_value_inr FROM orders WHERE order_id=?",
                          (a["ann_id"],)).fetchone()
        n = record_filing(conn, a, text, "stored-text", ov[0] if ov else None)
        st["filings"] += 1
        st["with_book"] += 1 if n else 0
        st["snapshots"] += n
    st["companies"] = conn.execute(
        "SELECT COUNT(DISTINCT company_id) FROM orderbook").fetchone()[0]
    return st


# --------------------------------------------------------------------------
# reading PDFs
# --------------------------------------------------------------------------
def _pdf_text(session, url: str, kind: str) -> tuple[str | None, str | None]:
    """(text, error). Reads the PDF cache if the file is there, never writes it.
    BSE moves older attachments from AttachLive to AttachHis."""
    urls = [url]
    if "/AttachLive/" in url:
        urls.append(url.replace("/AttachLive/", "/AttachHis/"))
    for u in urls:
        data = download_pdf(session, u, cache=False)
        if data:
            pages = CONFIG.orderbook_max_pages if kind in ("presentation", "transcript") else 12
            return pdf_to_text(data, max_pages=pages), None
    return None, "download failed"


def candidates(conn, days: int | None = None, all_companies: bool = False,
               limit: int | None = None) -> list[dict]:
    """Stored filings worth opening for an order book, best first, one per
    company per day (the same deck is filed on both exchanges)."""
    where = ["a.pdf_url IS NOT NULL",
             # a failed download is retried on the next run (BSE refuses
             # requests for days at a time); only a PDF actually read is done
             "a.ann_id NOT IN (SELECT ann_id FROM orderbook_checked WHERE error IS NULL)"]
    params: list = []
    if days:
        where.append("a.filed_at >= ?")
        params.append((now_ist() - timedelta(days=days)).isoformat())
    rows = [dict(r) for r in conn.execute(
        f"SELECT a.ann_id, a.exchange, a.company_id, a.company_name, a.category, a.subcategory, "
        f"a.headline, a.filed_at, a.pdf_url, a.is_order_like FROM announcements a "
        f"WHERE {' AND '.join(where)} ORDER BY a.filed_at DESC", tuple(params))]
    tracked = None if all_companies else tracked_company_ids(conn)
    seen: set[tuple[str, str]] = set()
    out = []
    for r in rows:
        if tracked is not None and r["company_id"] not in tracked:
            continue
        if not is_candidate(r.get("category"), r.get("subcategory"), r.get("headline")):
            continue
        key = (r["company_id"], (r["filed_at"] or "")[:10],
               classify_source(r.get("category"), r.get("subcategory"), r.get("headline")))
        if key in seen:
            continue
        seen.add(key)
        r["kind"] = key[2]
        out.append(r)
    out.sort(key=lambda r: (PRIORITY.get(r["kind"], 9), -(_ts(r["filed_at"]))))
    return out[:limit] if limit else out


def _ts(s) -> float:
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


def fetch(conn, days: int | None = None, limit: int | None = None,
          all_companies: bool = False, pause: float = 0.3, progress=None) -> dict:
    """Open candidate PDFs and record their order books. Resumable: every
    filing opened is marked, so an interrupted run carries on where it stopped."""
    from .sources.bse import BSEClient
    session = BSEClient().s
    todo = candidates(conn, days=days, all_companies=all_companies, limit=limit)
    st = {"to_open": len(todo), "opened": 0, "with_book": 0, "snapshots": 0, "failed": 0}
    for i, a in enumerate(todo, 1):
        text, err = _pdf_text(session, a["pdf_url"], a["kind"])
        if err or not text:
            st["failed"] += 1
            upsert(conn, "orderbook_checked", {
                "ann_id": a["ann_id"], "checked_at": now_ist().isoformat(),
                "n_found": 0, "source": "pdf", "error": err or "no text"}, "ann_id")
        else:
            full = "\n\n".join(x for x in (a.get("headline"), text) if x)
            n = record_filing(conn, a, full, "pdf")
            st["opened"] += 1
            st["with_book"] += 1 if n else 0
            st["snapshots"] += n
        # Commit after every filing: a long write transaction would hold the
        # database lock and make a scheduled scan running alongside wait.
        conn.commit()
        if i % 10 == 0 and progress:
            progress(i, len(todo), st)
        time.sleep(pause)
    conn.commit()
    return st


def pull_history(conn, days: int, chunk_days: int = 7) -> dict:
    """Add older investor presentations, press releases and transcripts to
    `announcements`, from BSE's subcategory feed, so `fetch` can read them.

    Only metadata is stored here (headline, PDF link, date); nothing is
    downloaded. Older than the scanner's own history only -- days already
    covered by scans are skipped.
    """
    from .pipeline import _ann_id, _merge_company
    from .sources import bse as bse_src

    client = bse_src.BSEClient()
    row = conn.execute("SELECT MIN(filed_at) FROM announcements").fetchone()
    covered_from = datetime.fromisoformat(row[0]) if row and row[0] else now_ist()
    start = now_ist() - timedelta(days=days)
    st = {"windows": 0, "found": 0, "new": 0}
    cur = start
    while cur < covered_from:
        end = min(cur + timedelta(days=chunk_days), covered_from)
        st["windows"] += 1
        for sub in BSE_HISTORY_SUBCATS:
            try:
                raws = client.announcements(cur, end, category=bse_src.CAT_COMPANY_UPDATE,
                                            subcategory=sub, max_pages=40)
            except Exception as exc:
                log.warning("history %s %s: %s", sub, cur.date(), exc)
                continue
            for raw in raws:
                rec = bse_src.normalise(raw)
                st["found"] += 1
                aid = _ann_id(rec["exchange"], rec["native_id"])
                if conn.execute("SELECT 1 FROM announcements WHERE ann_id=?", (aid,)).fetchone():
                    continue
                company = _merge_company(conn, rec)
                upsert(conn, "announcements", {
                    "ann_id": aid, "exchange": rec["exchange"], "native_id": rec["native_id"],
                    "company_id": company["company_id"],
                    "symbol": rec.get("symbol") or rec.get("bse_code"),
                    "company_name": rec.get("company_name"), "category": rec.get("category"),
                    "subcategory": rec.get("subcategory"), "headline": rec.get("headline"),
                    "body": rec.get("body"), "pdf_url": rec.get("pdf_url"), "pdf_text": None,
                    "filed_at": rec["filed_at"].isoformat() if rec.get("filed_at") else None,
                    "disseminated_at": (rec["disseminated_at"].isoformat()
                                        if rec.get("disseminated_at") else None),
                    "fetched_at": now_ist().isoformat(), "is_order_like": 0,
                    "raw": json.dumps(raw, default=str)[:20000],
                }, "ann_id")
                st["new"] += 1
            time.sleep(0.4)
        conn.commit()
        log.info("history %s -> %s: %d new so far", cur.date(), end.date(), st["new"])
        cur = end
    return st


# --------------------------------------------------------------------------
# reading the series back
# --------------------------------------------------------------------------
PERIOD_DAYS = {"3m": 92, "6m": 183, "1y": 366, "all": 100000}


def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


# Which document to believe when several state a book for the same quarter.
# A results release, presentation or the company's own order-book update is
# written to state it; an earnings call transcript also carries analysts'
# numbers and segment asides ("these seven, eight projects are around
# INR 20,000 crores out of INR 76,000 crores" -- BEL), so it ranks last.
SOURCE_RANK = {"update": 0, "results": 0, "presentation": 1, "order_win": 1,
               "press_release": 2, "annual": 2, "other": 3, "transcript": 3}
# Growth needs two statements at least this far apart: two figures a day or
# three weeks apart are two different measures, not growth (BEL "+261% over
# 1 day", Aastha "+228% over 21 days" on 18 Oct).
MIN_GROWTH_DAYS = 60
# A step between consecutive points this large is far more often a misread or
# a change of scope than growth (Jindal Saw 352 -> 9,988 Cr: a seamless-pipe
# aside in a transcript, then the group book). Kept, but flagged for checking
# and left out of the top gainers.
SUSPECT_STEP = 4.0


def _quarter(as_of: str) -> str:
    d = _d(as_of)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}" if d else as_of or ""


def build_series(snaps: list[dict]) -> tuple[list[dict], str]:
    """One point per quarter, best statement first. Uses the whole-company book
    when the company has ever stated one; otherwise its segment book (marked).

    Within one filing, a company states its book and then its parts ("of which
    India Rs 293 crore and outside India Rs 639 crore"; "boiler order book is at
    INR 150 crores"). The parts can be missed by the scope rules, but a part is
    never larger than the whole, so only the largest figure a filing gives for
    a date counts as that filing's whole-company book.
    """
    usable = [s for s in snaps if not any(
        m in (s.get("flags") or "") for m in ("unit error", "not the whole book"))]
    total = [s for s in usable if s.get("scope") == "total"]
    scope = "total" if total else "segment"
    pool = total or usable

    # Dates within a month of each other in one filing are one reading: BEL's
    # call put "INR 20,000 crores" (projects, inferred 30 Jun) beside "order
    # book position as on 1st July is INR 72,258 crores".
    by_filing: dict = {}
    for s in pool:
        by_filing.setdefault(s.get("ann_id"), []).append(s)
    largest: list[dict] = []
    for figs in by_filing.values():
        figs.sort(key=lambda s: s["as_of"])
        group: list[dict] = []
        for s in figs:
            if group and (_d(s["as_of"]) and _d(group[0]["as_of"])
                          and (_d(s["as_of"]) - _d(group[0]["as_of"])).days > 31):
                largest.append(max(group, key=lambda g: g["order_book_inr"]))
                group = []
            group.append(s)
        if group:
            largest.append(max(group, key=lambda g: g["order_book_inr"]))

    def better(s: dict, cur: dict) -> bool:
        a = (-SOURCE_RANK.get(s.get("source_kind") or "other", 3), s["as_of"],
             s["confidence"], s.get("filed_at") or "")
        b = (-SOURCE_RANK.get(cur.get("source_kind") or "other", 3), cur["as_of"],
             cur["confidence"], cur.get("filed_at") or "")
        return a > b

    by_q: dict[str, dict] = {}
    for s in largest:
        q = _quarter(s["as_of"])
        if q not in by_q or better(s, by_q[q]):
            by_q[q] = s
    points = sorted(by_q.values(), key=lambda s: s["as_of"])
    # The same figure restated in a later filing that gives no date of its own
    # (an order-win release repeating last quarter's book) is not a new
    # reading: keep the dated original, so a flat line is not invented.
    out: list[dict] = []
    for p in points:
        if (out and abs(p["order_book_inr"] - out[-1]["order_book_inr"]) < 1e5
                and p.get("as_of_basis") != "stated"):
            continue
        out.append(p)
    return out, scope


def growth(series: list[dict], period: str) -> dict | None:
    """Change from the earliest point inside the period to the latest one,
    when the two are at least MIN_GROWTH_DAYS apart. `suspect` is set when any
    step along the way multiplies or divides the book by SUSPECT_STEP or more."""
    if len(series) < 2:
        return None
    last = series[-1]
    end = _d(last["as_of"])
    start = end - timedelta(days=PERIOD_DAYS.get(period, 366))
    window = [s for s in series[:-1] if _d(s["as_of"]) and _d(s["as_of"]) >= start
              and (end - _d(s["as_of"])).days >= MIN_GROWTH_DAYS]
    if not window:
        return None
    base = window[0]
    if not base["order_book_inr"]:
        return None
    path = series[series.index(base):]
    suspect = any(a["order_book_inr"] and not (
        1 / SUSPECT_STEP < b["order_book_inr"] / a["order_book_inr"] < SUSPECT_STEP)
        for a, b in zip(path, path[1:]))
    return {"pct": last["order_book_inr"] / base["order_book_inr"] - 1,
            "from_as_of": base["as_of"], "from_value": base["order_book_inr"],
            "days": (end - _d(base["as_of"])).days, "suspect": suspect}
