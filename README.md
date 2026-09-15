# NSE / BSE Order Scanner

Scans NSE and BSE corporate announcements for **order wins**, extracts the order
value, weighs it against the company's revenue, scores it, and pushes the
high-conviction ones to Telegram. **Every** order-shaped filing is stored —
including the ones that don't clear the alert bar — because that history is what
makes the scoring smarter over time.

---

## Quick start (Windows)

```bat
cd path\to\order_scanner
pip install -r requirements.txt
copy .env.example .env
:: edit .env: Telegram token + chat id, optionally an LLM key

python -m order_scanner.cli scan --force --lookback 240   :: first run
python -m order_scanner.cli dashboard                     :: http://127.0.0.1:8000
```

Seed the history before you trust the novelty scores:

```bat
python -m order_scanner.cli backfill --days 60
```

Backfill stores and scores but never alerts.

---

## How the scoring works

Your original rule was *"order ≥ 50% of the latest quarter's revenue."* That
rule is still in here — it's component B, worth 20 of the 100 points, and it also
survives as a hard override that always surfaces an order clearing 50% of a
quarter. But on its own it has three problems:

1. **It ignores execution period.** A ₹500 Cr order over 5 years adds ₹100 Cr/yr.
   The same order over 6 months adds ₹1,000 Cr/yr annualised. Same headline,
   ten times the impact.
2. **It ignores firmness.** An MoU is a press release. An LOA is revenue.
3. **It ignores the company's own base rate.** A firm that announces a ₹20 Cr
   order every fortnight shouldn't trigger the same excitement on its 30th.

So six components make up the score:

| # | Component | Max | What it measures |
|---|-----------|-----|------------------|
| A | Annualised impact | 40 | `(order value ÷ execution years) ÷ TTM revenue`, log-scaled. 1% floor, 60% full marks. |
| B | Quarterly ratio | 20 | `order value ÷ latest quarterly revenue` — **your rule**, on a curve rather than a cliff. |
| C | Firmness | 15 | LOA / work order (1.0) > LOI (0.6) > L1 (0.4) > MoU (0.3) > cancellation (0). |
| D | Margin quality | 10 | Company EBITDA margin, adjusted by order-mix keywords (defence, export, annuity up; lowest-bidder civil work down). |
| E | Novelty | 10 | Order value ÷ that company's median announced order over 24 months. Paid for by the historical table. |
| F | Capacity | 5 | Deliberately non-monotonic — an order 10× TTM revenue is a risk, not a bonus. |

**Size comes first.** C–F describe the *quality* of an order, not whether it
matters, so they are multiplied by a **materiality gate** between 0 and 1. The
gate reads the order against revenue through whichever lens makes it look
bigger, log-interpolated between the two points:

| Lens | Gate 0 at | Gate 1 at |
|------|-----------|-----------|
| Annualised value ÷ TTM revenue | 2% | 10% |
| Order value ÷ latest quarterly revenue | 10% | 50% (your rule) |

An order below both floors earns nothing for being firm, novel or high-margin:
a ₹3 Cr defence LOA at a ₹400 Cr-revenue company scores 0 instead of 39. When
significance can't be measured (no order value, or no revenue data) the gate is
0.25, which keeps those orders ranked on the dashboard but well below WATCH.

Firmness also discounts A and B (`0.55 + 0.45 × firmness`), so an MoU's size
counts for ~69% of face value. Then multiplicative penalties apply:

- order > 5× market cap → ×0.40 (the classic micro-cap pump pattern)
- SME + order > 2× market cap → ×0.60
- related-party customer → ×0.50
- filed more than 48h ago → ×0.70

