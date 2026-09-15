"""The last completed financial year's revenue and profit, from screener.in.

Guidance only means something against a base. "Total Income of Rs 105 crores
in FY27" is a tripling for Veerhealth (FY26 sales Rs 32.48 Cr plus other income
Rs 0.49 Cr) and a slowdown for a Rs 200 Cr company. screener.in's annual
profit-and-loss table gives exactly that base, by year, for main-board and SME
listings alike. It is fetched only for filings that carry guidance, and cached
on the company's fundamentals row.
"""
from __future__ import annotations

import html
import logging
import re

import requests

from ..config import CONFIG
from ..db import upsert
from ..extract.guidance import fiscal_year_end
from .sector import SCREENER_COMPANY

log = logging.getLogger(__name__)

_SECTION = re.compile(r'<section id="profit-loss".*?</section>', re.S)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_YEAR_COL = re.compile(r"^[A-Z][a-z]{2} (\d{4})$")      # "Mar 2026"
_LINES = {
    "sales": re.compile(r"^(?:sales|revenue)\b", re.IGNORECASE),
    "other_income": re.compile(r"^other income\b", re.IGNORECASE),
    "net_profit": re.compile(r"^net profit\b", re.IGNORECASE),
}


def _text(cell: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>|\s+", " ", cell)).strip()


def _crore(cell: str) -> float | None:
    try:
        return float(cell.replace(",", "")) * 1e7        # screener reports Rs crore
    except ValueError:
        return None


def from_screener(key: str) -> dict[int, dict]:
    """{year: {"sales", "other_income", "net_profit"}} in rupees, by year ended."""
    try:
        r = requests.get(SCREENER_COMPANY.format(key=key),
                         headers={"User-Agent": CONFIG.user_agent, "Accept": "text/html"},
                         timeout=CONFIG.request_timeout)
        r.raise_for_status()
    except Exception as exc:
        log.debug("screener annuals %s failed: %s", key, exc)
        return {}
    section = _SECTION.search(r.text)
    if not section:
        return {}
    rows = [[_text(c) for c in _CELL.findall(row)] for row in _ROW.findall(section.group(0))]
    header = next((row for row in rows if any(_YEAR_COL.match(c) for c in row)), None)
    if not header:
        return {}
    years = [int(m.group(1)) if (m := _YEAR_COL.match(c)) else None for c in header]
    out: dict[int, dict] = {}
    for row in rows:
        if not row:
            continue
        line = next((name for name, rx in _LINES.items() if rx.match(row[0])), None)
        if not line:
            continue
        for year, cell in zip(years, row):
            value = _crore(cell) if year else None
            if value is not None:
                out.setdefault(year, {})[line] = value
    return out


def ensure(conn, company: dict, funds: dict | None, filed_at) -> dict | None:
    """Add the last completed year's figures to `funds`, fetching them if needed."""
    if not CONFIG.fetch_annuals:
        return funds
    want = fiscal_year_end(filed_at) - 1
    if funds and funds.get("last_fy") == want and funds.get("last_fy_revenue_inr"):
        return funds
    key = company.get("nse_symbol") or company.get("bse_code")
    if not key:
        return funds
    years = from_screener(str(key))
    done = sorted((y for y in years if y <= want and years[y].get("sales")), reverse=True)
    if not done:
        return funds
    fy = years[done[0]]
    row = {"company_id": company["company_id"], "last_fy": done[0],
           "last_fy_revenue_inr": fy.get("sales"),
           "last_fy_other_income_inr": fy.get("other_income"),
           "last_fy_pat_inr": fy.get("net_profit")}
    upsert(conn, "fundamentals", row, "company_id")
    return {**(funds or {}), **row}
