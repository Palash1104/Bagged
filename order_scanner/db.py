"""SQLite persistence.

Design note: *every* order-shaped announcement is stored, scored or not.
The historical table is what powers the novelty component of the score and
lets you backtest thresholds later.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH

IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Master list of companies, keyed by a canonical internal id.
CREATE TABLE IF NOT EXISTS companies (
    company_id      TEXT PRIMARY KEY,      -- ISIN when known, else NSE:SYM / BSE:CODE
    isin            TEXT,
    nse_symbol      TEXT,
    bse_code        TEXT,
    name            TEXT NOT NULL,
    name_key        TEXT,                  -- normalised name, for cross-exchange matching
    industry        TEXT,
    sector          TEXT,     -- sector label shown on the dashboard; industry is finer
    is_sme          INTEGER DEFAULT 0,
    market_cap_inr  REAL,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_companies_nse ON companies(nse_symbol);
CREATE INDEX IF NOT EXISTS idx_companies_bse ON companies(bse_code);
CREATE INDEX IF NOT EXISTS idx_companies_isin ON companies(isin);
CREATE INDEX IF NOT EXISTS idx_companies_namekey ON companies(name_key);

-- Cached fundamentals.  Refreshed lazily; `as_of` drives staleness.
CREATE TABLE IF NOT EXISTS fundamentals (
    company_id          TEXT PRIMARY KEY REFERENCES companies(company_id),
    latest_quarter      TEXT,        -- e.g. '2026-06-30'
    q_revenue_inr       REAL,        -- latest quarter net sales / revenue from ops
    ttm_revenue_inr     REAL,        -- sum of last 4 quarters
    prev_ttm_revenue_inr REAL,
    ebitda_margin       REAL,        -- fraction, e.g. 0.14
    net_margin          REAL,
    q_revenue_yoy       REAL,
    source              TEXT,        -- nse | bse | yfinance | manual
    confidence          REAL,        -- 0-1
    as_of               TEXT,
    raw                 TEXT         -- json blob of what the source returned
);

-- Raw announcements, deduped.  One row per exchange filing.
CREATE TABLE IF NOT EXISTS announcements (
    ann_id          TEXT PRIMARY KEY,     -- sha1(exchange|native_id)
    exchange        TEXT NOT NULL,        -- NSE | BSE
    native_id       TEXT,
    company_id      TEXT REFERENCES companies(company_id),
    symbol          TEXT,
    company_name    TEXT,
    category        TEXT,
    subcategory     TEXT,
    headline        TEXT,
    body            TEXT,
    pdf_url         TEXT,
    pdf_text        TEXT,
    filed_at        TEXT,                 -- ISO8601 IST
    disseminated_at TEXT,
    fetched_at      TEXT,
    is_order_like   INTEGER DEFAULT 0,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_ann_filed ON announcements(filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ann_company ON announcements(company_id);
CREATE INDEX IF NOT EXISTS idx_ann_orderlike ON announcements(is_order_like);

-- Extracted, structured orders.  This is the historical order book.
CREATE TABLE IF NOT EXISTS orders (
    order_id            TEXT PRIMARY KEY,   -- same as ann_id (1 order row per filing)
    ann_id              TEXT REFERENCES announcements(ann_id),
    company_id          TEXT REFERENCES companies(company_id),
    company_name        TEXT,
    exchange            TEXT,
    filed_at            TEXT,

    order_value_inr     REAL,
    value_currency      TEXT,
    value_is_range      INTEGER DEFAULT 0,
    value_low_inr       REAL,
    value_high_inr      REAL,

    customer            TEXT,
    customer_type       TEXT,     -- government | psu | private | export | unknown
    execution_months    REAL,
    order_type          TEXT,     -- loa | loi | work_order | mou | l1 | amendment | unknown
    is_amendment        INTEGER DEFAULT 0,
    is_related_party    INTEGER DEFAULT 0,
    scope               TEXT,
    summary             TEXT,     -- one readable sentence describing the order

    extraction_method   TEXT,     -- regex | llm | manual
    extraction_conf     REAL,
    extraction_notes    TEXT,

    -- snapshot of the fundamentals used, so scores stay reproducible
    q_revenue_inr       REAL,
    ttm_revenue_inr     REAL,
    market_cap_inr      REAL,

    ratio_to_quarter    REAL,
    ratio_to_ttm        REAL,
    annualised_ratio    REAL,

    score               REAL,
    score_breakdown     TEXT,     -- json
    tier                TEXT,     -- HIGH | MEDIUM | WATCH | STORE
    data_quality        TEXT,     -- good | partial | low

    created_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_company ON orders(company_id, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_score ON orders(score DESC);
CREATE INDEX IF NOT EXISTS idx_orders_filed ON orders(filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_tier ON orders(tier);

-- Alerts actually sent (so re-runs never double-fire).
CREATE TABLE IF NOT EXISTS alerts (
    alert_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT REFERENCES orders(order_id),
    channel     TEXT,
    tier        TEXT,
    sent_at     TEXT,
    ok          INTEGER,
    error       TEXT,
    UNIQUE(order_id, channel)
);

-- LLM extraction cache so you never pay twice for the same filing.
CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,   -- sha1 of prompt input
    response    TEXT,
    model       TEXT,
    created_at  TEXT
);

-- Bookkeeping for scheduled runs.
CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT,
    finished_at     TEXT,
    window_from     TEXT,
    window_to       TEXT,
    n_announcements INTEGER,
    n_order_like    INTEGER,
    n_extracted     INTEGER,
    n_alerts        INTEGER,
    llm_calls       INTEGER,
    errors          TEXT
);
"""