**Management guidance counts too.** A filing that says *"in FY27 we are expecting
Total Income of ₹105 crores with PAT of minimum ₹5–7 crores"* is scored on the
growth it implies over the last completed year (from screener.in's annual P&L):
income per year from +15% (nothing) to 2× (full marks), PAT from +25% to 3×,
weighted 60/40, discounted for guidance further out. The final score is the
stronger of the order score and the guidance score plus a quarter of the weaker,
so strong guidance can reach HIGH and alert on its own — even on a small or MoU
order. Guidance above 8× last year's revenue is treated as a misread and held at
WATCH. Client- or project-specific projections ("additional turnover from above
client", "at peak utilisation") are not company guidance and are ignored.

**Tiers:** ≥75 HIGH (red, Telegram) · ≥60 MEDIUM (amber, Telegram) ·
≥40 WATCH (daily digest + dashboard) · below that, stored silently.

Two safety rails: a non-firm order never reaches HIGH however large, and the
50%-of-quarter override does **not** fire when a penalty was applied — otherwise
it would promote exactly the pump filings the penalties exist to catch.

Everything above is tunable in `config.py` (`ScoreWeights`, `Thresholds`,
`Materiality`, `Penalties`). After changing weights, run `python -m order_scanner.cli rescore`
to re-score the whole stored history and see what the change would have done.

---

## Data sources

| Need | Primary | Fallback |
|------|---------|----------|
| BSE announcements | `api.bseindia.com/.../AnnSubCategoryGetData/w` — has a dedicated **"Award of Order / Receipt of Order"** subcategory, so orders are pulled directly | wider sweep of Company Update + Others |
| NSE announcements | `nseindia.com/api/corporate-announcements` (equities + SME) | — |
| Quarterly revenue | `nseindia.com/api/results-comparision` (official, reported in ₹ lakhs) | yfinance `.NS`/`.BO` quarterly income statement |
| Manual override | `data/manual_fundamentals.csv` — always wins | — |

NSE needs a cookie handshake and re-bootstraps automatically on 401/403. The
manual CSV takes columns `key,q_revenue_cr,ttm_revenue_cr,ebitda_margin,market_cap_cr,latest_quarter`
where `key` is an NSE symbol, BSE code or ISIN — use it for anything the APIs
get wrong or don't cover.

---

## Extraction

Regex first, LLM only where the rules come up short (`ExtractedOrder.needs_llm`),
capped at 40 calls per run and cached in SQLite so a re-run of the same window
is free.

The regex layer handles `Rs. 425.60 Crore`, `INR 1,250 Lakhs`, `₹1,25,50,00,000`,
`USD 12.5 Million` (FX-converted), and `Rs 100-120 crore` ranges. The hard part
isn't finding numbers — it's picking the *right* one. Filings are full of money
that isn't the order: EMD, bank guarantees, paid-up capital, GST. Each candidate
is scored by distance-weighted proximity to value cues ("order value is",
"contract worth") minus proximity to anti-cues ("earnest money deposit of",
"paid-up capital"), with leading qualifiers weighted higher because that's how
filings read.

Cancellations, revisions and short-closures are detected and stored as
amendments — never alerted as new orders.

Test it on any text without touching the network:

```bat
python -m order_scanner.cli test-extract "The Company has received an LoA from NTPC worth Rs 425.60 crore, to be executed within 18 months."
```

---

## Scheduling (Windows Task Scheduler)

`run_scan.bat` runs one scan and appends to `data\scan.log`:

```bat
@echo off
cd /d C:\path\to\order_scanner
call C:\Users\<you>\anaconda3\Scripts\activate.bat
set PYTHONIOENCODING=utf-8
python -m order_scanner.cli scan >> data\scan.log 2>&1
```

The task "Order Scanner" runs it every hour from 08:00 (last run 21:00), hidden:

```
Task Scheduler -> Create Task
  General : "Order Scanner", "Run only when user is logged on"
  Triggers: Daily 08:00, repeat every 1 hour for a duration of 14 hours
  Actions : conhost.exe --headless cmd.exe /c C:\path\to\order_scanner\run_scan.bat
  Settings: "Run task as soon as possible after a scheduled start is missed",
            stop the task if it runs longer than 50 minutes,
            do not start a new instance if one is running
```

`conhost --headless` stops a console window flashing up every hour. "Run whether
user is logged on or not" also works, but needs admin rights or a stored password.

On Linux/macOS instead: `0 8-21 * * 1-5 cd /path && python -m order_scanner.cli scan`

**The PC has to be on and awake for a scan to run, but hours it isn't are not
lost.** A scheduled scan covers `lookback_minutes + overlap_minutes` (60 + 10) or
everything since the end of the last recorded run, whichever reaches further
back, up to `max_catchup_hours` (96, enough for a weekend). Switch the PC on after
a day away and the next run picks up that day's filings; their alerts arrive
late, and filings over 48 hours old carry the stale discount. For gaps longer
than four days, run `backfill --days N`. Deduplication is by
`sha1(exchange|native_id)`, so overlap is free. Runs outside 08:00–22:00 IST and
on weekends are no-ops unless you pass `--force`; `--lookback N` scans exactly N
minutes and skips the catch-up.

**Dashboard at sign-in.** A second task, "Order Scanner Dashboard", runs
`run_dashboard.bat` (hidden, no time limit) whenever you sign in, so
http://127.0.0.1:8765 is always up. It listens on this PC only. To restart it:
`Stop-ScheduledTask` then `Start-ScheduledTask -TaskName 'Order Scanner Dashboard'`.

---

## Commands

```
scan            one batch    --lookback N --force --deep --dry-run
backfill        seed history --days N (stores + scores, never alerts)
dashboard       local UI     --host --port
test-extract    run the extractor over text (offline)
rescore         re-score all stored orders after tuning weights
resummarize     rebuild the one-line order descriptions from stored filing text
sectors         look up company industry labels (screener.in, BSE, yfinance); --all to refresh
fetch-pdfs      cache every stored order's filing PDF for the dashboard
fundamentals    resolve revenue for one symbol
config          print effective config
```

## Tests

```bat
python tests\test_extract.py         :: 20 extraction cases
python tests\test_score.py           :: 23 scoring invariants
python tests\test_pipeline_smoke.py  :: 22 end-to-end checks, network stubbed
```

---

## Known limits

- **NSE/BSE APIs are unofficial.** Field names drift; the readers use aliases
  and `.get()` throughout, but a layout change can still need a patch. NSE also
  rate-limits and sometimes blocks datacenter IPs — a residential IP or your own
  PC works best, which is why this is built to run locally.
- **Execution period is often unstated.** The model assumes 18 months and flags
  `data_quality: partial` when it does. Component A is sensitive to this, so
  check the flag before acting on a borderline score.
- **Order-book context is missing.** A ₹200 Cr order against an existing
  ₹5,000 Cr book is incremental, but neither exchange publishes order books in a
  machine-readable form. The `orders` table is the substitute — after a few
  months it approximates each company's intake run-rate.
- **FX rates are static** in `.env`. Fine for order sizing, not for anything
  needing precision.
- Nothing here is investment advice; it's a screening tool that surfaces filings
  for you to read yourself.
