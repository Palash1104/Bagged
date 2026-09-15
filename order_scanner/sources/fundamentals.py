"""Resolving latest-quarter and TTM revenue for a company.

Chain of sources, best first:
  1. NSE /api/results-comparision   (official, free, reported in Rs LAKHS)
  2. yfinance quarterly income statement  (.NS then .BO)
  3. data/manual_fundamentals.csv  (your own overrides; always wins if present)

Results are cached in SQLite and refreshed when older than `max_age_days`.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timedelta

from ..config import DATA_DIR, LAKH
from ..db import IST, get_fundamentals, now_ist, upsert

log = logging.getLogger(__name__)

MANUAL_CSV = DATA_DIR / "manual_fundamentals.csv"
MAX_AGE_DAYS = 20


def _f(row: dict, *keys) -> float | None:
    for k in keys:
        v = row.get(k)
        if v in (None, "", "-", "NA"):
            continue
        try:
            return float(str(v).replace(",", ""))
        except ValueError:
            continue
    return None


def _s(row: dict, *keys) -> str | None:
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "-"):
            return str(v)
    return None


# ---------------------------------------------------------------- NSE
def from_nse(nse_client, symbol: str) -> dict | None:
    """NSE results-comparision. Amounts come back in Rupees Lakhs."""
    data = nse_client.results_comparison(symbol)
    if not data:
        return None
    rows = data.get("resCmpData") or data.get("data") or []
    if not isinstance(rows, list) or not rows:
        return None

    quarters = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        rev = _f(r, "income", "re_income", "totalIncome", "revenue",
                 "incomeFromOperations", "net_sales")
        if rev is None:
            continue
        pbt = _f(r, "proLossBefTax", "reProLossBefTax", "pbt")
        pat = _f(r, "proLossAftTax", "reProLossAftTax", "net_prof", "pat")
        exp = _f(r, "exp", "expenditure", "re_exp")
        end = _s(r, "to_date", "re_to_date", "toDate", "quarter_ended")
        quarters.append({
            "end": end,
            "revenue_inr": rev * LAKH,
            "pbt_inr": pbt * LAKH if pbt is not None else None,
            "pat_inr": pat * LAKH if pat is not None else None,
            "expenditure_inr": exp * LAKH if exp is not None else None,
        })

    if not quarters:
        return None

    quarters = quarters[:8]  # already newest-first in NSE's response
    latest = quarters[0]
    ttm = sum(q["revenue_inr"] for q in quarters[:4]) if len(quarters) >= 4 else None
    prev_ttm = (sum(q["revenue_inr"] for q in quarters[4:8])
                if len(quarters) >= 8 else None)
    yoy = None
    if len(quarters) >= 5 and quarters[4]["revenue_inr"]:
        yoy = latest["revenue_inr"] / quarters[4]["revenue_inr"] - 1

    ebitda_margin = None
    if latest["expenditure_inr"] and latest["revenue_inr"]:
        ebitda_margin = 1 - latest["expenditure_inr"] / latest["revenue_inr"]
    net_margin = (latest["pat_inr"] / latest["revenue_inr"]
                  if latest["pat_inr"] and latest["revenue_inr"] else None)

    return {
        "latest_quarter": latest["end"],
        "q_revenue_inr": latest["revenue_inr"],
        "ttm_revenue_inr": ttm,
        "prev_ttm_revenue_inr": prev_ttm,
        "ebitda_margin": ebitda_margin,
        "net_margin": net_margin,
        "q_revenue_yoy": yoy,
        "source": "nse",
        "confidence": 0.9 if ttm else 0.65,
        "raw": json.dumps(quarters[:8]),
    }


# ---------------------------------------------------------------- yfinance
def from_yfinance(nse_symbol: str | None, bse_code: str | None) -> dict | None:
    try:
        import yfinance as yf
    except ImportError:
        return None

    tickers = []
    if nse_symbol:
        tickers.append(f"{nse_symbol}.NS")
    if bse_code:
        tickers.append(f"{bse_code}.BO")

    for t in tickers:
        try:
            tk = yf.Ticker(t)
            df = tk.quarterly_income_stmt
            if df is None or df.empty:
                continue
            idx = {str(i).lower(): i for i in df.index}
            rev_key = next((idx[k] for k in
                            ("total revenue", "operating revenue", "revenue")
                            if k in idx), None)
            if rev_key is None:
                continue
            series = df.loc[rev_key].dropna()
            if series.empty:
                continue
            cols = list(series.index)[:8]
            revs = [float(series[c]) for c in cols]

            ebitda = None
            ek = next((idx[k] for k in ("ebitda", "normalized ebitda") if k in idx), None)
            if ek is not None:
                try:
                    ebitda = float(df.loc[ek].dropna().iloc[0]) / revs[0]
                except Exception:
                    ebitda = None

            mcap = None
            try:
                mcap = float(tk.fast_info.get("market_cap") or 0) or None
            except Exception:
                pass

            return {
                "latest_quarter": str(cols[0])[:10],
                "q_revenue_inr": revs[0],
                "ttm_revenue_inr": sum(revs[:4]) if len(revs) >= 4 else None,
                "prev_ttm_revenue_inr": sum(revs[4:8]) if len(revs) >= 8 else None,
                "ebitda_margin": ebitda,
                "net_margin": None,
                "q_revenue_yoy": (revs[0] / revs[4] - 1) if len(revs) >= 5 and revs[4] else None,
                "source": f"yfinance:{t}",
                "confidence": 0.7,
                "market_cap_inr": mcap,
                "raw": json.dumps({"ticker": t, "revenues": revs}),
            }
        except Exception as exc:
            log.debug("yfinance %s failed: %s", t, exc)
    return None


# ---------------------------------------------------------------- manual
_manual_cache: dict[str, dict] | None = None


def from_manual(nse_symbol: str | None, bse_code: str | None,
                isin: str | None) -> dict | None:
    """CSV override. Columns: key,q_revenue_cr,ttm_revenue_cr,ebitda_margin,
    market_cap_cr,latest_quarter  — `key` may be an NSE symbol, BSE code or ISIN.
    """
    global _manual_cache
    if _manual_cache is None:
        _manual_cache = {}
        if MANUAL_CSV.exists():
            with MANUAL_CSV.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    key = (row.get("key") or "").strip().upper()
                    if key:
                        _manual_cache[key] = row
    for key in filter(None, (nse_symbol, bse_code, isin)):
        row = _manual_cache.get(str(key).strip().upper())
        if not row:
            continue
        cr = 1e7
        return {
            "latest_quarter": row.get("latest_quarter"),
            "q_revenue_inr": _f(row, "q_revenue_cr") * cr if _f(row, "q_revenue_cr") else None,
            "ttm_revenue_inr": _f(row, "ttm_revenue_cr") * cr if _f(row, "ttm_revenue_cr") else None,
            "prev_ttm_revenue_inr": None,
            "ebitda_margin": _f(row, "ebitda_margin"),
            "net_margin": None,
            "q_revenue_yoy": None,
            "source": "manual",
            "confidence": 1.0,
            "market_cap_inr": _f(row, "market_cap_cr") * cr if _f(row, "market_cap_cr") else None,
            "raw": json.dumps(row),
        }
    return None


# ---------------------------------------------------------------- resolver
def resolve(conn, company: dict, nse_client=None,
            max_age_days: int = MAX_AGE_DAYS, force: bool = False) -> dict | None:
    """Return cached fundamentals, refreshing from upstream when stale."""
    cid = company["company_id"]
    cached = get_fundamentals(conn, cid)
    if cached and not force:
        as_of = cached.get("as_of")
        if as_of:
            try:
                age = now_ist() - datetime.fromisoformat(as_of)
                if age < timedelta(days=max_age_days) and cached.get("q_revenue_inr"):
                    return cached
            except ValueError:
                pass

    nse_symbol = company.get("nse_symbol")
    bse_code = company.get("bse_code")
    isin = company.get("isin")

    result = from_manual(nse_symbol, bse_code, isin)
    if result is None and nse_client and nse_symbol:
        try:
            result = from_nse(nse_client, nse_symbol)
        except Exception as exc:
            log.debug("nse fundamentals %s failed: %s", nse_symbol, exc)
    if result is None:
        result = from_yfinance(nse_symbol, bse_code)

    if result is None:
        if cached:
            return cached  # stale beats nothing
        return None

    mcap = result.pop("market_cap_inr", None) or company.get("market_cap_inr")
    row = {
        "company_id": cid,
        "latest_quarter": result.get("latest_quarter"),
        "q_revenue_inr": result.get("q_revenue_inr"),
        "ttm_revenue_inr": result.get("ttm_revenue_inr"),
        "prev_ttm_revenue_inr": result.get("prev_ttm_revenue_inr"),
        "ebitda_margin": result.get("ebitda_margin"),
        "net_margin": result.get("net_margin"),
        "q_revenue_yoy": result.get("q_revenue_yoy"),
        "source": result.get("source"),
        "confidence": result.get("confidence"),
        "as_of": now_ist().isoformat(),
        "raw": result.get("raw"),
    }
    upsert(conn, "fundamentals", row, "company_id")
    if mcap:
        conn.execute("UPDATE companies SET market_cap_inr=? WHERE company_id=?",
                     (mcap, cid))
        row["market_cap_inr"] = mcap
    elif company.get("market_cap_inr"):
        row["market_cap_inr"] = company["market_cap_inr"]
    return row