def now_ist() -> datetime:
    return datetime.now(IST)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


@contextmanager
def connect(path: Path | str = DB_PATH):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add a column to a table that already exists, so they are applied by hand.
MIGRATIONS = [
    ("companies", "name_key", "TEXT"),
    ("orders", "summary", "TEXT"),
    ("companies", "sector", "TEXT"),
    ("orders", "guidance", "TEXT"),                      # json list, see guidance.py
    ("fundamentals", "last_fy", "INTEGER"),              # year the last full FY ended
    ("fundamentals", "last_fy_revenue_inr", "REAL"),
    ("fundamentals", "last_fy_other_income_inr", "REAL"),
    ("fundamentals", "last_fy_pat_inr", "REAL"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, coltype in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db(path: Path | str = DB_PATH) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], pk: str) -> None:
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != pk)
    sql = (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({pk}) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[c] for c in cols])


def known_ann_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> set[str]:
    ids = list(ids)
    if not ids:
        return set()
    out: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = f"SELECT ann_id FROM announcements WHERE ann_id IN ({','.join('?' * len(chunk))})"
        out.update(r["ann_id"] for r in conn.execute(q, chunk))
    return out


def company_order_history(
    conn: sqlite3.Connection, company_id: str, months: int = 24
) -> list[float]:
    """Order values this company has announced in the trailing window.

    Used by the novelty component: an order that is unremarkable for a
    serial announcer should not score like a transformational one.
    """
    cutoff = (now_ist() - timedelta(days=months * 30)).isoformat()
    rows = conn.execute(
        "SELECT order_value_inr FROM orders "
        "WHERE company_id=? AND filed_at >= ? AND order_value_inr > 0 "
        "AND is_amendment = 0",
        (company_id, cutoff),
    ).fetchall()
    return [r["order_value_inr"] for r in rows]


def already_alerted(conn: sqlite3.Connection, order_id: str, channel: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM alerts WHERE order_id=? AND channel=? AND ok=1",
        (order_id, channel),
    ).fetchone()
    return r is not None


def record_alert(conn, order_id, channel, tier, ok, error=None) -> None:
    conn.execute(
        "INSERT INTO alerts (order_id, channel, tier, sent_at, ok, error) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(order_id, channel) DO UPDATE SET "
        "sent_at=excluded.sent_at, ok=excluded.ok, error=excluded.error",
        (order_id, channel, tier, now_ist().isoformat(), 1 if ok else 0, error),
    )


def get_fundamentals(conn, company_id: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM fundamentals WHERE company_id=?", (company_id,)
    ).fetchone()
    return dict(r) if r else None


def cache_llm(conn, key: str, response: dict, model: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache (key, response, model, created_at) VALUES (?,?,?,?)",
        (key, json.dumps(response), model, now_ist().isoformat()),
    )


def get_llm_cache(conn, key: str) -> dict | None:
    r = conn.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
    return json.loads(r["response"]) if r else None
