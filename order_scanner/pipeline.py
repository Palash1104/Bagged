"""Orchestration: fetch -> dedupe -> extract -> enrich -> score -> alert -> store."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, time as dtime, timedelta

from .alert import format_message, send_digest, send_telegram
from .config import CONFIG
from .db import (IST, already_alerted, company_order_history, connect,
                 init_db, known_ann_ids, now_ist, record_alert, upsert)
from .extract.llm import extract_with_llm, merge
from .extract.pdf import download_pdf, pdf_to_text
from .extract.reg30 import parse_reg30
from .extract.rules import (extract, is_non_order, looks_like_order,
                            norm_company_name, worth_pdf_probe)
from .score import score_order
from .sources import bse as bse_src
from .sources import fundamentals as fund_src
from .sources import annual as annual_src
from .sources import sector as sector_src
from .sources import nse as nse_src

log = logging.getLogger(__name__)


def _ann_id(exchange: str, native_id: str) -> str:
    return hashlib.sha1(f"{exchange}|{native_id}".encode()).hexdigest()


def _company_id(rec: dict) -> str:
    if rec.get("isin"):
        return str(rec["isin"]).strip().upper()
    if rec.get("symbol"):
        return f"NSE:{str(rec['symbol']).strip().upper()}"
    if rec.get("bse_code"):
        return f"BSE:{str(rec['bse_code']).strip()}"
    name = (rec.get("company_name") or "unknown").strip().upper()
    return "NAME:" + hashlib.sha1(name.encode()).hexdigest()[:12]


def market_hours_ok(force: bool = False) -> bool:
    if force:
        return True
    now = now_ist()
    if not CONFIG.scan_weekends and now.weekday() >= 5:
        return False
    oh, om = (int(x) for x in CONFIG.market_open_hhmm.split(":"))
    ch, cm = (int(x) for x in CONFIG.market_close_hhmm.split(":"))
    return dtime(oh, om) <= now.time() <= dtime(ch, cm)


def _resolve_company_id(conn, rec: dict) -> str:
    """Find the id this company already lives under, else mint a new one.

    One company files on both exchanges under different identifiers: NSE
    supplies a symbol, BSE a numeric scrip code, and the ISIN is frequently
    absent on one side -- so _company_id() alone hands the same company two
    different ids. Observed: "Star Paper Mills Limited" (NSE) and "Star Paper
    Mills Ltd-$" (BSE) became two companies, and the same Rs 50 Cr order was
    scored twice with two different tiers.

    Matching on ISIN first, then on the normalised name, keeps both filings
    on one company_id so order history and the novelty score see one company.
    """
    isin = (rec.get("isin") or "").strip().upper() or None
    if isin:
        row = conn.execute(
            "SELECT company_id FROM companies WHERE isin=?", (isin,)).fetchone()
        if row:
            return row["company_id"]

    nkey = norm_company_name(rec.get("company_name"))
    # Short keys are too collision-prone to merge on.
    if len(nkey) >= 6:
        row = conn.execute(
            "SELECT company_id FROM companies WHERE name_key=?", (nkey,)).fetchone()
        if row:
            return row["company_id"]

    return _company_id(rec)


def _merge_company(conn, rec: dict) -> dict:
    """Upsert the company row, merging identifiers seen across exchanges."""
    cid = _resolve_company_id(conn, rec)
    existing = conn.execute(
        "SELECT * FROM companies WHERE company_id=?", (cid,)).fetchone()
    existing = dict(existing) if existing else {}

    # If we know the ISIN now but previously keyed on symbol, fold the rows.
    row = {
        "company_id": cid,
        "isin": rec.get("isin") or existing.get("isin"),
        "nse_symbol": rec.get("symbol") or existing.get("nse_symbol"),
        "bse_code": rec.get("bse_code") or existing.get("bse_code"),
        "name": rec.get("company_name") or existing.get("name") or "unknown",
        "name_key": (norm_company_name(rec.get("company_name"))
                     or existing.get("name_key")),
        "industry": rec.get("industry") or existing.get("industry"),
        "is_sme": 1 if rec.get("is_sme") else existing.get("is_sme", 0),
        "market_cap_inr": existing.get("market_cap_inr"),
        "updated_at": now_ist().isoformat(),
    }
    upsert(conn, "companies", row, "company_id")
    return row


def _duplicate_order_id(conn, company_id: str, value, filed_at,
                        order_id: str) -> str | None:
    """The same order already stored from the other exchange, if any.

    Matched on company + order value (0.5% tolerance) within three days.
    The duplicate is still stored -- both filings are real and worth keeping
    -- but it must not fire a second alert for one order win.
    """
    if not value or not filed_at:
        return None
    row = conn.execute(
        "SELECT order_id FROM orders "
        "WHERE company_id=? AND order_id<>? AND order_value_inr IS NOT NULL "
        "AND abs(order_value_inr - ?) <= ? "
        "AND abs(julianday(filed_at) - julianday(?)) <= 3 LIMIT 1",
        (company_id, order_id, value, max(1.0, value * 0.005), filed_at),
    ).fetchone()
    return row["order_id"] if row else None


def collect(from_dt: datetime, to_dt: datetime, deep: bool = False) -> list[dict]:
    records: list[dict] = []
    if CONFIG.scan_nse:
        try:
            client = nse_src.NSEClient()
            records += nse_src.fetch_window(
                client, from_dt, to_dt, include_sme=CONFIG.include_sme)
        except Exception as exc:
            log.error("NSE collection failed: %s", exc)
    if CONFIG.scan_bse:
        try:
            records += bse_src.fetch_window(
                bse_src.BSEClient(), from_dt, to_dt, deep=deep)
        except Exception as exc:
            log.error("BSE collection failed: %s", exc)
    return records


def _catch_up_start(to_dt: datetime) -> datetime | None:
    """Where a scheduled scan should start so the hours the PC was off or asleep
    are not lost: the end of the last recorded run, less the overlap, but no
    more than `max_catchup_hours` back. None when no run is on record."""
    with connect() as conn:
        row = conn.execute("SELECT MAX(window_to) FROM runs").fetchone()
    if not row or not row[0]:
        return None
    last = datetime.fromisoformat(row[0])
    if last.tzinfo is None:
        last = last.replace(tzinfo=IST)
    start = last - timedelta(minutes=CONFIG.overlap_minutes)
    floor = to_dt - timedelta(hours=CONFIG.max_catchup_hours)
    if start < floor:
        log.warning("last scan ended %s, over %dh ago: covering the last %dh only; "
                    "run `backfill --days N` for the rest", last.isoformat(),
                    CONFIG.max_catchup_hours, CONFIG.max_catchup_hours)
        return floor
    return start


def run_scan(lookback_minutes: int | None = None, force: bool = False,
             deep: bool = False, dry_run: bool = False,
             from_dt: datetime | None = None,
             to_dt: datetime | None = None,
             probe_pdfs: bool | None = None) -> dict:
    init_db()
    started = now_ist()
    if probe_pdfs is None:
        probe_pdfs = CONFIG.probe_generic_pdfs

    if not market_hours_ok(force):
        log.info("outside scan window; skipping (use --force to override)")
        return {"skipped": "outside market hours"}

    to_dt = to_dt or started
    if from_dt is None:
        mins = lookback_minutes or (CONFIG.lookback_minutes + CONFIG.overlap_minutes)
        from_dt = to_dt - timedelta(minutes=mins)
        # A scheduled scan (no explicit --lookback) also covers everything since
        # the previous run, so a PC that was off or asleep catches up.
        if lookback_minutes is None:
            resume = _catch_up_start(to_dt)
            if resume and resume < from_dt:
                log.info("catching up from the last run: %s", resume.isoformat())
                from_dt = resume

    log.info("scanning %s -> %s", from_dt.isoformat(), to_dt.isoformat())
    records = collect(from_dt, to_dt, deep=deep)
    log.info("fetched %d announcements", len(records))

    stats = {"n_announcements": len(records), "n_order_like": 0,
             "n_extracted": 0, "n_alerts": 0, "llm_calls": 0,
             "n_non_order": 0, "n_duplicates": 0,
             "n_pdf_probes": 0, "n_probe_hits": 0, "errors": []}
    watch_rows: list[dict] = []

    with connect() as conn:
        nse_client = nse_src.NSEClient() if CONFIG.scan_nse else None

        for rec in records:
            rec["ann_id"] = _ann_id(rec["exchange"], rec["native_id"])

        new_ids = set(r["ann_id"] for r in records) - known_ann_ids(
            conn, [r["ann_id"] for r in records])
        fresh = [r for r in records if r["ann_id"] in new_ids]
        log.info("%d new (%d already seen)", len(fresh), len(records) - len(fresh))

        pdf_session = bse_src.BSEClient().s

        for rec in fresh:
            try:
                company = _merge_company(conn, rec)
                order_like = (rec.get("targeted_order_subcat")
                              or looks_like_order(rec.get("headline"),
                                                  rec.get("body"),
                                                  rec.get("subcategory")))

                # Regulatory/tax/court orders, credit-rating releases and
                # rumour verifications all trip the order triggers. Screen
                # them out here, before the PDF download, so they cost
                # nothing. Headline + subcategory only, never the body.
                non_order = is_non_order(rec.get("headline"),
                                         rec.get("subcategory"))
                if non_order:
                    if order_like:
                        stats["n_non_order"] += 1
                        log.debug("not an order (%s): %s", non_order,
                                  (rec.get("headline") or "")[:80])
                    order_like = False

                # The PDF probe. A filing headlined "General Updates" or
                # "Press Release" carries no keywords at all, so the only way
                # to find the order win inside is to open the attachment.
                # Budgeted, and skipped entirely during backfill.
                pdf_text = None
                if (not order_like and probe_pdfs and rec.get("pdf_url")
                        and stats["n_pdf_probes"] < CONFIG.max_pdf_probes_per_run
                        and worth_pdf_probe(rec.get("headline"),
                                            rec.get("subcategory"))):
                    stats["n_pdf_probes"] += 1
                    pdf_text = pdf_to_text(download_pdf(pdf_session,
                                                        rec["pdf_url"]))
                    if pdf_text and looks_like_order(pdf_text):
                        order_like = True
                        stats["n_probe_hits"] += 1
                        log.info("probe: order found in PDF of %r (%s)",
                                 (rec.get("headline") or "")[:50],
                                 company["name"])

                ann_row = {
                    "ann_id": rec["ann_id"],
                    "exchange": rec["exchange"],
                    "native_id": rec["native_id"],
                    "company_id": company["company_id"],
                    "symbol": rec.get("symbol") or rec.get("bse_code"),
                    "company_name": rec.get("company_name"),
                    "category": rec.get("category"),
                    "subcategory": rec.get("subcategory"),
                    "headline": rec.get("headline"),
                    "body": rec.get("body"),
                    "pdf_url": rec.get("pdf_url"),
                    "pdf_text": None,
                    "filed_at": rec["filed_at"].isoformat() if rec.get("filed_at") else None,
                    "disseminated_at": (rec["disseminated_at"].isoformat()
                                        if rec.get("disseminated_at") else None),
                    "fetched_at": now_ist().isoformat(),
                    "is_order_like": 1 if order_like else 0,
                    "raw": json.dumps(rec.get("raw"), default=str)[:60000],
                }

                if not order_like:
                    upsert(conn, "announcements", ann_row, "ann_id")
                    continue

                stats["n_order_like"] += 1

                # Pull the PDF only for order-shaped filings. If the probe
                # above already fetched it, this is a cache hit.
                pdf_bytes = (download_pdf(pdf_session, rec["pdf_url"])
                             if rec.get("pdf_url") else None)
                if pdf_text is None:
                    pdf_text = pdf_to_text(pdf_bytes)
                if pdf_text:
                    ann_row["pdf_text"] = pdf_text[:40000]
                upsert(conn, "announcements", ann_row, "ann_id")

                result = extract(rec.get("headline"), rec.get("body"), pdf_text,
                                 company_name=company["name"],
                                 reg30=parse_reg30(pdf_bytes),
                                 filed_at=ann_row["filed_at"],
                                 subcategory=rec.get("subcategory"))
                if not result.is_order:
                    continue

                if result.needs_llm and stats["llm_calls"] < CONFIG.llm_max_calls_per_run:
                    body = "\n".join(filter(None, [rec.get("body"), pdf_text]))
                    llm_res = extract_with_llm(
                        conn, company["name"], rec["exchange"],
                        rec.get("headline") or "", body)
                    if llm_res is not None:
                        stats["llm_calls"] += 1
                        result = merge(result, llm_res)

                stats["n_extracted"] += 1

                funds = fund_src.resolve(conn, company, nse_client=nse_client)
                sector_src.ensure(conn, company)     # once per company
                if result.guidance:
                    # guidance is measured against the last completed year
                    funds = annual_src.ensure(conn, company, funds, ann_row["filed_at"])
                history = company_order_history(conn, company["company_id"])

                order_dict = result.to_dict()
                order_dict.update({
                    "company_name": company["name"],
                    "symbol": company.get("nse_symbol") or company.get("bse_code"),
                    "bse_code": company.get("bse_code"),
                    "exchange": rec["exchange"],
                    "filed_at": ann_row["filed_at"],
                    "pdf_url": rec.get("pdf_url"),
                    "extraction_conf": result.confidence,
                })

                sr = score_order(order_dict, funds, company, history)

                order_row = {
                    "order_id": rec["ann_id"],
                    "ann_id": rec["ann_id"],
                    "company_id": company["company_id"],
                    "company_name": company["name"],
                    "exchange": rec["exchange"],
                    "filed_at": ann_row["filed_at"],
                    "order_value_inr": result.order_value_inr,
                    "value_currency": result.value_currency,
                    "value_is_range": 1 if result.value_is_range else 0,
                    "value_low_inr": result.value_low_inr,
                    "value_high_inr": result.value_high_inr,
                    "customer": result.customer,
                    "customer_type": result.customer_type,
                    "execution_months": result.execution_months,
                    "order_type": result.order_type,
                    "is_amendment": 1 if result.is_amendment else 0,
                    "is_related_party": 1 if result.is_related_party else 0,
                    "scope": result.scope,
                    "summary": result.summary,
                    "guidance": json.dumps(result.guidance) if result.guidance else None,
                    "extraction_method": result.method,
                    "extraction_conf": result.confidence,
                    "extraction_notes": "; ".join(result.notes)[:1000],
                    "q_revenue_inr": (funds or {}).get("q_revenue_inr"),
                    "ttm_revenue_inr": (funds or {}).get("ttm_revenue_inr"),
                    "market_cap_inr": (funds or {}).get("market_cap_inr")
                                      or company.get("market_cap_inr"),
                    "ratio_to_quarter": sr.ratio_to_quarter,
                    "ratio_to_ttm": sr.ratio_to_ttm,
                    "annualised_ratio": sr.annualised_ratio,
                    "score": sr.score,
                    "score_breakdown": sr.breakdown_json(),
                    "tier": sr.tier,
                    "data_quality": sr.data_quality,
                    "created_at": now_ist().isoformat(),
                }
                upsert(conn, "orders", order_row, "order_id")

                dup_of = _duplicate_order_id(
                    conn, company["company_id"], result.order_value_inr,
                    ann_row["filed_at"], rec["ann_id"])

                if sr.tier in ("HIGH", "MEDIUM") and dup_of:
                    # One order win, filed on both exchanges. Keep the row,
                    # send one alert.
                    stats["n_duplicates"] += 1
                    log.info("duplicate of %s; not alerting again for %s",
                             dup_of, company["name"])
                elif sr.tier in ("HIGH", "MEDIUM"):
                    if dry_run:
                        log.info("[dry-run] would alert %s (%s, %.0f)",
                                 company["name"], sr.tier, sr.score)
                        stats["n_alerts"] += 1
                    elif not already_alerted(conn, rec["ann_id"], "telegram"):
                        msg = format_message(order_dict | order_row, sr, funds, company)
                        ok, err = send_telegram(msg)
                        record_alert(conn, rec["ann_id"], "telegram", sr.tier, ok, err)
                        if ok:
                            stats["n_alerts"] += 1
                        else:
                            log.warning("telegram send failed: %s", err)
                elif sr.tier == "WATCH" and not dup_of:
                    watch_rows.append(order_row)

            except Exception as exc:
                log.exception("failed on %s", rec.get("company_name"))
                stats["errors"].append(f"{rec.get('company_name')}: {exc}")

        if watch_rows and not dry_run:
            send_digest(watch_rows)

        conn.execute(
            "INSERT INTO runs (started_at, finished_at, window_from, window_to, "
            "n_announcements, n_order_like, n_extracted, n_alerts, llm_calls, errors) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (started.isoformat(), now_ist().isoformat(),
             from_dt.isoformat(), to_dt.isoformat(),
             stats["n_announcements"], stats["n_order_like"],
             stats["n_extracted"], stats["n_alerts"], stats["llm_calls"],
             json.dumps(stats["errors"][:20])))

    log.info("run complete: %s", stats)
    return stats


def backfill(days: int = 30, deep: bool = True,
             probe_pdfs: bool = False) -> dict:
    """Seed the historical table. Run this once before relying on novelty scores.

    The PDF probe defaults OFF here: on a 30-minute live window it costs a
    handful of downloads, but across a 90-day backfill it is ~400 extra PDFs
    per day. Pass probe_pdfs=True to accept that for maximum recall.
    """
    init_db()
    to_dt = now_ist()
    totals = {"n_announcements": 0, "n_order_like": 0, "n_extracted": 0}
    for i in range(days, 0, -1):
        day_to = to_dt - timedelta(days=i - 1)
        day_from = day_to - timedelta(days=1)
        log.info("backfill day %s", day_from.date())
        s = run_scan(force=True, deep=deep, dry_run=True,
                     from_dt=day_from, to_dt=day_to, probe_pdfs=probe_pdfs)
        for k in totals:
            totals[k] += s.get(k, 0)
    return totals
