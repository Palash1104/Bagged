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
from dataclasses import dataclass
from datetime import datetime

import requests

try:                                    # optional: a Chrome-identical TLS handshake
    from curl_cffi import requests as cffi_requests
except ImportError:                     # pragma: no cover - depends on the install
    cffi_requests = None

from ..config import CONFIG
from ..db import IST

log = logging.getLogger(__name__)

API = "https://api.bseindia.com/BseIndiaAPI/api"
ANN = f"{API}/AnnSubCategoryGetData/w"
ATTACH_LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
ATTACH_HIST = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"

SITE = "https://www.bseindia.com"

# Why BSE has answered 403 since 30 Sep 2026
# -------------------------------------------
# Checked on 18 Oct 2026 from the same PC: Chrome on bseindia.com/corporates/ann
# gets 200 from this exact API, with no cookies at all, while python-requests
# with the same headers gets 403. So the IP is not blocked and cookies are not
# needed. BSE's bot filter (Akamai) is refusing the *client*: python-requests
# has a TLS handshake no browser has. curl_cffi makes the handshake Chrome's,
# so it is tried first. The site-root Referer and the Origin header matter too
# (a full-path Referer has been refused since ~23 Sep), and the Sec-Fetch /
# client-hint headers are the ones Chrome sends on this call.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

# What Chrome sends on the page's own call to api.bseindia.com.
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": SITE,
    "Referer": SITE + "/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}
