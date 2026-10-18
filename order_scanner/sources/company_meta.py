"""Market capitalisation and last-full-year revenue, for the Orderbook view.

Both come from one screener.in company page, which covers main-board and SME
listings alike. The consolidated page is tried first -- an order book is
almost always stated for the group, so it should be set against group
revenue -- and the standalone page when the company has no consolidated
accounts. Which one was used is stored, and the dashboard shows it.

The orders table never had a market cap (yfinance's fast_info has been
returning nothing), so this is also what fills `companies.market_cap_inr`.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timedelta

import requests

from ..config import CONFIG
from ..db import now_ist, upsert
from .annual import parse_annuals

log = logging.getLogger(__name__)

PAGES = ("https://www.screener.in/company/{key}/consolidated/",
         "https://www.screener.in/company/{key}/")

# <span class="name"> Market Cap </span> <span class="nowrap value"> ₹ <span class="number">26,076</span> Cr.
_MCAP = re.compile(r"Market\s+Cap\s*</span>.{0,200}?<span class=\"number\">\s*([\d,.]+)\s*</span>\s*"
                   r"(Cr|Lakh|Lac)?", re.S | re.I)
_BASIS = re.compile(r"(Consolidated|Standalone)\s+Figures", re.I)


def parse_market_cap(page: str) -> float | None:
    m = _MCAP.search(page)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "Cr").lower()
    return num * (1e5 if unit.startswith("la") else 1e7)


def parse_basis(page: str, url: str) -> str:
    m = _BASIS.search(page)
    if m:
        return m.group(1).lower()
    return "consolidated" if "/consolidated/" in url else "standalone"


def fetch(key: str, patient: bool = False) -> dict | None:
    """{market_cap_inr, last_fy, last_fy_revenue_inr, revenue_basis}, or None.
    `patient` waits out a 429 (bulk refreshes); a scan makes one attempt."""
    out: dict = {}
    for tmpl in PAGES:
        url = tmpl.format(key=key)
        try:
            r = None
            # screener.in answers 429 to a long run of requests (91 of 217
            # companies came back empty on 18 Oct): wait and try again.
            for wait in ((0, 15, 45) if patient else (0,)):
                time.sleep(wait)
                r = requests.get(url, headers={"User-Agent": CONFIG.user_agent, "Accept": "text/html"},
                                 timeout=CONFIG.request_timeout)
                if r.status_code != 429:
                    break
            if r.status_code == 404:
                continue
            r.raise_for_status()
        except Exception as exc:
            log.debug("screener meta %s failed: %s", url, exc)
            continue
        page = html.unescape(r.text)
        out.setdefault("market_cap_inr", parse_market_cap(page))
        years = parse_annuals(r.text)
        done = sorted((y for y in years if years[y].get("sales")), reverse=True)
        if done:
            out.update({"last_fy": done[0], "last_fy_revenue_inr": years[done[0]]["sales"],
                        "revenue_basis": parse_basis(page, url)})
            break
    return out or None


def _stale(stamp: str | None, max_age_days: int) -> bool:
    if not stamp:
        return True
    try:
        return now_ist() - datetime.fromisoformat(stamp) > timedelta(days=max_age_days)
    except ValueError:
        return True


def ensure(conn, company: dict, max_age_days: int | None = None, force: bool = False,
           patient: bool = False) -> bool:
    """Refresh one company's market cap and revenue when they are missing or old.
    Returns True if a fetch was made."""
    if not CONFIG.fetch_company_meta and not force:
        return False
    max_age = CONFIG.company_meta_max_age_days if max_age_days is None else max_age_days
    cid = company["company_id"]
    row = conn.execute("SELECT f.meta_as_of, c.market_cap_inr FROM fundamentals f "
                       "LEFT JOIN companies c ON c.company_id = f.company_id "
                       "WHERE f.company_id=?", (cid,)).fetchone()
    # A row stamped without a market cap is a failed fetch from before
    # failures were left unstamped: retry it rather than wait out max_age.
    if (not force and row and row["market_cap_inr"]
            and not _stale(row["meta_as_of"], max_age)):
        return False
    key = company.get("nse_symbol") or company.get("bse_code")
    if not key:
        return False
    meta = fetch(str(key), patient=patient) or {}
    if not (meta.get("market_cap_inr") or meta.get("last_fy_revenue_inr")):
        # Nothing read (blocked, page changed, unknown symbol): leave the row
        # unstamped so the next `orderbook meta` retries it instead of waiting
        # out company_meta_max_age_days.
        return True
    stamp = now_ist().isoformat()
    fund = {"company_id": cid, "meta_as_of": stamp}
    if meta.get("last_fy_revenue_inr"):
        fund.update({"last_fy": meta["last_fy"],
                     "last_fy_revenue_inr": meta["last_fy_revenue_inr"],
                     "revenue_basis": meta["revenue_basis"]})
    upsert(conn, "fundamentals", fund, "company_id")
    if meta.get("market_cap_inr"):
        conn.execute("UPDATE companies SET market_cap_inr=?, market_cap_as_of=? WHERE company_id=?",
                     (meta["market_cap_inr"], stamp, cid))
    return True


def refresh_many(conn, companies: list[dict], max_age_days: int | None = None,
                 force: bool = False, pause: float = 1.0) -> dict:
    """Refresh a list of companies, politely spaced for screener.in."""
    st = {"fetched": 0, "skipped": 0, "with_mcap": 0}
    for c in companies:
        if ensure(conn, c, max_age_days=max_age_days, force=force, patient=True):
            st["fetched"] += 1
            conn.commit()
            time.sleep(pause)
        else:
            st["skipped"] += 1
    st["with_mcap"] = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE market_cap_inr > 0").fetchone()[0]
    return st
