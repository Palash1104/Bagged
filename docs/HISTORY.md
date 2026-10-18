# Bagged — decision log and incidents

This file records why things are the way they are. It is newest last. Each
entry gives the problem, what was found and what changed. Read it before
"fixing" a rule that looks odd: most rules exist because of a specific real
filing. Longer write-ups of most rounds are in the claude.ai Project "Order
Scanner" (`claude/*.md`).

---

## 12 Sep 2026 — first build

The user asked for a scanner of NSE and BSE announcements that flags orders
of at least 50% of the latest quarter's revenue. It should store every order,
even those below the bar, and the user was open to better logic.

Choices made:
- scheduled batches, not a polling loop;
- alerts via Telegram plus a local dashboard;
- extraction regex-first with an LLM fallback;
- a tiered composite score (0–100) instead of the flat 50% rule.

**Why not the flat 50% rule:**
- It ignores the execution period: ₹500 Cr over 5 years is not the same as
  over 6 months.
- It ignores firmness: an MoU is a press release, not an order.
- It ignores the company's base rate: a serial announcer's 30th ₹20 Cr order
  is not news.

The 50% rule survives as component B (20 points) and as an always-alert
override. The override never fires when a penalty applied, because otherwise
it promotes exactly the micro-cap pump filings the penalties catch.

**Data sources:**
- BSE `AnnSubCategoryGetData`. It has a dedicated subcategory, "Award of
  Order / Receipt of Order", which is pulled first; then Company Update and
  Others are swept for stragglers.
- NSE `corporate-announcements`, which needs a cookie bootstrap.
- Revenue: NSE results API (in ₹ lakhs), then yfinance, then
  `data/manual_fundamentals.csv`.

## 12 Sep — precision round (first 2-day backfill: 68 orders, 4 of 5 alerts wrong)

The wrong alerts:
- a broker parsed as its own customer;
- a CRISIL rating release, where the "order" was the rated bank facility;
- a financial-services firm;
- a garbled customer name.

The fixes:
- `is_non_order()` drops regulatory, court, tax, exchange and insolvency
  "orders", rating releases and rumour verifications. It matches on the
  **headline/subcategory only**, because real order PDFs contain that
  boilerplate.
- Customer sanity checks. When the only counterparty found is the company
  itself, the order is flagged related-party.
- Believability gate: an order above 25× quarterly revenue is treated as a
  misparse, held at WATCH and cannot alert.
- Companies are merged across exchanges on ISIN, then normalised name.
- Duplicate alerts are suppressed (same value within 0.5%, within 3 days).

## 12 Sep — recall round

- **The plural bug:** `\b(?:order|contract)\b` cannot match "orders". BSE's
  most on-target headline, "Bagging/Receiving of orders/contracts", was
  missed (~6 filings a day). Every filing noun now carries `s?`.
- The execution period is read from the Reg-30 table label, and word
  numbers are parsed ("Eighteen Months"). Coverage went from 9/60 to 26/60.
- A bare value in the Reg-30 cell ("Size of the order 4,23,00,000") is read
  only when a value label sits right in front of it.
- **The PDF probe.** Headlines like "General Updates" and "Press Release"
  carry the order only in the attached PDF. The probe fetches those PDFs and
  re-classifies the text. It is on for `scan` and off for `backfill`, because
  of bandwidth.
- Dashboard: a company drill-down showing the full order history.

## 12 Sep — own-project capex is not an order (Accuracy Shipping)

The filing announced a "Letter of Intent from CBIC for setting up a Container
Freight Station". That is a licence for the company's own capex. The scanner
reported ₹187.50 Cr, the midpoint of the *projected* peak revenue, and it
scored MEDIUM.

- **Fix:** `OWN_PROJECT_PATTERNS`, which needs **2 signals**: licence to
  build, capex, funding mix, peak-utilisation revenue. The tell is the
  direction of the money.
- An EPC order to build something *for a customer* trips only 1 signal and
  stays an order.
- The value anti-cues now reject capex and projected-revenue figures
  independently.