# python-requests has to state the browser identity itself; curl_cffi brings
# its own User-Agent and client hints that match its handshake, and overriding
# them would make the two disagree.
BROWSER_ID = {
    "User-Agent": _UA,
    "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
HEADERS = {**BROWSER_ID, **API_HEADERS}          # also used by sources.sector
# curl_cffi adds a page navigation's headers by default; a fetch() never sends
# these two, so they are switched off (None removes a header in curl_cffi).
CHROME_API_HEADERS = {**API_HEADERS, "Sec-Fetch-User": None, "Upgrade-Insecure-Requests": None}
# The set used up to 18 Oct, kept as the last resort.
LEGACY_HEADERS = {"User-Agent": _UA, "Accept": "application/json, text/plain, */*",
                  "Accept-Language": "en-US,en;q=0.9", "Origin": SITE,
                  "Referer": SITE + "/", "Connection": "keep-alive"}


@dataclass(frozen=True)
class Strategy:
    name: str
    transport: str          # "chrome" (curl_cffi) or "requests"
    headers: dict
    warm_up: bool = False   # open www.bseindia.com first, to collect its cookies


# Tried in this order; the first one BSE answers is kept for the session.
STRATEGIES = [
    Strategy("chrome", "chrome", CHROME_API_HEADERS),
    Strategy("chrome+cookies", "chrome", CHROME_API_HEADERS, warm_up=True),
    Strategy("requests+browser-headers", "requests", HEADERS, warm_up=True),
    Strategy("requests-legacy", "requests", LEGACY_HEADERS),
]


def have_chrome_transport() -> bool:
    return cffi_requests is not None


def new_session(transport: str):
    """A session for `transport`. Tests replace this to fake BSE."""
    if transport == "chrome":
        if cffi_requests is None:
            raise RuntimeError("curl_cffi is not installed (pip install curl_cffi)")
        return cffi_requests.Session(impersonate="chrome")
    return requests.Session()


def _available(strategies=None) -> list[Strategy]:
    return [s for s in (strategies or STRATEGIES)
            if s.transport != "chrome" or have_chrome_transport()]

CAT_COMPANY_UPDATE = "Company Update"
SUBCAT_ORDER = "Award of Order / Receipt of Order"

# Categories worth scanning. "Company Update" and "Others" carry almost all
# order news; the rest are pulled only when you run a full backfill.
SCAN_CATEGORIES = [CAT_COMPANY_UPDATE, "Others", "-1"]

_TAG = re.compile(r"<[^>]+>")


class BSEClient:
    """BSE's announcement API, through the first strategy BSE answers.

    `.s` is the live session; the PDF downloaders borrow it, so the files on
    www.bseindia.com are fetched with the same client BSE accepted.
    """

    def __init__(self, strategies: list[Strategy] | None = None) -> None:
        self.strategies = _available(strategies)
        if not have_chrome_transport():
            log.info("curl_cffi not installed: BSE calls go through python-requests, "
                     "which BSE has refused since 30 Sep 2026 (pip install curl_cffi)")
        self.variant = -1
        self.s = None
        self._use_variant(0)
        # Feed health, read by the pipeline: a run where every call failed is
        # reported in `runs.errors` and, if it persists, sent as a warning.
        self.ok_calls = 0
        self.failed_calls = 0
        self.last_error: str | None = None
        self.refused: list[str] = []       # strategies BSE answered 403 this session

    @property
    def strategy(self) -> Strategy:
        return self.strategies[self.variant]

    def _use_variant(self, i: int) -> None:
        st = self.strategies[i]
        self.variant = i
        self.s = new_session(st.transport)
        self.s.headers.update(st.headers)
        self._warmed = False

    def _warm_up(self) -> None:
        if self._warmed or not self.strategy.warm_up:
            return
        self._warmed = True
        try:
            # A navigation, as a browser makes it, so Akamai sets its cookies.
            self.s.get(SITE + "/", timeout=CONFIG.request_timeout, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1", "Origin": None, "Referer": None})
        except Exception as exc:          # the API call will say whether it mattered
            log.debug("BSE warm-up failed: %s", exc)

    def _get(self, url: str, params: dict):
        last_exc = None
        attempt = 0
        while attempt < CONFIG.max_retries:
            try:
                self._warm_up()
                r = self.s.get(url, params=params, timeout=CONFIG.request_timeout)
                if r.status_code == 403:
                    self.refused.append(self.strategy.name)
                    if self.variant + 1 < len(self.strategies):
                        # Refused: move to the next strategy straight away, and
                        # keep it for the rest of this session if it works.
                        log.warning("BSE refused %s (403); trying %s", self.strategy.name,
                                    self.strategies[self.variant + 1].name)
                        self._use_variant(self.variant + 1)
                        continue
                r.raise_for_status()
                data = r.json()
                self.ok_calls += 1
                return data
            except Exception as exc:
                last_exc = exc
                time.sleep(CONFIG.retry_backoff ** attempt)
            attempt += 1
        self.failed_calls += 1
        self.last_error = str(last_exc)[:160]
        log.error("BSE GET failed %s %s: %s", url, params, last_exc)
        return None

    @property
    def dead(self) -> bool:
        """Every call this session failed: the feed is down, not just quiet."""
        return self.failed_calls > 0 and self.ok_calls == 0

    def announcements(self, from_dt: datetime, to_dt: datetime,
                      category: str = "-1", subcategory: str = "-1",
                      scrip: str = "", max_pages: int = 60) -> list[dict]:
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._get(ANN, {
                "pageno": page,
                "strCat": category,
                "subcategory": subcategory,
                "strPrevDate": from_dt.strftime("%Y%m%d"),
                "strToDate": to_dt.strftime("%Y%m%d"),
                "strSearch": "P",
                "strScrip": scrip,
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
            time.sleep(0.6)
        return out


_SHARED: BSEClient | None = None


def shared_session():
    """One BSE session per process for odd calls (sector lookups)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = BSEClient()
    return _SHARED.s


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


def check(days: int = 2) -> dict:
    """Is the BSE feed answering? One targeted call over the last `days`."""
    from datetime import timedelta

    from ..db import now_ist
    client = BSEClient()
    to_dt = now_ist()
    rows = client.announcements(to_dt - timedelta(days=days), to_dt,
                                category=CAT_COMPANY_UPDATE, subcategory="-1", max_pages=1)
    return {"ok": not client.dead, "rows": len(rows), "header_set": client.variant,
            "strategy": client.strategy.name, "refused": client.refused,
            "chrome_transport": have_chrome_transport(), "error": client.last_error}


def probe(days: int = 2) -> list[dict]:
    """Try every strategy on its own, once, and say what BSE answered to each:
    one announcements call, then one PDF from www.bseindia.com when it worked."""
    from datetime import timedelta

    from ..db import now_ist
    to_dt = now_ist()
    params = {"pageno": 1, "strCat": CAT_COMPANY_UPDATE, "subcategory": "-1",
              "strPrevDate": (to_dt - timedelta(days=days)).strftime("%Y%m%d"),
              "strToDate": to_dt.strftime("%Y%m%d"), "strSearch": "P",
              "strScrip": "", "strType": "C"}
    out = []
    for st in STRATEGIES:
        res = {"strategy": st.name, "status": None, "rows": None, "pdf": None, "error": None}
        out.append(res)
        if st.transport == "chrome" and not have_chrome_transport():
            res["error"] = "curl_cffi not installed"
            continue
        try:
            c = BSEClient([st])
            c._warm_up()
            r = c.s.get(ANN, params=params, timeout=CONFIG.request_timeout)
            res["status"] = r.status_code
            if r.status_code == 200:
                rows = (r.json() or {}).get("Table") or []
                res["rows"] = len(rows)
                pdf = next((normalise(x)["pdf_url"] for x in rows if x.get("ATTACHMENTNAME")), None)
                if pdf:
                    p = c.s.get(pdf, timeout=CONFIG.request_timeout)
                    ok = p.status_code == 200 and p.content[:4] == b"%PDF"
                    res["pdf"] = "ok" if ok else str(p.status_code)
        except Exception as exc:
            res["error"] = str(exc)[:120]
    return out


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
