"""Company classification, for the label next to the company name.

The NSE/BSE industry classification has four levels, which screener.in shows
on every company page, e.g. for Veerhealth Care:

    Healthcare > Healthcare > Pharmaceuticals & Biotechnology > Pharmaceuticals
    (broad sector)  (sector)   (broad industry)                  (industry)

The dashboard labels a company with the finest level -- `industry`
("Pharmaceuticals", "Railway Wagons", "Civil Construction") -- and colours the
label by the broader `sector` ("Healthcare", "Capital Goods"), so the colour
groups stay meaningful while the label says what the company actually does.

Sources, in order:

  1. screener.in's company page, keyed by NSE symbol or BSE code. It covers
     main-board and SME listings alike, with no login;
  2. BSE's quote header, for a company with a BSE code that screener.in does
     not answer for (IndustryNew = sector, ISubGroup = industry);
  3. yfinance, as a last resort -- a different taxonomy ("Consumer Cyclical"),
     so only when neither exchange-based source answers.

A lookup is made once per company during scans. An empty string records
"looked up, nothing found", so a scan does not retry it; the `sectors`
command does, and `sectors --all` refreshes every company with an order.
"""
from __future__ import annotations

import html
import logging
import re

import requests

from ..config import CONFIG
from .bse import API, HEADERS

log = logging.getLogger(__name__)

SCREENER_COMPANY = "https://www.screener.in/company/{key}/"
# <a href="/market/..." title="Sector">Healthcare</a> ... title="Industry">Pharmaceuticals<
_SCREENER_LABEL = re.compile(r'title="(Sector|Industry)"[^>]*>\s*([^<]+?)\s*<')


def _label(s: str | None) -> str | None:
    """'Telecom -  Equipment &amp; Accessories' -> 'Telecom - Equipment & Accessories'."""
    s = re.sub(r"\s+", " ", html.unescape(s or "")).strip()
    return s or None


def from_screener(key: str) -> tuple[str | None, str | None]:
    """(sector, industry) from a screener.in company page (NSE symbol or BSE code)."""
    try:
        r = requests.get(SCREENER_COMPANY.format(key=key),
                         headers={"User-Agent": CONFIG.user_agent, "Accept": "text/html"},
                         timeout=CONFIG.request_timeout)
        r.raise_for_status()
    except Exception as exc:
        log.debug("screener sector %s failed: %s", key, exc)
        return None, None
    found = {m.group(1): _label(m.group(2)) for m in _SCREENER_LABEL.finditer(r.text)}
    return found.get("Sector"), found.get("Industry")


def from_bse(bse_code: str) -> tuple[str | None, str | None]:
    try:
        r = requests.get(f"{API}/ComHeadernew/w",
                         params={"quotetype": "EQ", "scripcode": bse_code, "seriesid": ""},
                         headers=HEADERS, timeout=CONFIG.request_timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # network, non-JSON, unknown scrip
        log.debug("bse sector %s failed: %s", bse_code, exc)
        return None, None
    if not isinstance(data, dict):
        return None, None
    return (_label(data.get("IndustryNew") or data.get("Sector")),
            _label(data.get("ISubGroup") or data.get("Industry")))


def from_yfinance(nse_symbol: str) -> tuple[str | None, str | None]:
    try:
        import yfinance as yf
        info = yf.Ticker(f"{nse_symbol}.NS").info or {}
    except Exception as exc:
        log.debug("yfinance sector %s failed: %s", nse_symbol, exc)
        return None, None
    return _label(info.get("sector")), _label(info.get("industry"))


def resolve(company: dict) -> tuple[str | None, str | None]:
    """(sector, industry): screener.in, then BSE, then yfinance."""
    sector = industry = None
    key = company.get("nse_symbol") or company.get("bse_code")
    if key:
        sector, industry = from_screener(str(key))
    if not industry and company.get("bse_code"):
        sector, industry = from_bse(str(company["bse_code"]))
    if not (sector or industry) and company.get("nse_symbol"):
        sector, industry = from_yfinance(company["nse_symbol"])
    return sector, industry


def store(conn, company_id: str, sector: str | None, industry: str | None,
          replace_industry: bool = False) -> None:
    """Save the lookup. An industry label the exchange feed already supplied is
    kept, unless this is a deliberate full refresh."""
    if replace_industry and industry:
        conn.execute("UPDATE companies SET sector = ?, industry = ? WHERE company_id = ?",
                     (sector or "", industry, company_id))
    else:
        conn.execute(
            "UPDATE companies SET sector = ?, industry = COALESCE(NULLIF(industry, ''), ?) "
            "WHERE company_id = ?", (sector or "", industry, company_id))


def ensure(conn, company: dict) -> None:
    """Look the classification up during a scan, once per company."""
    if not CONFIG.fetch_sectors:
        return
    row = conn.execute("SELECT sector FROM companies WHERE company_id = ?",
                       (company["company_id"],)).fetchone()
    if row is None or row["sector"] is not None:
        return
    # A new company has no feed industry worth keeping over screener.in's.
    store(conn, company["company_id"], *resolve(company), replace_industry=True)