- `cli recheck` was added: it replays the rules over the stored filing text,
  so the database no longer needs wiping after rule changes.

## 12 Sep — dashboard design rounds

- One order filed on both exchanges is stored twice and collapsed into one
  row in the display layer only (`_same_order`).
- Visual pass:
  - only HIGH and MEDIUM get a status hue; WATCH and STORE are told apart by
    the shape of their dot;
  - numbers are printed in normal text colour;
  - glass surfaces;
  - an Auto/Light/Dark theme toggle.
- Trap: `overflow-x:auto` on a container plus a sticky `thead` makes the
  header float. Use `overflow:clip`.
- Writes to `dashboard.py` were reverted twice, probably by a stale editor
  buffer. Always verify the file after writing it.

## Mid-Sep — Telegram, guidance, Reg-30 table, summaries

- Secrets are read from `.env` at the root. Check them with `cli config` and
  `cli test-alert`, which auto-discovers the chat id.
- Messages use HTML parse mode, so every field goes through `alert._esc()`.
- `extract/guidance.py`: management guidance ("FY27 total income Rs 105 Cr")
  is scored separately and combined with the order score. Veerhealth's tiny
  sample order came with guidance that implied income would triple.
- `extract/reg30.py` reads the SEBI disclosure table structurally (ruled
  tables, then word positions). Flattened text had interleaved the columns
  and misread the related-party *question* as an answer on 50 of 65 orders.
- `extract/summary.py` writes a one-sentence description of each order. It
  replaced the old "whatever follows 'for'" scope.
- Scheduling moved to hourly (08:00–22:00) with catch-up for time the PC was
  off. The dashboard starts at sign-in on port 8765.

## 18 Oct 2026 — Orderbook view

The user wanted an order-book tracker like Financially Free's, on a separate
page with a switch from the orders page.

- **The key decision:** a book is not the sum of announced wins, because
  execution is never seen. So the book is read from what companies state in
  results releases, presentations, transcripts and order-win releases.
- The extraction rules in `extract/orderbook.py` each come from a real
  misread across 405 stored filings:
  - targets and forecasts;
  - new-order values next to the phrase;
  - revenue after the anchor;
  - table footnotes;
  - billed amounts;
  - "including orders negotiated";
  - reductions;
  - GW and metres read as money;
  - the LSE's "International Order Book".

  Segment and subsidiary books are kept but marked. A date glued to another
  figure belongs to that figure.
- Series: one point per as-of date. Growth is the latest point against the
  earliest one inside the period.
- `sources/company_meta.py` adds market cap and last-FY revenue from
  screener.in. It is the first working market-cap source, because yfinance
  returned nothing. Not yet verified live.
- `orderbook fetch` opens candidate PDFs for companies that already have an
  order (1,883 at the time). It reads them without caching, is resumable,
  commits per filing and retries failed downloads.

## 18 Oct — BSE outage (30 Sep – 18 Oct) and its fix

- **What happened:** every BSE API call returned 403, and nothing alerted.
  The database has no BSE filings for that window.
- **First fix (not enough):** site-root `Referer` plus `Origin`, which was
  the fix other projects used.
- **Diagnosis:** Claude's built-in browser on the user's PC got a 200 from
  the same API (no cookies needed), while python-requests got 403.
- **Fix:** `sources/bse.py` now tries several strategies:
  1. curl_cffi with `impersonate="chrome"` and the headers Chrome sends on
     that call (`Sec-Fetch-Mode: cors`, `Sec-Fetch-Site: same-site`);
  2. the same, after a cookie warm-up visit to the homepage;
  3. requests with browser headers;
  4. the legacy headers.

  The first strategy BSE accepts is kept for the session.
- `bse-check --probe` prints a status code per strategy. After the fix,
  strategies 1–3 got 200 (so the browser-like headers alone were enough this
  time) and the legacy headers got 403.
- Feed health: a run where every call fails writes "BSE feed down" into
  `runs.errors`. Three failed runs in a row send one Telegram warning, at
  most once a day.
