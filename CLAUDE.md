# Bagged — NSE/BSE order-win scanner

Context for any new Claude session (Claude Code in VS Code or Cowork). Read
this first. `README.md` is the user-facing manual; `docs/HISTORY.md` records
every decision and incident so far, and why each was made. Read that before
changing extraction or scoring rules.

## What we are building

Bagged scans every NSE and BSE corporate announcement for **order wins**
("received a Letter of Award from NHAI worth Rs 480 crore"). For each one it:

1. extracts the order value, customer, execution period and firmness;
2. weighs the order against the company's own revenue;
3. scores it 0–100;
4. sends the meaningful ones to **Telegram**.

**Every** order-shaped filing is stored, including the ones that don't alert,
because a company's own order history is part of the score.

A second page, the **Orderbook view**, tracks each company's *reported* order
book over time: growth, book/revenue, market cap and a history sparkline. It
is modelled on the order-book tracker from the Financially Free YouTube
channel, which inspired the project.

The owner is Palash, a CS student who trades Indian equities. This is a
personal screening tool that runs on his own PC. It is not a product and not
investment advice.

## Environment

- Windows PC, Anaconda Python 3.13, PowerShell.
- Project root: `C:\Users\tralp\Downloads\order_scanner\order_scanner` (git repo).
- Everything runs as `python -m order_scanner.cli <command>` from the root.
- Windows Task Scheduler runs:
  - `run_scan.bat` ("Order Scanner"): every hour, 08:00–22:00 IST. Each run
    also catches up on time the PC was off, up to 96 h; use `backfill`
    beyond that.
  - `run_dashboard.bat` ("Order Scanner Dashboard"): at sign-in, serving
    http://127.0.0.1:8765.
- Logs go to `data\scan.log`, `data\backfill.log` and `data\dashboard.log`.
- Secrets live in `.env` at the root (Telegram token and chat id, LLM key, FX
  rates); see `.env.example`. **Never read, print or commit `.env`.**
- `data/` holds the database `orders.db` (SQLite, WAL) and the PDF cache.
  It is several GB and git-ignored.
- The user works in VS Code with Claude Code, and sometimes in Cowork, which
  is linked to this PC.

## Layout

```
order_scanner/
  cli.py            every command (argparse). `python -m order_scanner.cli -h`
  config.py         ALL tunables: score weights, thresholds, penalties, keyword lists, limits
  pipeline.py       run_scan(): fetch -> dedupe -> extract -> enrich -> score -> alert -> store
  score.py          the composite 0-100 score (components A-F, guidance, penalties)
  alert.py          Telegram messages + daily digest (HTML parse mode: escape via _esc)
  db.py             schema + migrations (_migrate adds columns to old DBs)
  dashboard.py      FastAPI app: "/" orders page, "/orderbook", /api/*; serve() is at the very bottom
  orderbook.py      order-book service: record_filing, recheck, fetch, pull_history, build_series, growth
  orderbook_page.py HTML/CSS/JS of the /orderbook page (reuses the orders page stylesheet)
  extract/
    rules.py        is this an order? (looks_like_order, is_non_order, own-project capex) + field extraction
    money.py        rupee amounts, crore/lakh, ranges, anti-cues (EMD, bank guarantee, capex...)
    reg30.py        SEBI Reg-30 disclosure table read structurally from the PDF
    summary.py      one-sentence order description for the dashboard
    guidance.py     management guidance ("FY27 income of Rs 105 Cr") scored separately
    orderbook.py    order-book figure extraction (sentence-scoped regex, dates, scope)
    pdf.py          download_pdf (streams; cache=False never writes) + text/OCR
    llm.py          LLM fallback (anthropic or openai per .env), capped at 40 calls/run, cached
  sources/
    bse.py          BSE API client (curl_cffi Chrome transport + fallbacks), check(), probe()
    nse.py          NSE API client (cookie bootstrap, re-bootstraps on 401/403)
    fundamentals.py quarterly revenue: NSE results API -> yfinance -> data/manual_fundamentals.csv
    company_meta.py market cap + last-FY revenue from screener.in
    annual.py       screener.in annual P&L (the base for guidance)
    sector.py       industry labels: screener.in -> BSE -> yfinance
tests/              plain scripts: `python tests\test_x.py` prints PASS/FAIL and the totals
```

## Core design decisions

These are settled; don't undo them without asking.

- **Scheduled batches, not a polling loop.** This survives crashes, sleep and
  NSE cookie expiry.
- **Regex first, LLM only as a fallback** when the rules come up short.
- **Composite score instead of "order ≥ 50% of quarterly revenue".** The
  user's original rule survives as component B and as an always-alert
  override.

  | | Component | Points | Measures |
  |---|---|---|---|
  | A | Annualised impact | 40 | order value per year of execution ÷ TTM revenue |
  | B | Quarterly ratio | 20 | order value ÷ latest quarterly revenue |
  | C | Firmness | 15 | LOA/work order > LOI > L1 > MoU |
  | D | Margin quality | 10 | |
  | E | Novelty | 10 | vs the company's own order history |
  | F | Capacity | 5 | |

  - A materiality gate keeps a tiny order from scoring on C–F.
  - Multiplicative penalties: pump-shaped filings (order much larger than
    market cap, SME), related-party customer, stale filing.
  - Tiers: ≥75 HIGH, ≥60 MEDIUM (both alert), ≥40 WATCH (digest only),
    below that stored silently.
  - A non-firm order never reaches HIGH.
  - Anything above 25× quarterly revenue is treated as a misparse and held
    at WATCH.
- **Precision over recall.** "Order" is overloaded in Indian filings
  (regulatory, court, tax, exchange orders). Every rule exists because of a
  real misread. Each new rule gets a test case built from the real sentence.
