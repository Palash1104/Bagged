"""BSE corporate announcements.

BSE is the more valuable feed for this use case: it has a dedicated
subcategory "Award of Order / Receipt of Order" under "Company Update",
so order filings can be pulled directly instead of keyword-sifted.
We still pull the wider net as well, because plenty of issuers file order
news under "Others" or a generic Company Update.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import requests

from ..config import CONFIG
from ..db import IST

log = logging.getLogger(__name__)

API = "https://api.bseindia.com/BseIndiaAPI/api"
ANN = f"{API}/AnnSubCategoryGetData/w"
ATTACH_LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
ATTACH_HIST = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"

HEADERS = {
    "User-Agent": CONFIG.user_agent,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/corporates/ann.html",
    "Connection": "keep-alive",
}

CAT_COMPANY_UPDATE = "Company Update"
SUBCAT_ORDER = "Award of Order / Receipt of Order"

# Categories worth scanning. "Company Update" and "Others" carry almost all
# order news; the rest are pulled only when you run a full backfill.
SCAN_CATEGORIES = [CAT_COMPANY_UPDATE, "Others", "-1"]

_TAG = re.compile(r"<[^>]+>")


class BSEClient:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def _get(self, url: str, params: dict):
        last_exc = None
        for attempt in range(CONFIG.max_retries):
            try:
                r = self.s.get(url, params=params, timeout=CONFIG.request_timeout)
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                last_exc = exc
                time.sleep(CONFIG.retry_backoff ** attempt)
        log.error("BSE GET failed %s %s: %s", url, params, last_exc)
        return None

    def announcements(self, from_dt: datetime, to_dt: datetime,
                      category: str = "-1", subcategory: str = "-1",
                      scrip: str = "", max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._get(ANN, {
                "pageno": page,
                "strCat": category,
                "subcategory": subcategory,
                "strPrevDate": from_dt.strftime("%Y%m%d"),
                "strToDate": to_dt.strftime("%Y%m%d"),
                "strSearch": "P",
                "strscrip": scrip,
                "strType": "C",
            })
            if not data:
                break
            rows = data.get("Table") or []
            if not rows:
                break
            out.extend(rows)
            total = 0
            meta = data.get("Table1") or []
            if meta and isinstance(meta[0], dict):
                total = int(meta[0].get("ROWCNT") or meta[0].get("TotalPageCnt") or 0)
            if total and len(out) >= total:
                break
            if len(rows) < 50:
                break
            time.sleep(0.4)
        return out


def _clean(html: str | None) -> str | None:
    if not html:
        return None
    text = _TAG.sub(" ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip() or None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d %b %Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def normalise(raw: dict) -> dict:
    def pick(*keys):
        for k in keys:
            v = raw.get(k)
            if v not in (None, "", "-"):
                return v
        return None

    attach = pick("ATTACHMENTNAME", "AttachmentName")
    pdf = None
    if attach:
        pdf = ATTACH_LIVE + str(attach).strip()

    filed = _parse_dt(pick("News_submission_dt", "NEWS_DT", "DT_TM"))
    scrip = pick("SCRIP_CD", "ScripCd")

    headline = _clean(pick("HEADLINE", "NEWSSUB", "NEWS_SUB"))
    body = _clean(pick("MORE", "NEWSBODY", "HEADLINE"))
    # BSE often duplicates headline into MORE; keep both only if they differ.
    if body and headline and body.startswith(headline[:60]):
        body = body

    return {
        "exchange": "BSE",
        "native_id": str(pick("NEWSID", "NewsId") or f"{scrip}|{filed}"),
        "symbol": None,
        "bse_code": str(scrip) if scrip else None,
        "company_name": pick("SLONGNAME", "SNAME", "Slongname"),
        "isin": pick("ISIN", "SC_ISIN"),
        "industry": pick("INDUSTRY", "SECTOR"),
        "category": pick("CATEGORYNAME", "Category"),
        "subcategory": pick("SUBCATNAME", "SubCategory"),
        "headline": headline or "",
        "body": body,
        "pdf_url": pdf,
        "filed_at": filed,
        "disseminated_at": _parse_dt(pick("DissemDT", "DT_TM")),
        "is_sme": False,
        "raw": raw,
    }


def fetch_window(client: BSEClient, from_dt: datetime, to_dt: datetime,
                 deep: bool = False) -> list[dict]:
    """Pull the order subcategory first, then a wider sweep for stragglers."""
    seen: dict[str, dict] = {}

    targeted = client.announcements(
        from_dt, to_dt, category=CAT_COMPANY_UPDATE, subcategory=SUBCAT_ORDER)
    for raw in targeted:
        n = normalise(raw)
        n["targeted_order_subcat"] = True
        seen[n["native_id"]] = n

    cats = SCAN_CATEGORIES if deep else [CAT_COMPANY_UPDATE, "Others"]
    for cat in cats:
        try:
            for raw in client.announcements(from_dt, to_dt, category=cat):
                n = normalise(raw)
                if n["native_id"] not in seen:
                    n["targeted_order_subcat"] = (
                        (n.get("subcategory") or "").strip() == SUBCAT_ORDER)
                    seen[n["native_id"]] = n
        except Exception as exc:
            log.error("BSE %s fetch failed: %s", cat, exc)

    return list(seen.values())