- **Follow-up bug:** a streamed curl_cffi response has an empty `.content`,
  so every PDF came back empty and was counted as a failed download.
  `download_pdf` now reads the stream with `iter_content`, which also
  enforces the size cap. A regression test is in `test_bse_feed.py`.
- **Recovery:** `backfill --days 21`.

## 18 Oct — the outage backfill read no PDFs; PDFs move to AttachHis

- **What happened:** `backfill --days 21` ran 13:35–13:46, before the
  streamed-PDF fix landed at 13:50. Every BSE order filing in the outage
  window was stored without its PDF text (0 of 273), so 114 of 139 outage
  orders had no value and 120 filings never became orders.
- **Then `recheck` failed every download it tried** with 404. Checked in a
  browser: BSE moves attachments from `AttachLive/` to `AttachHis/` two to
  three weeks after filing (11–14 Aug files: 404 on Live, 200 on His; 30 Sep:
  some already moved, some in both; 18 Oct: Live only). `orderbook._pdf_text`
  already knew this; `download_pdf` did not, so the scan, `recheck`,
  `fetch-pdfs` and the dashboard's PDF link all missed moved files.
- **Fixes:** `download_pdf` retries a failed `AttachLive` link on `AttachHis`
  and caches under the stored URL. `recheck` creates an order from a PDF it
  fetched in the same pass (it used to check the stored text, so it needed two
  runs), commits every 50 filings so a scan running alongside is not locked
  out, and prints progress.

## 18 Oct — re-scoring aged every order and dropped firmness

- **What happened:** after the recheck above, HIGH fell from 42 orders to 3
  and MEDIUM from 114 to 61; only 12 of the 83 alerted orders were still
  HIGH/MEDIUM.
- **Cause 1, staleness:** the stale penalty compared the filing time with
  *now*, so any re-score (recheck or rescore) made every order older than 48 h
  stale: 2,870 of 2,894 orders, against 2,263 actually found late.
- **Cause 2, `rescore` only:** firmness and the order-mix keyword adjustment
  have no column, so `rescore` scored every order at firmness 0.5 with no
  bumps — an LOA lost 7.5 points and 22% of its size points (in the smoke test
  a HIGH 88.6 became WATCH 48.1).
- **Fixes:** staleness is measured from filing to when Bagged first fetched it
  (`announcements.fetched_at`, passed as `seen_at`), so it no longer changes on
  re-score. `rescore` reads firmness and the keyword adjustment back from the
  stored score breakdown. Tests: two in test_score, one in
  test_pipeline_smoke (rescore three days later changes nothing).
- **Result** (simulated on a copy of the DB): HIGH 45, MEDIUM 127, WATCH 320;
  81 of the 83 alerted orders are HIGH/MEDIUM again.

## 18 Oct — the Orderbook view, made readable

- **The page was never seen:** the scheduled dashboard process had been
  started before `/orderbook` was added and answered 404. A restart of the
  "Order Scanner Dashboard" task is needed after changing the page or API.
- **Top gainers were misreads:** Jindal Saw +2,738% (a transcript aside on
  seamless pipes, then the group book), BEL +261% "over 1 day" (a projects
  figure next to the book in one call), Aastha +228% over 21 days, 63 Moons
  +789% (a billed-against figure). Of 86 companies with a growth figure, the
  top of the list was dominated by these.
- **Fixes in `orderbook.build_series` / `growth`:**
  - within one filing, dates less than a month apart are one reading and only
    its largest figure is the whole-company book (a part is never larger than
    the whole);
  - one point per quarter, preferring results releases, order-book updates
    and presentations over call transcripts;
  - growth needs two points at least 60 days apart;
  - a step of 4× or more is flagged ("!" in the table) and kept out of the
    top gainers, not hidden.
- **Page:** the growth header names the period; filters are remembered per
  browser; the pager says how many rows a market-cap filter hides for lack
  of a market cap.
- **Market cap coverage:** 91 of 217 companies came back empty from
  screener.in and were stamped as refreshed, so they would not be retried for
  a week. Empty fetches are no longer stamped, rows stamped without a market
  cap are retried, and bulk refreshes wait out a 429.
- Tests: six series cases in test_orderbook (53).