- **An order book is not the sum of order wins.** Book = opening + new orders −
  executed, and Bagged never sees execution. The Orderbook view reads only the
  figure companies *state* in filings. Each figure keeps its as-of date,
  scope (total/segment/subsidiary), confidence and source sentence.
  The series (`orderbook.build_series`) has one point per quarter: within a
  filing only the largest figure for a date counts (parts are never larger
  than the whole), and results/presentations/updates beat transcripts.
  Growth needs two points ≥ 60 days apart; a 4× step is flagged and kept out
  of the top gainers.
- **Same order on both exchanges:** stored as two filings, collapsed into one
  row in the dashboard only (`_same_order`).
- **Companies are merged across exchanges** on ISIN first, then normalised
  name.

## Rules for working on this repo

- **Finish every task with the exact commands to run**, in order, in a code
  block. This is the user's standing preference.
- **Explain briefly.** The user often asks for answers "in short".
- After any change, run the test suites (all must pass):
  ```
  python tests\test_extract.py          :: 71
  python tests\test_score.py            :: 25
  python tests\test_pipeline_smoke.py   :: 24
  python tests\test_orderbook.py        :: 53
  python tests\test_orderbook_smoke.py  :: 16
  python tests\test_bse_feed.py         :: 27  (prints simulated 403s on purpose)
  ```
- **Replay stored data after a rule change instead of wiping the DB.** All
  filing text is stored, so both of these run offline:
  - after `rules.py`, `money.py` or keyword lists in `config.py`:
    `cli recheck` (re-extracts and re-scores; reports what changed);
  - after weights or thresholds in `config.py`, or `score.py`: `cli rescore`;
  - after `extract/orderbook.py`: `cli orderbook recheck`.
- **Every filing noun in a regex needs `s?`.** `\border\b` does not match
  "orders"; that bug once hid ~6 order filings a day.
- Match non-order markers on **headline/subcategory only**. Real order PDFs
  mention credit ratings and tax matters in boilerplate.
- **Long jobs run alongside the hourly scan.** Commit per item and keep
  write transactions short, because SQLite allows one writer.
  `orderbook fetch` and `backfill` are resumable: Ctrl+C and rerun. `recheck`
  commits every 50 filings, so Ctrl+C keeps its work; a rerun starts over but
  finds the PDFs already cached.
- **`dashboard.py` has had writes reverted**, probably by a stale editor
  buffer. After writing it, verify the file on disk. A full-file rewrite must
  keep `serve()` at the bottom, which `cli dashboard` imports.
- **Exchange APIs are unofficial and change without notice.**
  - BSE has refused python-requests since 30 Sep 2026. `sources/bse.py` uses
    curl_cffi (Chrome impersonation) and falls back through other strategies.
  - `bse-check` / `bse-check --probe` diagnose the feed.
  - A streamed curl_cffi response has an empty `.content`, so `download_pdf`
    must read the stream with `iter_content`.
  - BSE moves attachments from `AttachLive/` to `AttachHis/` 2–3 weeks after
    filing; the old link then 404s. `download_pdf` retries on `AttachHis/` and
    caches under the URL as stored.
- Dates and times are IST (`db.IST`, `now_ist()`).
- Python 3.11-compatible syntax: no backslashes or nested same-type quotes
  inside f-string expressions.

## Commands (cheat sheet)

```
scan [--force --lookback N --deep --dry-run --probe-pdfs]   one batch (what the scheduler runs)
backfill --days N [--probe-pdfs]                             store + score history, never alerts
dashboard [--port 8765]                                      local UI (default port 8000; the scheduled one uses 8765)
recheck / rescore                                            replay rules / scores over stored data
orderbook recheck | fetch [--days --limit --dry-run --all-companies --no-meta] | history --days 400 | meta [--force] | show SYM [--refresh-meta]
bse-check [--probe]                                          is BSE answering, and through which client
config / test-alert                                          check .env loaded / send a sample Telegram alert
test-extract "text..." [--headline H]                        run the extractor on a sentence
sectors [--all] / fetch-pdfs / resummarize / fundamentals SYM
```

## Status (18 Oct 2026)

**Working:** the hourly scans, Telegram alerts, the orders dashboard with
company drill-down, and the Orderbook view (`/orderbook`, linked from the
top bar).

**Recently fixed (18 Oct):** see docs/HISTORY.md for each.
- The BSE 403 outage (30 Sep – 18 Oct); `backfill --days 21` and `recheck`
  recovered its filings and their PDFs.
- PDFs moved to `AttachHis/`; re-scoring that aged every order and dropped
  firmness; order-book series and growth misreads.
- The scheduled dashboard was started before `/orderbook` existed and
  answered 404 there: restart the "Order Scanner Dashboard" task after any
  change to `dashboard.py` or `orderbook_page.py`.

**Next steps for the user:**
1. `rescore` (if not run since the 18 Oct fixes), then restart the dashboard task.
2. `orderbook history --days 400`, then `orderbook fetch`, for 1-year growth.
3. `orderbook meta`, to retry the market caps screener.in did not return.

**Partly verified live:** screener.in market cap and revenue parsing works
(126 of 217 companies on 18 Oct); the rest came back empty, likely throttled.

**Open ideas and known gaps:**
- Alerts on order-book jumps.
- Slide tables listing several quarters side by side are skipped.
- A concession or O&M contract is not yet told apart from a licence to build.
- Fundamentals coverage is weak for BSE-only small caps; use
  `data/manual_fundamentals.csv`.
- FX rates are static in `.env`.
- Backtest the score thresholds against forward returns once there are
  ~3 months of history. Filings go back to 16 Jun 2026.

`cli.new.py` and `dashboard.new.py` in the root are stale leftovers (git-ignored).
Ignore them.
