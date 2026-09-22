"""Command line entry points.

    python -m order_scanner.cli scan              # one scheduled batch
    python -m order_scanner.cli scan --force --lookback 240
    python -m order_scanner.cli backfill --days 60
    python -m order_scanner.cli dashboard
    python -m order_scanner.cli test-extract "Company bags Rs 250 crore order..."
    python -m order_scanner.cli rescore           # re-score stored orders
    python -m order_scanner.cli recheck           # re-extract stored filings
    python -m order_scanner.cli resummarize       # rebuild order descriptions
    python -m order_scanner.cli sectors           # look up company sectors
    python -m order_scanner.cli fetch-pdfs        # cache every order's PDF
    python -m order_scanner.cli fundamentals TITAN
    python -m order_scanner.cli test-alert        # verify Telegram wiring
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import CONFIG, DB_PATH, DOTENV_STATUS


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.ERROR)


def cmd_scan(args) -> int:
    from .pipeline import run_scan
    stats = run_scan(lookback_minutes=args.lookback, force=args.force,
                     deep=args.deep, dry_run=args.dry_run,
                     probe_pdfs=args.probe_pdfs)
    print(json.dumps(stats, indent=2, default=str))
    return 0


def cmd_backfill(args) -> int:
    from .pipeline import backfill
    print(json.dumps(backfill(days=args.days, deep=True,
                              probe_pdfs=args.probe_pdfs),
                     indent=2, default=str))
    return 0


def cmd_dashboard(args) -> int:
    from .dashboard import serve
    serve(host=args.host, port=args.port)
    return 0


def cmd_test_extract(args) -> int:
    from .extract.rules import extract
    text = args.text
    if text == "-":
        text = sys.stdin.read()
    res = extract(args.headline or text[:200], text, None)
    d = res.to_dict()
    if d.get("order_value_inr"):
        d["order_value_cr"] = round(d["order_value_inr"] / 1e7, 3)
    print(json.dumps(d, indent=2, default=str))
    return 0


def cmd_rescore(args) -> int:
    """Re-run scoring over stored orders after tweaking weights."""
    import json as _json

    from .db import company_order_history, connect, get_fundamentals
    from .score import score_order

    n = 0
    with connect() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY filed_at").fetchall()
        for r in rows:
            o = dict(r)
            company = conn.execute(
                "SELECT * FROM companies WHERE company_id=?",
                (o["company_id"],)).fetchone()
            company = dict(company) if company else {"company_id": o["company_id"]}
            funds = get_fundamentals(conn, o["company_id"])
            if funds:
                funds = dict(funds)
                funds["market_cap_inr"] = company.get("market_cap_inr")
            hist = company_order_history(conn, o["company_id"])
            hist = [h for h in hist if h != o.get("order_value_inr")]
            o["confidence"] = o.get("extraction_conf")
            sr = score_order(o, funds, company, hist)
            conn.execute(
                "UPDATE orders SET score=?, tier=?, score_breakdown=?, "
                "data_quality=?, ratio_to_quarter=?, ratio_to_ttm=?, "
                "annualised_ratio=? WHERE order_id=?",
                (sr.score, sr.tier, sr.breakdown_json(), sr.data_quality,
                 sr.ratio_to_quarter, sr.ratio_to_ttm, sr.annualised_ratio,
                 o["order_id"]))
            n += 1
    print(f"rescored {n} orders")
    return 0


def cmd_recheck(args) -> int:
    """Re-run the CURRENT extraction rules over already-stored announcements.

    Every filing's headline, body and PDF text are kept in the database, so a
    change to the extractor does not require wiping orders.db and
    re-downloading 90 days of PDFs -- this replays the rules offline.

    Three outcomes per filing:
      * no longer an order  -> the order row is deleted and the announcement
                               is re-flagged, so it stops showing on the
                               dashboard (e.g. a licence to build the
                               company's own facility)
      * still an order      -> extracted fields are refreshed and re-scored,
                               picking up better value/execution parsing
      * newly an order      -> a row is created for a filing whose text now
                               classifies (needs stored text; filings that
                               never had their PDF fetched are reported
                               instead, since the value lives in the PDF)

    Fundamentals are taken from the stored snapshot -- no network calls.
    """
    from .db import connect, get_fundamentals, init_db, now_ist, upsert
    from .extract.pdf import download_pdf, pdf_to_text, rupee_damage
    from .extract.reg30 import parse_reg30
    from .sources import annual as annual_src
    from .extract.rules import extract, is_non_order, looks_like_order
    from .score import score_order
    from .sources.bse import BSEClient

    init_db()
    pdf_session = BSEClient().s
    st = {"checked": 0, "dropped": 0, "refreshed": 0, "created": 0,
          "need_pdf": 0}
    dropped, changed = [], []

    with connect() as conn:
        anns = [dict(r) for r in conn.execute(
            "SELECT * FROM announcements ORDER BY filed_at").fetchall()]

        for a in anns:
            has_text = bool(a.get("body") or a.get("pdf_text") or a.get("headline"))
            order_like = bool(a.get("is_order_like")) or looks_like_order(
                a.get("headline"), a.get("body"), a.get("subcategory"))
            if not order_like or not has_text:
                continue
            st["checked"] += 1

            company = conn.execute("SELECT * FROM companies WHERE company_id=?",
                                   (a["company_id"],)).fetchone()
            company = (dict(company) if company
                       else {"company_id": a["company_id"],
                             "name": a.get("company_name")})

            marker = is_non_order(a.get("headline"), a.get("subcategory"))
            pdf_text, reg30 = a.get("pdf_text"), None
            if not marker and a.get("pdf_url"):
                # From the PDF cache: re-read the disclosure table, and OCR a
                # scanned filing that was stored with no text.
                pdf_bytes = download_pdf(pdf_session, a["pdf_url"])
                reg30 = parse_reg30(pdf_bytes)
                # ... or one whose stored text has damaged rupee figures.
                if len(pdf_text or "") < 200 or rupee_damage(pdf_text):
                    fresh = pdf_to_text(pdf_bytes)
                    if fresh and (len(fresh) > len(pdf_text or "")
                                  or rupee_damage(pdf_text)):
                        pdf_text = fresh
                        conn.execute("UPDATE announcements SET pdf_text=? WHERE ann_id=?",
                                     (pdf_text[:40000], a["ann_id"]))
            res = None if marker else extract(
                a.get("headline"), a.get("body"), pdf_text,
                company_name=company.get("name"), reg30=reg30,
                filed_at=a.get("filed_at"), subcategory=a.get("subcategory"))

            existed = conn.execute("SELECT 1 FROM orders WHERE order_id=?",
                                   (a["ann_id"],)).fetchone() is not None

            # ---- no longer an order ------------------------------------
            if marker or not res.is_order:
                why = marker or "; ".join(res.notes if res else [])
                if existed:
                    conn.execute("DELETE FROM alerts WHERE order_id=?", (a["ann_id"],))
                    conn.execute("DELETE FROM orders WHERE order_id=?", (a["ann_id"],))
                    st["dropped"] += 1
                    dropped.append((a.get("company_name"), why[:110]))
                conn.execute("UPDATE announcements SET is_order_like=0 WHERE ann_id=?",
                             (a["ann_id"],))
                continue

            conn.execute("UPDATE announcements SET is_order_like=1 WHERE ann_id=?",
                         (a["ann_id"],))
            if not existed and not a.get("pdf_text") and a.get("pdf_url"):
                # Classifies on headline/body, but the figure is in the PDF we
                # never fetched. A scan with the probe enabled will collect it.
                st["need_pdf"] += 1
                continue

            funds = get_fundamentals(conn, company["company_id"])
            funds = dict(funds) if funds else None
            if funds:
                funds["market_cap_inr"] = company.get("market_cap_inr")
            if res.guidance:
                funds = annual_src.ensure(conn, company, funds, a.get("filed_at"))
            hist = [r["order_value_inr"] for r in conn.execute(
                "SELECT order_value_inr FROM orders WHERE company_id=? "
                "AND order_id<>? AND order_value_inr > 0 AND is_amendment=0",
                (company["company_id"], a["ann_id"]))]

            d = res.to_dict()
            d.update({"company_name": company.get("name"),
                      "exchange": a["exchange"], "filed_at": a["filed_at"],
                      "extraction_conf": res.confidence})
            sr = score_order(d, funds, company, hist)

            prev = conn.execute(
                "SELECT order_value_inr, execution_months, score, tier "
                "FROM orders WHERE order_id=?", (a["ann_id"],)).fetchone()

            upsert(conn, "orders", {
                "order_id": a["ann_id"], "ann_id": a["ann_id"],
                "company_id": company["company_id"],
                "company_name": company.get("name"),
                "exchange": a["exchange"], "filed_at": a["filed_at"],
                "order_value_inr": res.order_value_inr,
                "value_currency": res.value_currency,
                "value_is_range": 1 if res.value_is_range else 0,
                "value_low_inr": res.value_low_inr,
                "value_high_inr": res.value_high_inr,
                "customer": res.customer, "customer_type": res.customer_type,
                "execution_months": res.execution_months,
                "order_type": res.order_type,
                "is_amendment": 1 if res.is_amendment else 0,
                "is_related_party": 1 if res.is_related_party else 0,
                "scope": res.scope, "summary": res.summary,
                "guidance": json.dumps(res.guidance) if res.guidance else None,
                "extraction_method": res.method,
                "extraction_conf": res.confidence,
                "extraction_notes": "; ".join(res.notes)[:1000],
                "q_revenue_inr": (funds or {}).get("q_revenue_inr"),
                "ttm_revenue_inr": (funds or {}).get("ttm_revenue_inr"),
                "market_cap_inr": (funds or {}).get("market_cap_inr")
                                  or company.get("market_cap_inr"),
                "ratio_to_quarter": sr.ratio_to_quarter,
                "ratio_to_ttm": sr.ratio_to_ttm,
                "annualised_ratio": sr.annualised_ratio,
                "score": sr.score, "score_breakdown": sr.breakdown_json(),
                "tier": sr.tier, "data_quality": sr.data_quality,
                "created_at": now_ist().isoformat(),
            }, "order_id")

            if existed:
                st["refreshed"] += 1
                if prev and (prev["order_value_inr"] != res.order_value_inr
                             or prev["execution_months"] != res.execution_months
                             or prev["tier"] != sr.tier):
                    changed.append((a.get("company_name"),
                                    prev["order_value_inr"], res.order_value_inr,
                                    prev["execution_months"], res.execution_months,
                                    prev["tier"], sr.tier))
            else:
                st["created"] += 1

    cr = lambda v: "—" if v in (None, 0) else f"{v/1e7:.2f}cr"
    mo = lambda v: "—" if not v else f"{round(v)}m"
    if dropped:
        print(f"\nNo longer orders ({len(dropped)}):")
        for name, why in dropped:
            print(f"  - {str(name)[:34]:<34} {why}")
    if changed:
        print(f"\nChanged ({len(changed)}):")
        for n, ov, nv, om, nm, ot, nt in changed:
            bits = []
            if ov != nv:
                bits.append(f"value {cr(ov)} -> {cr(nv)}")
            if om != nm:
                bits.append(f"exec {mo(om)} -> {mo(nm)}")
            if ot != nt:
                bits.append(f"{ot} -> {nt}")
            print(f"  ~ {str(n)[:34]:<34} {'; '.join(bits)}")
    if st["need_pdf"]:
        print(f"\n{st['need_pdf']} filing(s) now classify as orders but their PDF was "
              f"never fetched,\nso no value can be read. A scan with the PDF probe "
              f"will collect them.")
    print()
    print(json.dumps(st, indent=2))
    return 0


def cmd_resummarize(args) -> int:
    """Rebuild every stored order's one-line description from its saved text.

    Only `scope` and `summary` are written. Values, customer, scores and tiers
    are left exactly as they are, so this is safe to run after any change to
    the summariser. No network calls. A summary the LLM wrote is kept.
    """
    from .db import connect, init_db
    from .extract.summary import describe

    init_db()
    n = with_summary = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT o.*, a.headline, a.body, a.pdf_text FROM orders o "
            "LEFT JOIN announcements a ON a.ann_id = o.ann_id").fetchall()
        for r in rows:
            scope, summary = describe(
                r["headline"], r["body"], r["pdf_text"],
                customer=r["customer"], order_type=r["order_type"],
                value_inr=r["order_value_inr"],
                value_is_range=bool(r["value_is_range"]),
                value_low_inr=r["value_low_inr"], value_high_inr=r["value_high_inr"],
                execution_months=r["execution_months"])
            if "llm" in (r["extraction_method"] or "") and r["summary"]:
                summary = r["summary"]
            conn.execute("UPDATE orders SET scope=?, summary=? WHERE order_id=?",
                         (scope, summary, r["order_id"]))
            n += 1
            with_summary += 1 if summary else 0
    print(f"rebuilt descriptions for {n} orders ({with_summary} with a summary)")
    return 0


def cmd_sectors(args) -> int:
    """Look up the sector of every company with an order and no sector on file.

    Retries companies an earlier lookup found nothing for; with --all, re-resolves
    every company with an order. Network calls: one or two per company --
    screener.in, then BSE, then yfinance.
    """
    import time

    from .db import connect, init_db
    from .sources import sector as sector_src

    init_db()
    found = 0
    with connect() as conn:
        missing_only = "" if args.all else "(sector IS NULL OR sector = '') AND "
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM companies WHERE {missing_only}"
            "company_id IN (SELECT DISTINCT company_id FROM orders)")]
        for co in rows:
            sector, industry = sector_src.resolve(co)
            sector_src.store(conn, co["company_id"], sector, industry,
                             replace_industry=args.all)
            time.sleep(0.8)         # be polite to screener.in
            found += 1 if sector else 0
            print(f"  {str(co.get('name'))[:42]:<42} {sector or '-'}"
                  f"{'  /  ' + industry if industry else ''}")
    print(f"sector found for {found} of {len(rows)} companies")
    return 0


def cmd_fetch_pdfs(args) -> int:
    """Cache the PDF of every stored order that is not cached yet.

    Scans already cache each order filing's PDF; this fills any gap so the
    dashboard can open every order's PDF from disk, even after the exchange
    link stops working.
    """
    from .db import connect, init_db
    from .extract.pdf import cache_path, download_pdf
    from .sources.bse import BSEClient

    init_db()
    with connect() as conn:
        urls = [r["pdf_url"] for r in conn.execute(
            "SELECT DISTINCT a.pdf_url FROM orders o JOIN announcements a "
            "ON a.ann_id = o.ann_id WHERE a.pdf_url IS NOT NULL AND a.pdf_url <> ''")]
    missing = [u for u in urls if not cache_path(u).exists()]
    session = BSEClient().s
    got = sum(1 for u in missing if download_pdf(session, u))
    print(f"{len(urls)} order PDFs: {len(urls) - len(missing)} already cached, "
          f"{got} downloaded, {len(missing) - got} could not be fetched")
    return 0


def cmd_fundamentals(args) -> int:
    from .db import connect, init_db
    from .sources.fundamentals import resolve
    from .sources.nse import NSEClient
    init_db()
    with connect() as conn:
        company = {"company_id": f"NSE:{args.symbol.upper()}",
                   "nse_symbol": args.symbol.upper(), "bse_code": args.bse,
                   "isin": None, "name": args.symbol.upper()}
        conn.execute(
            "INSERT OR IGNORE INTO companies (company_id, nse_symbol, name) "
            "VALUES (?,?,?)",
            (company["company_id"], company["nse_symbol"], company["name"]))
        out = resolve(conn, company, nse_client=NSEClient(), force=True)
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_config(args) -> int:
    from dataclasses import asdict
    cfg = asdict(CONFIG)
    cfg["telegram_token"] = "***" if cfg["telegram_token"] else ""
    cfg["llm_api_key"] = "***" if cfg["llm_api_key"] else ""
    cfg["db_path"] = str(DB_PATH)
    cfg["dotenv"] = DOTENV_STATUS
    print(json.dumps(cfg, indent=2, default=str))
    return 0


def _sample_alert_text() -> str:
    """A realistic alert built through the real formatter."""
    from types import SimpleNamespace

    from .alert import format_message

    order = {
        "company_name": "Sample Engineering Ltd",
        "symbol": "SAMPLE",
        "order_value_inr": 4_256_000_000.0,
        "value_is_range": False,
        "customer": "NTPC",
        "customer_type": "psu",
        "execution_months": 18.0,
        "scope": "Supply, erection and commissioning of balance-of-plant packages.",
        "exchange": "NSE",
        "order_type": "loa",
        "filed_at": "2026-09-12T10:15:00",
        "pdf_url": "https://www.nseindia.com/",
    }
    score_res = SimpleNamespace(
        tier="HIGH", score=81.0,
        ratio_to_quarter=0.72, ratio_to_ttm=0.21, annualised_ratio=0.48,
        components={
            "annualised_impact": {"points": 32.0, "max": 40.0},
            "quarterly_ratio": {"points": 18.0, "max": 20.0},
            "firmness": {"points": 15.0, "max": 15.0},
        },
        penalties={}, data_quality="good", guidance={},
        reasons=["override: order is 72% of last quarter's revenue"],
    )
    funds = {"q_revenue_inr": 5.9e9, "ttm_revenue_inr": 2.0e10,
             "source": "sample"}
    company = {"nse_symbol": "SAMPLE", "industry": "Heavy Electrical Equipment"}
    return ("\U0001F9EA <i>test alert — order-scanner wiring check</i>\n\n"
            + format_message(order, score_res, funds, company))


def cmd_test_alert(args) -> int:
    """Verify the bot token, find the chat ID, send one sample alert."""
    from . import alert
    from .config import DOTENV_PATH

    print(f"dotenv: {DOTENV_STATUS}")

    token = args.token or CONFIG.telegram_token
    if not token:
        print("\nNo bot token.\n"
              "  1. In Telegram, message @BotFather and send /newbot.\n"
              "  2. Copy the token it gives you (looks like 123456:ABC-def...).\n"
              f"  3. Put it in {DOTENV_PATH} as:  TELEGRAM_BOT_TOKEN=<token>\n"
              "  4. Re-run this command.  (Or pass --token to try one now.)")
        return 1

    me, err = alert.verify_token(token)
    if err:
        print(f"\nTelegram rejected the token: {err}")
        return 1
    print(f"bot:    @{me.get('username')} ({me.get('first_name')}) — token OK")

    chat_id = args.chat_id or CONFIG.telegram_chat_id
    if not chat_id:
        found, err = alert.discover_chat_ids(token)
        if err:
            print(f"\nCould not read updates: {err}")
            return 1
        if not found:
            print("\nNo TELEGRAM_CHAT_ID set, and the bot has seen no messages.\n"
                  f"  Open Telegram, send any message to @{me.get('username')}\n"
                  "  (or add the bot to your group/channel and post once),\n"
                  "  then re-run this command.")
            return 1
        print("\nchats the bot can see:")
        for cid, label in found:
            print(f"  {cid:<16} {label}")
        chat_id = found[0][0]
        print(f"\nUsing {chat_id} for this test. To make it permanent, add to "
              f"{DOTENV_PATH}:\n  TELEGRAM_CHAT_ID={chat_id}")
    else:
        print(f"chat:   {chat_id}")

    text = args.text or _sample_alert_text()
    ok, err = alert.send_telegram(text, chat_id=chat_id, token=token)
    if not ok:
        print(f"\nSend failed: {err}")
        return 1
    print("\nSent. Check Telegram.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="order-scanner")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="one scan batch")
    s.add_argument("--lookback", type=int, default=None,
                   help="minutes to look back (default from config)")
    s.add_argument("--force", action="store_true", help="ignore market hours")
    s.add_argument("--deep", action="store_true", help="sweep all BSE categories")
    s.add_argument("--dry-run", action="store_true", help="store but do not alert")
    s.add_argument("--probe-pdfs", dest="probe_pdfs", action="store_true",
                   default=None,
                   help="open PDFs of generic-headline filings (default: on)")
    s.add_argument("--no-probe-pdfs", dest="probe_pdfs", action="store_false",
                   help="skip the generic-headline PDF probe")
    s.set_defaults(func=cmd_scan)

    b = sub.add_parser("backfill", help="seed history (no alerts)")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--probe-pdfs", dest="probe_pdfs", action="store_true",
                   help="also open generic-headline PDFs (~400 extra per day)")
    b.set_defaults(func=cmd_backfill, probe_pdfs=False)

    d = sub.add_parser("dashboard")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8000)
    d.set_defaults(func=cmd_dashboard)

    t = sub.add_parser("test-extract", help="run the extractor over text")
    t.add_argument("text", help="announcement text, or - for stdin")
    t.add_argument("--headline", default=None)
    t.set_defaults(func=cmd_test_extract)

    r = sub.add_parser("rescore", help="re-score stored orders after tuning")
    r.set_defaults(func=cmd_rescore)

    rc = sub.add_parser("recheck",
                        help="re-run the current extraction rules over stored "
                             "filings (no re-download) and drop what no longer "
                             "qualifies as an order")
    rc.set_defaults(func=cmd_recheck)

    rs = sub.add_parser("resummarize",
                        help="rebuild order descriptions from stored filing text")
    rs.set_defaults(func=cmd_resummarize)

    se = sub.add_parser("sectors",
                        help="look up company sector and industry (screener.in, BSE, yfinance)")
    se.add_argument("--all", action="store_true",
                    help="re-resolve every company with an order, not only missing ones")
    se.set_defaults(func=cmd_sectors)

    fp = sub.add_parser("fetch-pdfs", help="cache every stored order's filing PDF")
    fp.set_defaults(func=cmd_fetch_pdfs)

    f = sub.add_parser("fundamentals", help="resolve revenue for one symbol")
    f.add_argument("symbol")
    f.add_argument("--bse", default=None)
    f.set_defaults(func=cmd_fundamentals)

    c = sub.add_parser("config", help="print effective config")
    c.set_defaults(func=cmd_config)

    a = sub.add_parser("test-alert",
                       help="check the Telegram token, find the chat ID, "
                            "send one sample alert")
    a.add_argument("--token", default=None, help="override TELEGRAM_BOT_TOKEN")
    a.add_argument("--chat-id", default=None, help="override TELEGRAM_CHAT_ID")
    a.add_argument("--text", default=None,
                   help="send this text instead of the sample alert")
    a.set_defaults(func=cmd_test_alert)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
