"""BSE feed: the client BSE accepts since late Sep 2026, the 403 fallbacks, the
probe, and the dead-feed warning. The network is replaced by a fake BSE that
behaves the way the real one did on 18 Oct 2026: Chrome (curl_cffi) with the
site-root Referer gets 200, python-requests gets 403."""
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

os.environ["ORDER_SCANNER_DATA"] = tempfile.mkdtemp(prefix="bse-feed-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from order_scanner import pipeline  # noqa: E402
from order_scanner.config import CONFIG, DATA_DIR  # noqa: E402
from order_scanner.db import connect, init_db, now_ist  # noqa: E402
from order_scanner.sources import bse  # noqa: E402

CONFIG.retry_backoff = 1.0
CONFIG.max_retries = 2


class Resp:
    def __init__(self, code, data=None, content=b""):
        self.status_code, self._data, self.content = code, data or {}, content
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error: Forbidden")

    def json(self):
        return self._data


ROW = {"NEWSID": "n1", "SCRIP_CD": "500001", "SLONGNAME": "Alpha Ltd", "HEADLINE": "x",
       "News_submission_dt": "2026-10-18T10:00:00", "CATEGORYNAME": "Company Update",
       "ATTACHMENTNAME": "abc.pdf"}
SITE = "https://www.bseindia.com"
calls: list[dict] = []


class FakeSession:
    """Stands in for both transports. BSE's verdict is `accept(transport,
    headers, warmed)`; headers set to None are absent, as in both libraries."""
    accept = staticmethod(lambda t, h, w: True)

    def __init__(self, transport):
        self.transport, self.headers, self.warmed = transport, {}, False

    def get(self, url, params=None, timeout=None, headers=None, **kw):
        h = {k: v for k, v in {**self.headers, **(headers or {})}.items() if v is not None}
        calls.append({"transport": self.transport, "url": url, "headers": h})
        if url == SITE + "/":
            self.warmed = True
            return Resp(200)
        if not FakeSession.accept(self.transport, h, self.warmed):
            return Resp(403)
        if url.endswith(".pdf"):
            return Resp(200, content=b"%PDF-1.7 ...")
        return Resp(200, {"Table": [ROW], "Table1": [{"ROWCNT": 1}]})


bse.new_session = FakeSession


def with_curl_cffi(present):
    bse.cffi_requests = object() if present else None


def run(accept, curl=True):
    calls.clear()
    FakeSession.accept = staticmethod(accept)
    with_curl_cffi(curl)
    c = bse.BSEClient()
    rows = c.announcements(now_ist() - timedelta(days=1), now_ist())
    return c, rows


# BSE as it is now (checked 18 Oct 2026): Chrome gets 200 without cookies,
# python-requests gets 403 whatever its headers; the full-path Referer that
# broke first is refused too.
def bse_now(t, h, w):
    return (t == "chrome" and h.get("Referer") == SITE + "/" and h.get("Origin") == SITE)


def main():
    checks = []
    c, rows = run(bse_now)
    api_call = next(x for x in calls if "AnnSubCategoryGetData" in x["url"])
    checks += [
        ("with curl_cffi, BSE as it is now answers the first strategy",
         len(rows) == 1 and not c.dead and c.strategy.name == "chrome"),
        ("the API call goes through the Chrome transport", api_call["transport"] == "chrome"),
        ("it sends the site root as Referer, and an Origin",
         api_call["headers"].get("Referer") == SITE + "/" and api_call["headers"].get("Origin") == SITE),
        ("it sends a fetch()'s Sec-Fetch headers, not a navigation's",
         api_call["headers"].get("Sec-Fetch-Mode") == "cors"
         and api_call["headers"].get("Sec-Fetch-Site") == "same-site"
         and "Sec-Fetch-User" not in api_call["headers"]
         and "Upgrade-Insecure-Requests" not in api_call["headers"]),
        ("the Chrome transport keeps its own User-Agent", "User-Agent" not in api_call["headers"]),
        ("no warm-up visit when it is not needed", not any(x["url"] == SITE + "/" for x in calls)),
        ("the old full-path Referer would be refused",
         not bse_now("chrome", {"Referer": SITE + "/corporates/ann.html", "Origin": SITE}, False)),
    ]

    # Without curl_cffi only python-requests is left, and BSE refuses it.
    c, rows = run(bse_now, curl=False)
    checks += [
        ("without curl_cffi the Chrome strategies are skipped",
         all(s.transport == "requests" for s in c.strategies)),
        ("...and BSE as it is now refuses python-requests -> client.dead",
         rows == [] and c.dead and c.refused),
    ]
    with_curl_cffi(False)
    r = bse.check()
    checks.append(("check() says curl_cffi is missing", r["ok"] is False and r["chrome_transport"] is False))

    # If BSE starts wanting its cookies first, the warm-up strategy finds it.
    c, rows = run(lambda t, h, w: t == "chrome" and w)
    warm = next((x for x in calls if x["url"] == SITE + "/"), None)
    checks += [
        ("cookie-wanting BSE: falls to chrome+cookies and succeeds",
         len(rows) == 1 and c.strategy.name == "chrome+cookies" and c.refused == ["chrome"]),
        ("the warm-up is a plain page visit (no Origin/Referer, navigate mode)",
         warm and "Origin" not in warm["headers"] and "Referer" not in warm["headers"]
         and warm["headers"].get("Sec-Fetch-Mode") == "navigate"),
    ]

    # A requests-friendly BSE still works through the requests fallbacks.
    c, rows = run(lambda t, h, w: t == "requests", curl=False)
    checks.append(("requests strategy works when BSE accepts it",
                   len(rows) == 1 and c.strategy.name == "requests+browser-headers"))

    # Refusing everything: the client reports itself dead.
    c, rows = run(lambda t, h, w: False)
    checks.append(("refused everything -> client.dead",
                   rows == [] and c.dead and c.last_error
                   and len(set(c.refused)) == len(c.strategies)))

    # The probe tries each strategy once and reports its status and a PDF.
    FakeSession.accept = staticmethod(bse_now)
    with_curl_cffi(True)
    pr = {r["strategy"]: r for r in bse.probe()}
    checks.append(("probe: chrome 200 with rows and a PDF, requests 403",
                   pr["chrome"]["status"] == 200 and pr["chrome"]["rows"] == 1
                   and pr["chrome"]["pdf"] == "ok"
                   and pr["requests+browser-headers"]["status"] == 403))
    with_curl_cffi(False)
    pr = {r["strategy"]: r for r in bse.probe()}
    checks.append(("probe without curl_cffi says so", "not installed" in (pr["chrome"]["error"] or "")))

    # Sector lookups use the same accepted session.
    from order_scanner.sources import sector
    with_curl_cffi(True)
    bse._SHARED = None
    calls.clear()
    sector.from_bse("500001")
    checks.append(("sector lookup goes through the shared Chrome session",
                   calls and calls[-1]["transport"] == "chrome"))

    # PDFs come through a streamed curl_cffi response, whose .content is
    # empty: the downloader must read the stream (bug of 18 Oct, 0/934 PDFs).
    from order_scanner.extract.pdf import download_pdf

    class Streamed:
        status_code, headers, content = 200, {}, b""
        def raise_for_status(self): pass
        def close(self): pass
        def iter_content(self, chunk_size=1):
            yield b"%PDF-1.7 "
            yield b"rest of file"

    class StreamSession:
        def get(self, url, **kw):
            return Streamed()
    got = download_pdf(StreamSession(), "https://example.test/streamed.pdf", cache=False)
    checks.append(("a streamed PDF with an empty .content is still read",
                   got == b"%PDF-1.7 rest of file"))

    # BSE moves attachments from AttachLive to AttachHis after 2-3 weeks; the
    # old link 404s (seen 18 Oct on every Jun-Aug filing during recheck).
    from order_scanner.extract.pdf import cache_path

    class Moved:
        asked = []
        def get(self, url, **kw):
            self.asked.append(url)
            if "/AttachLive/" in url:
                return Resp(404)
            r = Streamed()
            return r
    live = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/moved-1.pdf"
    ms = Moved()
    got = download_pdf(ms, live)
    checks += [
        ("a 404 on AttachLive is retried on AttachHis", got == b"%PDF-1.7 rest of file"
         and ms.asked == [live, live.replace("/AttachLive/", "/AttachHis/")]),
        ("the moved PDF is cached under the URL as stored", cache_path(live).exists()),
    ]
    ms.asked.clear()
    download_pdf(ms, live)
    checks.append(("a second read comes from the cache, no download", ms.asked == []))
    ms.asked.clear()
    download_pdf(ms, "https://nsearchives.nseindia.com/corporate/x.pdf", cache=False)
    checks.append(("a non-BSE link is tried once", len(ms.asked) == 1))

    # 5. The pipeline records a dead feed and warns once on the third run.
    init_db()
    sent = []
    pipeline.send_telegram = lambda text: (sent.append(text), (True, None))[1]
    pipeline.nse_src.NSEClient = lambda *a, **k: None
    pipeline.nse_src.fetch_window = lambda *a, **k: []
    FakeSession.accept = staticmethod(lambda t, h, w: False)
    with_curl_cffi(True)
    after = []
    for _ in range(4):
        pipeline.run_scan(force=True, dry_run=False, lookback_minutes=30)
        after.append(len(sent))
    with connect() as conn:
        errs = [json.loads(r["errors"]) for r in conn.execute("SELECT errors FROM runs ORDER BY run_id")]
    checks += [
        ("each run records 'BSE feed down' in runs.errors",
         all(any("BSE feed down" in e for e in es) for es in errs)),
        ("no warning before three failed runs, one on the third", after[:3] == [0, 0, 1]),
        ("exactly one warning across four failed runs (daily limit)", len(sent) == 1),
        ("the warning names BSE and the check command",
         sent and "BSE" in sent[0] and "bse-check" in sent[0]),
        ("the daily marker was written", (DATA_DIR / pipeline.FEED_WARNING_MARK).exists()),
    ]

    print(f"{'CHECKS':-^78}")
    failed = 0
    for label, ok in checks:
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    print(f"\n{len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
