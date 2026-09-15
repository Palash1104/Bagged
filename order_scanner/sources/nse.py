"""NSE corporate announcements + financial results.

NSE requires a cookie handshake: hit a normal HTML page first, keep the
cookies, then call /api/*. Cookies expire after a few minutes of idling, so
the session self-heals on a 401/403.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import requests

from ..config import CONFIG
from ..db import IST

log = logging.getLogger(__name__)

BASE = "https://www.nseindia.com"
API = f"{BASE}/api"

HEADERS = {
    "User-Agent": CONFIG.user_agent,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": f"{BASE}/companies-listing/corporate-filings-announcements",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
}

BOOTSTRAP = [
    f"{BASE}/",
    f"{BASE}/companies-listing/corporate-filings-announcements",
]


class NSEClient:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._last_bootstrap = 0.0

    # ------------------------------------------------------------------
    def bootstrap(self, force: bool = False) -> None:
        if not force and time.time() - self._last_bootstrap < 240:
            return
        for url in BOOTSTRAP:
            try:
                self.s.get(url, timeout=CONFIG.request_timeout)
            except requests.RequestException as exc:
                log.warning("NSE bootstrap %s failed: %s", url, exc)
        self._last_bootstrap = time.time()

    def _get(self, url: str, params: dict | None = None):
        self.bootstrap()
        last_exc = None
        for attempt in range(CONFIG.max_retries):
            try:
                r = self.s.get(url, params=params, timeout=CONFIG.request_timeout)
                if r.status_code in (401, 403, 404) and attempt == 0:
                    self.bootstrap(force=True)
                    continue
                r.raise_for_status()
                if "application/json" not in r.headers.get("content-type", ""):
                    self.bootstrap(force=True)
                    last_exc = ValueError("non-json response")
                    continue
                return r.json()
            except Exception as exc:
                last_exc = exc
                time.sleep(CONFIG.retry_backoff ** attempt)
        log.error("NSE GET failed %s %s: %s", url, params, last_exc)
        return None

    # ------------------------------------------------------------------
    def announcements(self, from_dt: datetime, to_dt: datetime,
                      index: str = "equities") -> list[dict]:
        data = self._get(f"{API}/corporate-announcements", {
            "index": index,
            "from_date": from_dt.strftime("%d-%m-%Y"),
            "to_date": to_dt.strftime("%d-%m-%Y"),
        })
        if isinstance(data, dict):
            data = data.get("data") or data.get("rows") or []
        return data if isinstance(data, list) else []

    def results_comparison(self, symbol: str) -> dict | None:
        """Last several quarters of P&L. NSE reports these in Rupees LAKHS."""
        return self._get(f"{API}/results-comparision", {"symbol": symbol})

    def quote(self, symbol: str) -> dict | None:
        return self._get(f"{API}/quote-equity", {"symbol": symbol})

    def equity_master(self) -> list[dict]:
        data = self._get(f"{API}/equity-stockIndices", {"index": "SECURITIES IN F&O"})
        return (data or {}).get("data", []) if isinstance(data, dict) else []


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                "%d-%b-%Y", "%Y-%m-%d", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def normalise(raw: dict) -> dict:
    """NSE's field names drift; read defensively."""
    def pick(*keys):
        for k in keys:
            v = raw.get(k)
            if v not in (None, "", "-"):
                return v
        return None

    filed = _parse_dt(pick("an_dt", "sort_date", "exchdisstime", "bdate"))
    pdf = pick("attchmntFile", "attchmntfile", "attachmentFile")
    if pdf and pdf.startswith("/"):
        pdf = "https://nsearchives.nseindia.com" + pdf

    symbol = pick("symbol", "SYMBOL")
    return {
        "exchange": "NSE",
        "native_id": str(pick("seqId", "seq_id", "orgid") or f"{symbol}|{filed}"),
        "symbol": symbol,
        "company_name": pick("sm_name", "smName", "companyName", "comp"),
        "isin": pick("sm_isin", "isin"),
        "industry": pick("smIndustry", "industry"),
        "category": pick("desc", "subject", "attchmntText_desc"),
        "subcategory": pick("subject", "desc"),
        "headline": (pick("desc", "subject") or "") ,
        "body": pick("attchmntText", "attchmnttext", "smText", "desc"),
        "pdf_url": pdf,
        "filed_at": filed,
        "disseminated_at": _parse_dt(pick("exchdisstime", "sort_date")),
        "raw": raw,
    }


def fetch_window(client: NSEClient, from_dt: datetime, to_dt: datetime,
                 include_sme: bool = True) -> list[dict]:
    rows: list[dict] = []
    indices = ["equities"] + (["sme"] if include_sme else [])
    for idx in indices:
        try:
            for raw in client.announcements(from_dt, to_dt, index=idx):
                n = normalise(raw)
                n["is_sme"] = idx == "sme"
                rows.append(n)
        except Exception as exc:
            log.error("NSE %s fetch failed: %s", idx, exc)
    return rows
