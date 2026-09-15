"""Local dashboard over the historical order database.

    python -m order_scanner.cli dashboard     ->  http://127.0.0.1:8000
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .config import DB_PATH
from .db import connect, init_db

app = FastAPI(title="NSE/BSE Order Scanner")


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------
# Cross-exchange de-duplication.
#
# One order win announced on both NSE and BSE arrives as two filings, so it
# is stored as two order rows -- correctly, they are two real filings. Now
# that companies are merged on ISIN / normalised name, both rows sit under
# one company_id, which is what made the same order appear twice in a
# company's history (and twice in the main list, with two different scores,
# because the two filings extract slightly differently).
#
# The display layer collapses them. Nothing is deleted: `exchanges` records
# every exchange the order was seen on.
# --------------------------------------------------------------------------
def _days_apart(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 1e9
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).days)
    except ValueError:
        return 1e9


def _same_order(a: dict, b: dict) -> bool:
    """Are these two filings the same order win?"""
    if a.get("company_id") != b.get("company_id"):
        return False
    va, vb = a.get("order_value_inr"), b.get("order_value_inr")
    if va is not None and vb is not None:
        # Same rule the alert suppression uses: value within 0.5%, 3 days.
        if abs(va - vb) > max(1.0, abs(va) * 0.005):
            return False
        return _days_apart(a.get("filed_at"), b.get("filed_at")) <= 3
    # At most one side has a figure, so there is nothing to compare: fall back
    # to same calendar day + same order type. This is the looser arm of the
    # rule and it does the work the value test cannot -- Accuracy Shipping's
    # CFS award was filed on both exchanges, but only one filing stated the
    # Rs 187.50 Cr, so the value test left them as two rows.
    #
    # A wrong merge here can only drop a filing that carries no value at all,
    # and `duplicate_filings` on the surviving row makes the merge visible.
    return ((a.get("filed_at") or "")[:10] == (b.get("filed_at") or "")[:10]
            and (a.get("order_type") or "") == (b.get("order_type") or ""))


def _collapse_cross_exchange(rows: list[dict]) -> list[dict]:
    """One row per order win, keeping the best-extracted filing of each.

    The survivor is the most completely extracted filing -- a stated value
    first, then an execution period, then the higher score -- because the two
    filings of one order often differ in what the extractor could read out of
    them, and the richer row is the one worth showing.
    """
    by_co: dict = {}
    for r in rows:
        by_co.setdefault(r.get("company_id"), []).append(r)

    keeper_of: dict[int, tuple[dict, list[dict]]] = {}
    for group_rows in by_co.values():
        groups: list[list[dict]] = []
        for r in group_rows:
            for g in groups:
                if _same_order(g[0], r):
                    g.append(r)
                    break
            else:
                groups.append([r])
        for g in groups:
            keep = max(g, key=lambda x: (x.get("order_value_inr") is not None,
                                         x.get("execution_months") is not None,
                                         x.get("score") or 0))
            for x in g:
                keeper_of[id(x)] = (keep, g)

    out: list[dict] = []
    emitted: set[int] = set()
    for r in rows:                      # preserves the incoming sort order
        keep, group = keeper_of[id(r)]
        if id(keep) in emitted:
            continue
        emitted.add(id(keep))
        row = dict(keep)
        row["exchanges"] = sorted({x["exchange"] for x in group if x.get("exchange")})
        row["duplicate_filings"] = len(group)
        # every filing of this order, so each exchange's PDF stays reachable
        row["filings"] = [{"exchange": x.get("exchange"), "order_id": x.get("order_id")}
                          for x in sorted(group, key=lambda x: x.get("exchange") or "")]
        out.append(row)
    return out


@app.get("/api/orders")
def api_orders(
    q: str = Query("", description="company name search"),
    tier: str = "",
    min_score: float = 0,
    min_ratio: float = 0,
    days: int = 90,
    exchange: str = "",
    include_amendments: bool = False,
    has_value: bool = False,
    collapse: bool = True,
    limit: int = 5000,
):
    # Every column is table-qualified: `orders` and `announcements` share
    # several column names, so bare names are ambiguous across the join.
    where = ["o.filed_at >= datetime('now', ?, '+5 hours', '+30 minutes')"]
    params: list = [f"-{int(days)} days"]
    if q:
        where.append("o.company_name LIKE ?")
        params.append(f"%{q}%")
    if tier:
        where.append("o.tier = ?")
        params.append(tier.upper())
    if exchange:
        where.append("o.exchange = ?")
        params.append(exchange.upper())
    if min_score:
        where.append("o.score >= ?")
        params.append(min_score)
    if min_ratio:
        where.append("o.ratio_to_quarter >= ?")
        params.append(min_ratio)
    if not include_amendments:
        where.append("o.is_amendment = 0")
    if has_value:
        # Many filings are disclosures or LoIs with no figure at all; this
        # hides them rather than leaving the value column full of blanks.
        where.append("o.order_value_inr IS NOT NULL")
    params.append(int(limit))

    sql = (f"SELECT o.*, a.headline, a.pdf_url, co.industry, co.sector FROM orders o "
           f"LEFT JOIN announcements a ON a.ann_id = o.ann_id "
           f"LEFT JOIN companies co ON co.company_id = o.company_id "
           f"WHERE {' AND '.join(where)} ORDER BY o.filed_at DESC LIMIT ?")
    rows = _rows(sql, tuple(params))
    return JSONResponse(_collapse_cross_exchange(rows) if collapse else rows)


@app.get("/api/company/{company_id}")
def api_company(company_id: str, collapse: bool = True):
    orders = _rows(
        "SELECT * FROM orders WHERE company_id=? ORDER BY filed_at DESC",
        (company_id,))
    return JSONResponse({
        "company": _rows("SELECT * FROM companies WHERE company_id=?", (company_id,)),
        "fundamentals": _rows("SELECT * FROM fundamentals WHERE company_id=?", (company_id,)),
        "orders": _collapse_cross_exchange(orders) if collapse else orders,
        "n_filings": len(orders),
    })


_pdf_session = None


@app.get("/api/pdf/{order_id}")
def api_pdf(order_id: str):
    """One order filing's PDF, served from the local cache.

    Scans cache every order filing's PDF, so this keeps working after the
    exchange moves or drops the file. A PDF not cached yet is fetched once and
    cached; one that cannot be fetched sends the browser to the exchange link.
    """
    global _pdf_session
    from .extract.pdf import download_pdf

    rows = _rows(
        "SELECT o.company_name, o.filed_at, o.exchange, a.pdf_url FROM orders o "
        "LEFT JOIN announcements a ON a.ann_id = o.ann_id WHERE o.order_id = ?",
        (order_id,))
    if not rows or not rows[0].get("pdf_url"):
        return JSONResponse({"error": "no PDF on record for this order"}, status_code=404)
    r = rows[0]
    if _pdf_session is None:
        from .sources.bse import BSEClient
        _pdf_session = BSEClient().s      # the headers the scanner downloads with
    data = download_pdf(_pdf_session, r["pdf_url"])
    if not data:
        return RedirectResponse(r["pdf_url"])
    name = re.sub(r"[^A-Za-z0-9]+", "-",
                  f"{r['company_name']} {(r['filed_at'] or '')[:10]} {r['exchange'] or ''}")
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name.strip("-")}.pdf"'})


@app.get("/api/stats")
def api_stats():
    # Counted over collapsed orders so the header agrees with the table.
    orders = _collapse_cross_exchange(
        _rows("SELECT * FROM orders WHERE is_amendment=0"))
    tiers: dict[str, int] = {}
    for o in orders:
        tiers[o.get("tier") or "?"] = tiers.get(o.get("tier") or "?", 0) + 1
    return JSONResponse({
        "totals": {
            "n_orders": len(orders),
            "n_high": tiers.get("HIGH", 0),
            "n_medium": tiers.get("MEDIUM", 0),
            "n_watch": tiers.get("WATCH", 0),
            "total_value": sum(o["order_value_inr"] or 0 for o in orders),
            "n_valued": sum(1 for o in orders if o.get("order_value_inr")),
        },
        "runs": _rows("SELECT * FROM runs ORDER BY run_id DESC LIMIT 10"),
        "top_companies": _rows(
            "SELECT company_id, company_name, COUNT(*) n, SUM(order_value_inr) v "
            "FROM orders WHERE is_amendment=0 GROUP BY company_id "
            "ORDER BY v DESC LIMIT 15"),
    })


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


# --------------------------------------------------------------------------
# Colour roles. Tier is a STATUS scale, so it carries status hues: HIGH takes
# `critical` red, MEDIUM `serious` orange, WATCH blue, and STORE stays neutral
# ink with a hollow dot. That trio was run through the palette validator, all
# pairs, in both themes: worst colour-blind dE 13.9, normal-vision 15.7, clear
# of both floors. Violet (dE 1.9 from the blue under protan, dark) and aqua
# (2.9 from orange) were tried for WATCH and rejected, so violet only ever
# appears as decoration -- logo, background glow, a tile orb -- never on a
# data mark.
#
# Hue is never load-bearing: every tier shows its name as text, which is also
# the relief for `serious` sitting below 3:1 on the light surface. Values stay
# in text ink; colour lives on the marks beside them (dots, rings, pill edges).
#
# The 3D is CSS only -- a perspective grid floor, a turning cube mark, tilt on
# the summary tiles, bevelled score rings, a detail panel that unfolds -- so
# the page needs no CDN, and all motion stops under prefers-reduced-motion.
#
# Industry chips carry a colour chosen to read as the industry itself --
# medicine green for pharmaceuticals, safety orange for construction, copper
# for cables, maroon for railway wagons -- picked per label and spaced so the
# closest pair among the current labels sits at OKLab dE 8.9. That is below
# what a categorical series palette needs, and acceptable here only because
# every chip names its industry in text: the colour is a quick cue, never the
# identity. On dark surfaces the colours are lifted toward white.
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Order Scanner</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%232a78d6'/%3E%3Cstop offset='1' stop-color='%236a5ae0'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect x='4' y='4' width='24' height='24' rx='6' fill='url(%23g)'/%3E%3C/svg%3E">
<style>
/* ---------------------------------------------------------------- tokens */
:root{
  color-scheme:light;
  --plane:#eef0f5;
  --ink:#0d0f14; --ink-2:#4a4f5c; --ink-3:#737884;
  --line:rgba(13,15,20,.07); --rule:rgba(13,15,20,.14);
  --solid:#fbfbfd;
  --glass:rgba(255,255,255,.62);
  --glass-2:rgba(255,255,255,.45);
  --glass-hi:rgba(255,255,255,.92);
  --panel:rgba(255,255,255,.78);
  --head:rgba(247,248,251,.96);
  --edge:rgba(255,255,255,.85);
  --hover:rgba(42,120,214,.045);
  --lift:inset 0 1px 0 rgba(255,255,255,.9),0 1px 2px rgba(20,24,40,.05),0 14px 32px -16px rgba(20,24,40,.20);
  --deep:inset 0 1px 0 rgba(255,255,255,.9),0 2px 4px rgba(20,24,40,.05),0 28px 50px -22px rgba(20,24,40,.32);
  --orb-1:rgba(42,120,214,.22); --orb-2:rgba(106,90,224,.16); --orb-3:rgba(236,131,90,.10);
  --gridline:rgba(42,70,140,.11);
  --disc-hi:#ffffff; --disc-lo:#e3e6ee;
  --high:#d03b3b; --medium:#ec835a; --watch:#2a78d6; --store:#737884;
  --accent:#2a78d6; --violet:#6a5ae0;
  --sc-lift:0%;
}
@media(prefers-color-scheme:dark){:root:where(:not([data-theme=light])){
  color-scheme:dark;
  --plane:#0a0b0f;
  --ink:#f3f4f7; --ink-2:#b6bac6; --ink-3:#7c818e;
  --line:rgba(255,255,255,.065); --rule:rgba(255,255,255,.13);
  --solid:#15161b;
  --glass:rgba(22,24,31,.6);
  --glass-2:rgba(255,255,255,.035);
  --glass-hi:rgba(255,255,255,.075);
  --panel:rgba(26,28,36,.72);
  --head:rgba(18,19,25,.95);
  --edge:rgba(255,255,255,.08);
  --hover:rgba(255,255,255,.03);
  --lift:inset 0 1px 0 rgba(255,255,255,.06),0 1px 2px rgba(0,0,0,.4),0 18px 40px -18px rgba(0,0,0,.75);
  --deep:inset 0 1px 0 rgba(255,255,255,.07),0 2px 4px rgba(0,0,0,.4),0 34px 60px -24px rgba(0,0,0,.85);
  --orb-1:rgba(57,135,229,.20); --orb-2:rgba(144,133,233,.16); --orb-3:rgba(217,89,38,.08);
  --gridline:rgba(130,150,255,.085);
  --disc-hi:#2c2f3a; --disc-lo:#121318;
  --watch:#3987e5; --store:#7c818e; --accent:#3987e5; --violet:#9085e9;
  --sc-lift:28%;
}}
:root[data-theme=dark]{
  color-scheme:dark;
  --plane:#0a0b0f;
  --ink:#f3f4f7; --ink-2:#b6bac6; --ink-3:#7c818e;
  --line:rgba(255,255,255,.065); --rule:rgba(255,255,255,.13);
  --solid:#15161b;
  --glass:rgba(22,24,31,.6);
  --glass-2:rgba(255,255,255,.035);
  --glass-hi:rgba(255,255,255,.075);
  --panel:rgba(26,28,36,.72);
  --head:rgba(18,19,25,.95);
  --edge:rgba(255,255,255,.08);
  --hover:rgba(255,255,255,.03);
  --lift:inset 0 1px 0 rgba(255,255,255,.06),0 1px 2px rgba(0,0,0,.4),0 18px 40px -18px rgba(0,0,0,.75);
  --deep:inset 0 1px 0 rgba(255,255,255,.07),0 2px 4px rgba(0,0,0,.4),0 34px 60px -24px rgba(0,0,0,.85);
  --orb-1:rgba(57,135,229,.20); --orb-2:rgba(144,133,233,.16); --orb-3:rgba(217,89,38,.08);
  --gridline:rgba(130,150,255,.085);
  --disc-hi:#2c2f3a; --disc-lo:#121318;
  --watch:#3987e5; --store:#7c818e; --accent:#3987e5; --violet:#9085e9;
  --sc-lift:28%;
}
.t-HIGH{--tc:var(--high)} .t-MEDIUM{--tc:var(--medium)}
.t-WATCH{--tc:var(--watch)} .t-STORE{--tc:var(--store)}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;background:var(--plane);color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased}
button,input{font:inherit}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:2px}
.muted{color:var(--ink-3)}
.clip{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nowrap{white-space:nowrap}

/* ---------------------------------------------------------------- scene
   Decoration only, fixed behind everything: three slow glows and a grid
   floor laid back in perspective, masked out before it reaches the table. */
.scene{position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden}
.orb{position:absolute;border-radius:50%;animation:drift 32s ease-in-out infinite alternate}
.o1{width:52rem;height:52rem;left:-16rem;top:-24rem;
  background:radial-gradient(closest-side,var(--orb-1),transparent)}
.o2{width:44rem;height:44rem;right:-14rem;top:-12rem;animation-duration:40s;
  background:radial-gradient(closest-side,var(--orb-2),transparent)}
.o3{width:40rem;height:40rem;left:35%;bottom:-24rem;animation-duration:46s;
  background:radial-gradient(closest-side,var(--orb-3),transparent)}
@keyframes drift{to{transform:translate3d(5rem,3rem,0) scale(1.1)}}
.floor{position:absolute;left:-50%;right:-50%;bottom:0;height:55vh;
  background-image:linear-gradient(var(--gridline) 1px,transparent 1px),
    linear-gradient(90deg,var(--gridline) 1px,transparent 1px);
  background-size:72px 72px;background-position:center bottom;
  transform:perspective(520px) rotateX(58deg);transform-origin:50% 100%;
  -webkit-mask-image:linear-gradient(to top,#000,transparent 80%);
  mask-image:linear-gradient(to top,#000,transparent 80%)}

.glass{background:var(--glass);border:1px solid var(--edge);border-radius:18px;
  box-shadow:var(--lift);
  -webkit-backdrop-filter:blur(24px) saturate(160%);
  backdrop-filter:blur(24px) saturate(160%)}
/* Glass is decoration, never a dependency: fall back to the solid surface
   where it is unsupported or unwanted. */
@supports not ((backdrop-filter:blur(2px)) or (-webkit-backdrop-filter:blur(2px))){
  .glass{background:var(--solid)}
}
@media(prefers-reduced-transparency:reduce){
  .glass{background:var(--solid);-webkit-backdrop-filter:none;backdrop-filter:none}
}

/* ---------------------------------------------------------------- shell */
.shell{max-width:1560px;margin:0 auto;padding:0 24px 80px}

/* No bottom padding: --barh is measured from this element and the sticky
   table header docks directly beneath it, so any gap would be a band where
   scrolling rows show through. The band above the pill blurs whatever is
   behind it instead of painting flat plane, so the glows stay continuous;
   it is fixed and viewport-wide so it has no visible side edges. */
.top{position:sticky;top:0;z-index:30;padding:14px 0 0}
.top::before{content:"";position:fixed;left:0;right:0;top:0;height:var(--barh,70px);z-index:-1;
  background:color-mix(in oklab,var(--plane) 70%,transparent);
  -webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);
  -webkit-mask-image:linear-gradient(#000 72%,transparent);
  mask-image:linear-gradient(#000 72%,transparent)}
.topin{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:10px 12px 10px 14px}
.brand{display:flex;gap:12px;align-items:center}
.logo{width:34px;height:34px;display:grid;place-items:center;perspective:140px;flex:none}
.cube{position:relative;width:18px;height:18px;transform-style:preserve-3d;
  transform:rotateX(-26deg) rotateY(40deg);animation:spin 16s linear infinite}
.cube i{position:absolute;inset:0;border-radius:4px;border:1px solid rgba(255,255,255,.35);
  background:linear-gradient(135deg,var(--accent),var(--violet))}
.cube i:nth-child(1){transform:translateZ(9px)}
.cube i:nth-child(2){transform:rotateY(180deg) translateZ(9px);filter:brightness(.7)}
.cube i:nth-child(3){transform:rotateY(90deg) translateZ(9px);filter:brightness(.82)}
.cube i:nth-child(4){transform:rotateY(-90deg) translateZ(9px);filter:brightness(.82)}
.cube i:nth-child(5){transform:rotateX(90deg) translateZ(9px);filter:brightness(1.25)}
.cube i:nth-child(6){transform:rotateX(-90deg) translateZ(9px);filter:brightness(.6)}
@keyframes spin{from{transform:rotateX(-26deg) rotateY(0)}
  to{transform:rotateX(-26deg) rotateY(360deg)}}
h1{font-size:15px;line-height:1.2;margin:0;font-weight:650;letter-spacing:-.01em;white-space:nowrap}
.tag{font-size:11.5px;color:var(--ink-3);white-space:nowrap}
.spacer{flex:1 1 auto}
.since{display:inline-flex;align-items:center;gap:8px;font-size:12px;color:var(--ink-2);
  padding:4px 12px 4px 10px;border-radius:999px;background:var(--glass-2);
  border:1px solid var(--line);white-space:nowrap}
.since::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 0 3px color-mix(in oklab,var(--accent) 25%,transparent)}
.since:empty{display:none}

/* segmented controls: theme and tier */
.seg,.chips{display:inline-flex;gap:3px;padding:3px;border-radius:12px;flex-wrap:wrap;
  background:var(--glass-2);border:1px solid var(--line)}
.seg button,.chips button{appearance:none;border:0;background:none;color:var(--ink-3);
  font-size:12px;padding:4px 11px;border-radius:9px;cursor:pointer;line-height:1.5;
  display:inline-flex;align-items:center;gap:7px;transition:color .15s,background .15s}
.chips button{color:var(--ink-2);font-size:12.5px;padding:5px 12px}
.seg button:hover,.chips button:hover{color:var(--ink)}
.seg button[aria-pressed=true],.chips button[aria-pressed=true]{color:var(--ink);
  background:var(--glass-hi);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.2),0 1px 2px rgba(0,0,0,.12),
    0 6px 14px -10px rgba(0,0,0,.45)}
.seg button:focus-visible,.chips button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}

/* ---------------------------------------------------------------- filters */
.bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:14px 0 0;padding:10px 12px}
.search{position:relative;display:flex;align-items:center;flex:0 1 280px;min-width:180px}
.search svg{position:absolute;left:12px;color:var(--ink-3);pointer-events:none}
#q{width:100%;background:var(--glass-2);color:var(--ink);border:1px solid var(--rule);
  border-radius:11px;padding:7px 12px 7px 34px;font-size:13px;
  box-shadow:inset 0 1px 2px rgba(0,0,0,.06);transition:border-color .15s,box-shadow .15s}
#q::placeholder{color:var(--ink-3)}
#q:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px color-mix(in oklab,var(--accent) 22%,transparent)}
.switch{position:relative;display:inline-flex;align-items:center;gap:9px;font-size:12.5px;
  color:var(--ink-2);cursor:pointer;white-space:nowrap;padding:4px 2px}
.switch input{position:absolute;opacity:0;width:1px;height:1px;margin:0}
.track{position:relative;width:32px;height:18px;border-radius:999px;background:var(--rule);
  box-shadow:inset 0 1px 2px rgba(0,0,0,.22);transition:background .2s}
.track::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;
  border-radius:50%;background:linear-gradient(180deg,#fff,#e6e8ee);
  box-shadow:0 1px 3px rgba(0,0,0,.35);transition:transform .2s cubic-bezier(.2,.8,.2,1)}
.switch input:checked+.track{background:linear-gradient(90deg,var(--accent),var(--violet))}
.switch input:checked+.track::after{transform:translateX(14px)}
.switch input:focus-visible+.track{outline:2px solid var(--accent);outline-offset:2px}

/* ---------------------------------------------------------------- summary
   Tiles tilt toward the pointer; the transform lives on the tile itself
   (not a shared parent perspective) so edge tiles don't skew. */
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:14px 0 0}
.kpi{position:relative;padding:16px 18px;min-height:104px;
  transform:perspective(800px) rotateX(var(--rx,0deg)) rotateY(var(--ry,0deg)) translateY(var(--ty,0px));
  transition:transform .5s cubic-bezier(.2,.8,.2,1),box-shadow .3s}
.kpi:hover{--ty:-3px;box-shadow:var(--deep)}
.kpi.moving{transition:box-shadow .3s}
.kpi::before{content:"";position:absolute;left:18px;top:-1px;width:44px;height:3px;
  border-radius:0 0 3px 3px;background:var(--kc)}
.klabel{font-size:12.5px;color:var(--ink-2);font-weight:500;padding-right:44px}
.kval{font-size:30px;font-weight:650;letter-spacing:-.025em;line-height:1.1;margin-top:10px}
.ksub{font-size:12px;color:var(--ink-3);margin-top:4px}
.orb3d{position:absolute;right:18px;top:18px;width:34px;height:34px;border-radius:50%;
  background:
    radial-gradient(circle at 32% 26%,rgba(255,255,255,.9),rgba(255,255,255,0) 22%),
    radial-gradient(circle at 36% 34%,color-mix(in oklab,var(--kc) 60%,#fff),var(--kc) 48%,
      color-mix(in oklab,var(--kc) 45%,#000));
  box-shadow:0 12px 18px -10px color-mix(in oklab,var(--kc) 70%,transparent)}
.glare{position:absolute;inset:0;border-radius:inherit;pointer-events:none;opacity:0;
  transition:opacity .3s;
  background:radial-gradient(18rem circle at var(--gx,50%) var(--gy,0%),rgba(255,255,255,.12),transparent 50%)}
.kpi:hover .glare{opacity:1}
/* the count tiles double as tier filters (see setTier); the active tier gets
   a ring in its own colour and the other tiles' numbers step back */
.kpi[role=button]{cursor:pointer}
.kpi[role=button]:focus-visible{outline:2px solid var(--kc);outline-offset:2px}
.kpi[aria-pressed=true]{box-shadow:0 0 0 1.5px var(--kc),var(--deep)}
.kpi[aria-pressed=true] .klabel{color:var(--ink)}
.kval{transition:opacity .2s}
.kpis.filtered .kpi[aria-pressed=false] .kval{opacity:.5}

/* ---------------------------------------------------------------- table */
/* `overflow:hidden` and `overflow-x:auto` both make an element a scroll
   container, and a sticky thead then resolves against THAT box instead of the
   viewport. `overflow:clip` clips the rounded corners without creating a
   scrollport, so sticky still sees the viewport. Horizontal scrolling is only
   enabled where the table actually cannot fit. */
.card{margin:14px 0 0;overflow:hidden;overflow:clip;transition:opacity .2s}
.card.busy{opacity:.55}
.scroll{overflow-x:clip}
/* At those widths the scroll box becomes the sticky header's reference, so an
   offset of --barh would push the header down into the rows: dock it at 0.
   (`.scroll` in the selector outranks the `thead th` rule further down.) */
@media(max-width:1180px){.scroll{overflow-x:auto} .scroll thead th{top:0}}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{position:sticky;top:var(--barh,70px);z-index:10;
  text-align:left;font-weight:600;color:var(--ink-3);font-size:11.5px;
  padding:13px 20px 11px 0;border-bottom:1px solid var(--line);white-space:nowrap;
  cursor:pointer;user-select:none;background:var(--head);
  -webkit-backdrop-filter:blur(22px) saturate(160%);
  backdrop-filter:blur(22px) saturate(160%)}
thead th:first-child{padding-left:24px}
thead th:last-child{padding-right:24px}
thead th:hover{color:var(--ink-2)}
thead th[aria-sort]{color:var(--ink)}
thead th[aria-sort=descending]::after{content:"↓";margin-left:5px;color:var(--accent)}
thead th[aria-sort=ascending]::after{content:"↑";margin-left:5px;color:var(--accent)}
tbody td{padding:13px 20px 13px 0;border-bottom:1px solid var(--line);vertical-align:middle}
tbody td:first-child{padding-left:24px;position:relative}
tbody td:last-child{padding-right:24px}
tbody tr:last-child>td{border-bottom:0}
tr.row>td{transition:background .15s}
tr.row:hover>td,tr.row.open>td{background:var(--hover)}
tr.row.open>td{border-bottom-color:transparent}
/* the alerting tiers get a coloured edge; STORE rows stay quiet */
tr.row>td:first-child::before{content:"";position:absolute;left:0;top:12px;bottom:12px;
  width:3px;border-radius:0 3px 3px 0;background:var(--tc)}
tr.row.t-STORE>td:first-child::before{display:none}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
th.num{text-align:right}

.date .d1{display:block;white-space:nowrap;font-weight:500}
.date .d2{display:block;font-size:11.5px;color:var(--ink-3)}

/* company cell */
.cowrap{display:flex;align-items:center;gap:8px;min-width:0}
.cotoggle{display:flex;gap:11px;align-items:center;flex:1 1 auto;min-width:0;
  background:none;border:0;padding:0;margin:0;color:inherit;cursor:pointer;text-align:left}
.cotoggle:focus-visible{outline:2px solid var(--accent);outline-offset:4px;border-radius:8px}
.chev{flex:none;display:grid;place-items:center;width:24px;height:24px;border-radius:8px;
  color:var(--ink-3);background:var(--glass-2);border:1px solid var(--line);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08);
  transition:transform .25s cubic-bezier(.2,.8,.2,1),color .15s,background .15s}
.cotoggle:hover .chev{color:var(--ink);background:var(--glass-hi)}
[aria-expanded=true] .chev{transform:rotate(90deg);color:var(--accent);background:var(--glass-hi)}
.cotext{display:block;min-width:0}
.coline{display:block}
.co{font-weight:600;letter-spacing:-.005em}
.cotoggle:hover .co{text-decoration:underline;text-underline-offset:3px;
  text-decoration-color:var(--rule)}
/* industry chip: a tint and a dot in the industry's own colour (set inline as
   --sc); lifted toward white on dark surfaces so maroon or tyre-black stays
   visible. The text stays in ink. */
.sector{--sc:var(--ink-3);--scl:color-mix(in oklab,var(--sc),#fff var(--sc-lift));
  display:inline-flex;align-items:center;gap:6px;margin-left:9px;
  padding:0 9px 0 7px;border-radius:999px;font-size:11px;font-weight:500;line-height:18px;
  vertical-align:1px;color:var(--ink-2);white-space:nowrap;
  background:color-mix(in oklab,var(--scl) 16%,transparent);
  border:1px solid color-mix(in oklab,var(--scl) 40%,transparent)}
.sector::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--scl)}
.dtitle .sector{font-size:11.5px;vertical-align:2px}
.extlink{display:inline-flex;align-items:center;gap:5px;margin-left:9px;padding:1px 9px;
  border-radius:8px;border:1px solid var(--line);background:var(--glass-2);color:var(--ink-2);
  font-size:11.5px;font-weight:600;vertical-align:2px;
  transition:color .15s,border-color .15s,background .15s}
.extlink:hover{color:var(--accent);background:var(--glass-hi);text-decoration:none;
  border-color:color-mix(in oklab,var(--accent) 40%,transparent)}
.exs{display:inline-flex;gap:4px;margin-left:8px;vertical-align:2px}
.ex{font-size:9.5px;font-weight:650;letter-spacing:.05em;color:var(--ink-3);
  padding:0 5px;border-radius:5px;border:1px solid var(--line)}
.pdf{flex:none;display:grid;place-items:center;width:30px;height:30px;border-radius:9px;
  color:var(--ink-3);border:1px solid transparent;
  transition:color .15s,background .15s,transform .15s,box-shadow .15s}
.pdf:hover{color:var(--accent);background:var(--glass-hi);border-color:var(--line);
  box-shadow:var(--lift);transform:translateY(-1px);text-decoration:none}
/* per-filing PDF links in the order history */
.pdfchip{display:inline-flex;align-items:center;gap:5px;margin-right:6px;padding:2px 9px 2px 7px;
  border-radius:8px;border:1px solid var(--line);background:var(--glass-2);color:var(--ink-2);
  font-size:11.5px;font-weight:600;letter-spacing:.03em;
  transition:color .15s,border-color .15s,background .15s}
.pdfchip:hover{color:var(--accent);background:var(--glass-hi);text-decoration:none;
  border-color:color-mix(in oklab,var(--accent) 40%,transparent)}

/* value / ratio / customer */
.val{display:block;font-weight:550}
.minor{display:block;font-size:11px;color:var(--ink-3)}
.nd{color:var(--ink-3);font-size:12.5px;border-bottom:1px dotted var(--rule);cursor:help}
.rbar{display:block;width:64px;height:4px;margin:6px 0 0 auto;border-radius:2px;
  background:var(--rule);overflow:hidden}
.rbar i{display:block;height:100%;border-radius:2px;background:var(--ink-3)}
.rbar.over i{background:linear-gradient(90deg,var(--ink-3),var(--ink))}
.cname{max-width:30ch}
.ctype{display:inline-block;margin-top:4px;font-size:10.5px;font-weight:600;
  letter-spacing:.03em;color:var(--ink-2);padding:0 7px;border-radius:6px;
  background:var(--glass-2);border:1px solid var(--line)}

/* signal: bevelled score ring + tier pill */
.sig{display:flex;align-items:center;gap:12px}
.ring{position:relative;flex:none;width:38px;height:38px;border-radius:50%;
  display:grid;place-items:center;
  background:conic-gradient(var(--tc) calc(var(--p)*1%),
    color-mix(in oklab,var(--tc) 18%,transparent) 0);
  box-shadow:0 8px 14px -9px color-mix(in oklab,var(--tc) 85%,transparent)}
.ring::before{content:"";position:absolute;inset:4px;border-radius:50%;
  background:radial-gradient(circle at 35% 30%,var(--disc-hi),var(--disc-lo) 78%);
  box-shadow:inset 0 1px 1px rgba(255,255,255,.2),inset 0 -2px 4px rgba(0,0,0,.2),
    0 1px 2px rgba(0,0,0,.25)}
.ring b{position:relative;font-size:12px;font-weight:650;font-variant-numeric:tabular-nums}
.pill{display:inline-flex;align-items:center;gap:7px;padding:3px 11px 3px 9px;
  border-radius:999px;font-size:12px;font-weight:600;color:var(--ink);white-space:nowrap;
  background:linear-gradient(180deg,color-mix(in oklab,var(--tc) 22%,transparent),
    color-mix(in oklab,var(--tc) 8%,transparent));
  border:1px solid color-mix(in oklab,var(--tc) 34%,transparent);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.18),
    0 6px 12px -8px color-mix(in oklab,var(--tc) 70%,transparent)}
.t-STORE .pill{font-weight:500;color:var(--ink-2);box-shadow:inset 0 1px 0 rgba(255,255,255,.12)}
.pill.sm{font-size:11.5px;padding:1px 9px 1px 8px}
.dot{width:7px;height:7px;border-radius:50%;flex:none;background:var(--tc,var(--ink-3))}
.t-STORE .dot,.dot.t-STORE{background:none;box-shadow:inset 0 0 0 1.5px var(--tc)}

/* ---------------------------------------------------------------- detail */
tr.det[hidden]{display:none}
tr.det>td{padding:0 24px 20px;background:var(--hover);border-bottom:1px solid var(--line)}
.stage{perspective:1400px}
/* Unfolds from its top edge. A CSS animation restarts every time the row
   goes from display:none back to shown, so it plays on each open. */
.detbox{padding:18px 20px;border-radius:16px;background:var(--panel);
  border:1px solid var(--edge);box-shadow:var(--deep);overflow-x:auto;
  transform-origin:50% 0;animation:unfold .45s cubic-bezier(.2,.8,.2,1) both}
@keyframes unfold{from{opacity:0;transform:rotateX(-16deg) translateY(-6px) scale(.985)}
  to{opacity:1;transform:none}}
.dethead{display:flex;gap:18px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.dtitle{font-size:16px;font-weight:650;letter-spacing:-.01em}
.dids{font-size:12px;color:var(--ink-3);margin-top:2px}
.facts{display:flex;gap:10px;flex-wrap:wrap}
.fact{min-width:128px;padding:8px 14px 9px;border-radius:12px;background:var(--glass-hi);
  border:1px solid var(--edge);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.1),0 1px 2px rgba(0,0,0,.08),
    0 10px 20px -14px rgba(0,0,0,.45)}
.fact span{display:block;font-size:11px;color:var(--ink-3)}
.fact b{display:block;font-size:15px;font-weight:650;margin-top:1px}
.detnote{color:var(--ink-3);font-size:12px;margin:14px 0 6px}
/* management guidance behind the score, from the filing itself */
.guidance{margin:4px 0 12px;padding:9px 13px;border-radius:11px;font-size:12.5px;
  color:var(--ink-2);background:color-mix(in oklab,var(--accent) 9%,transparent);
  border:1px solid color-mix(in oklab,var(--accent) 24%,transparent)}
.guidance b{color:var(--ink);margin-right:8px;font-weight:650}
/* Full width, like the main table: auto layout spreads the extra width
   across columns in proportion to their content. */
table.inner{width:100%;font-size:12.5px}
table.inner th{position:static;background:none;backdrop-filter:none;
  -webkit-backdrop-filter:none;border-bottom:1px solid var(--line);
  padding:0 18px 8px 0;cursor:default;font-size:11px}
table.inner th:first-child,table.inner td:first-child{padding-left:12px}
table.inner td{border-bottom:1px solid var(--line);padding:9px 18px 9px 0}
table.inner tbody tr:last-child td{border-bottom:0}
table.inner .clip{max-width:36ch}
table.inner tr.cur>td{background:color-mix(in oklab,var(--accent) 11%,transparent);font-weight:600}
table.inner tr.cur td:first-child{border-radius:9px 0 0 9px}
table.inner tr.cur td:last-child{border-radius:0 9px 9px 0}

.empty{padding:72px 0;text-align:center;color:var(--ink-3)}
@media(max-width:1000px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:760px){
  .shell{padding:0 14px 60px}
  .since,.tag{display:none}
  .klabel{padding-right:30px}
  .orb3d{width:26px;height:26px;right:14px;top:16px}
  .cname{max-width:26ch}
  .search{flex:1 1 100%}
  .kval{font-size:24px}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
}
</style></head><body>

<div class="scene" aria-hidden="true">
  <div class="orb o1"></div><div class="orb o2"></div><div class="orb o3"></div>
  <div class="floor"></div>
</div>

<div class="shell">
  <div class="top"><div class="topin glass">
    <div class="brand">
      <span class="logo" aria-hidden="true"><span class="cube"><i></i><i></i><i></i><i></i><i></i><i></i></span></span>
      <div><h1>Order Scanner</h1><div class="tag">NSE · BSE order wins</div></div>
    </div>
    <span class="spacer"></span>
    <span class="since" id="since"></span>
    <div class="seg" id="theme" role="group" aria-label="Colour theme">
      <button type="button" data-t="system" aria-pressed="true">Auto</button>
      <button type="button" data-t="light" aria-pressed="false">Light</button>
      <button type="button" data-t="dark" aria-pressed="false">Dark</button>
    </div>
  </div></div>

  <div class="bar glass" id="bar">
    <label class="search">
      <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><circle cx="7" cy="7" r="4.6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="m10.5 10.5 3.2 3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      <input id="q" placeholder="Search companies" aria-label="Search company" autocomplete="off">
    </label>
    <div class="chips" id="tier" role="group" aria-label="Tier">
      <button type="button" data-v="" aria-pressed="true">All tiers</button>
      <button type="button" data-v="HIGH" aria-pressed="false"><i class="dot t-HIGH"></i>High</button>
      <button type="button" data-v="MEDIUM" aria-pressed="false"><i class="dot t-MEDIUM"></i>Medium</button>
      <button type="button" data-v="WATCH" aria-pressed="false"><i class="dot t-WATCH"></i>Watch</button>
      <button type="button" data-v="STORE" aria-pressed="false"><i class="dot t-STORE"></i>Store</button>
    </div>
    <span class="spacer"></span>
    <label class="switch"><input type="checkbox" id="hasval"><span class="track"></span>Value disclosed</label>
    <label class="switch"><input type="checkbox" id="amd"><span class="track"></span>Amendments</label>
  </div>

  <section class="kpis" id="kpis" aria-label="Summary of the orders in view">
    <div class="kpi glass tilt" style="--kc:var(--accent)" role="button" tabindex="0"
        data-tier="" title="Show every tier"><span class="orb3d"></span><span class="glare"></span>
      <div class="klabel">Orders in view</div><div class="kval" id="k-n">—</div>
      <div class="ksub" id="k-n-sub">&nbsp;</div></div>
    <div class="kpi glass tilt" style="--kc:var(--high)" role="button" tabindex="0"
        data-tier="HIGH" aria-pressed="false" title="Show only High tier orders"><span class="orb3d"></span><span class="glare"></span>
      <div class="klabel">High tier</div><div class="kval" id="k-high">—</div>
      <div class="ksub">Strongest signals</div></div>
    <div class="kpi glass tilt" style="--kc:var(--medium)" role="button" tabindex="0"
        data-tier="MEDIUM" aria-pressed="false" title="Show only Medium tier orders"><span class="orb3d"></span><span class="glare"></span>
      <div class="klabel">Medium tier</div><div class="kval" id="k-med">—</div>
      <div class="ksub">Worth a closer look</div></div>
    <div class="kpi glass tilt" style="--kc:var(--violet)"><span class="orb3d"></span><span class="glare"></span>
      <div class="klabel">Disclosed order value</div><div class="kval" id="k-val">—</div>
      <div class="ksub" id="k-val-sub">&nbsp;</div></div>
  </section>

  <div class="card glass" id="card">
    <div class="scroll"><table>
      <thead><tr>
        <th data-k="filed_at" aria-sort="descending">Filed</th>
        <th data-k="company_name">Company</th>
        <th data-k="order_value_inr" class="num">Order</th>
        <th data-k="ratio_to_quarter" class="num">vs Q revenue</th>
        <th data-k="execution_months" class="num">Exec</th>
        <th data-k="customer">Customer</th>
        <th data-k="score">Signal</th>
      </tr></thead><tbody id="tb"></tbody>
    </table></div>
    <div class="empty" id="empty" hidden>No orders match these filters.</div>
  </div>
</div>

<script>
const $=id=>document.getElementById(id);

/* ------------------------------------------------------------ theme
   Three states. "Auto" stamps nothing and lets prefers-color-scheme decide;
   Light and Dark stamp data-theme, which beats the media query in both
   directions. Persisted per browser. */
const THEME_KEY='order-scanner-theme';
function applyTheme(t){
  if(t==='light'||t==='dark') document.documentElement.dataset.theme=t;
  else delete document.documentElement.dataset.theme;
  theme.querySelectorAll('button').forEach(b=>
    b.setAttribute('aria-pressed',String(b.dataset.t===(t||'system'))));
}
let saved='system';
try{saved=localStorage.getItem(THEME_KEY)||'system'}catch(e){}
applyTheme(saved);
theme.addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b) return;
  applyTheme(b.dataset.t);
  try{localStorage.setItem(THEME_KEY,b.dataset.t)}catch(e){}
});

/* the sticky table header sits directly under the sticky top bar, whose
   height changes if the title row wraps on a narrow window */
const setBarH=()=>document.documentElement.style.setProperty(
  '--barh',(document.querySelector('.top').offsetHeight)+'px');
addEventListener('resize',setBarH); setBarH();

/* ------------------------------------------------------------ tilt
   Pointer-driven tilt on the summary tiles. Mouse only, and never under
   reduced motion. */
const still=matchMedia('(prefers-reduced-motion: reduce)');
document.querySelectorAll('.tilt').forEach(el=>{
  el.addEventListener('pointermove',e=>{
    if(still.matches||e.pointerType!=='mouse') return;
    const b=el.getBoundingClientRect(),
          x=(e.clientX-b.left)/b.width, y=(e.clientY-b.top)/b.height;
    el.classList.add('moving');
    el.style.setProperty('--ry',((x-.5)*12).toFixed(2)+'deg');
    el.style.setProperty('--rx',((.5-y)*12).toFixed(2)+'deg');
    el.style.setProperty('--gx',(x*100).toFixed(1)+'%');
    el.style.setProperty('--gy',(y*100).toFixed(1)+'%');
  });
  el.addEventListener('pointerleave',()=>{
    el.classList.remove('moving');
    el.style.removeProperty('--rx'); el.style.removeProperty('--ry');
  });
});

/* ------------------------------------------------------------ helpers */
let allRows=[],rows=[],sortK='filed_at',sortD=-1,tierV='';
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const cr=v=>v==null?'—':v>=1e7?(v/1e7).toFixed(2)+' Cr':v>=1e5?(v/1e5).toFixed(2)+' L':Math.round(v);
const inr=v=>v==null?'—':'₹'+cr(v);
const big=v=>v>=1e7?'₹'+(v/1e7).toLocaleString('en-IN',{maximumFractionDigits:v>=1e9?0:1})+' Cr':inr(v);
const pct=v=>v==null?'—':(v*100).toFixed(0)+'%';
const esc=s=>(s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const mon=m=>m?(m>=12&&m%12===0?(m/12)+' yr':Math.round(m)+' mo'):'<span class="muted">—</span>';
const dshort=s=>{const [y,m,d]=(s||'').slice(0,10).split('-');
  return d?[(+d)+' '+MON[+m-1],y]:['—',''];};
const cap=s=>s?s[0].toUpperCase()+s.slice(1).toLowerCase():'';
const OTYPE={work_order:'Work order',loa:'LoA',loi:'LoI',mou:'MoU',l1:'L1'};
const otype=t=>!t||t==='unknown'?'':(OTYPE[t]||cap(t.replace(/_/g,' ')));
const CTYPE={government:'Govt',psu:'PSU',private:'Private',export:'Export'};
/* Industry label -> a colour that reads as the industry. Matched on words, so a
   label not seen yet still lands sensibly ("Pharmaceuticals & Biotechnology",
   "Cement & Cement Products"); the first match wins, so specific rules come
   before general ones (heavy electrical before electrical, other industrial
   before industrial). Anything unmatched gets a stable colour from its name. */
const INDUSTRY_COLORS=[
  [/pharma|drug|biotech|formulation/i,               '#16a34a'], // medicine green
  [/hospital|healthcare|diagnostic|medical|health/i, '#e11d48'], // red cross
  [/cement|construction material|ceramic|sanitary/i, '#a8a29e'], // concrete
  [/construction|\bepc\b/i,                          '#f97316'], // safety orange
  [/real estate|realty|residential/i,                '#b45309'], // brick
  [/heavy electrical/i,                              '#ca8a04'], // transformer gold
  [/cable|\bwires?\b/i,                              '#c2410c'], // copper
  [/consumer electronic|electronics/i,               '#f472b6'], // gadget pink
  [/electrical|\belectric\b/i,                       '#facc15'], // electric yellow
  [/power|solar|renewable|energy|utilit/i,           '#f59e0b'], // sun amber
  [/\brail|wagon|locomotive|\bmetro\b/i,             '#9f1239'], // railway maroon
  [/\bports?\b|shipping|marine|shipyard/i,           '#075985'], // sea blue
  [/logistic|transport|courier|freight|warehous/i,   '#a07855'], // cardboard
  [/aerospace|defen[cs]e|aviation/i,                 '#4d7c0f'], // military olive
  [/rubber|\btyres?\b|\btires?\b/i,                  '#3f3f46'], // tyre black
  [/\biron\b|steel/i,                                '#4f6d8f'], // blue steel
  [/metal|mining|alumin|copper|zinc|gold|silver/i,   '#d6c7a1'], // champagne metal
  [/chemical|fertili|pesticide|agrochem|paint|pigment|\bdyes?\b/i, '#a3e635'], // lab lime
  [/it enabled|\bbpo\b|outsourc/i,                   '#06b6d4'], // data cyan
  [/computers|consult/i,                             '#4338ca'], // deep indigo
  [/software|information technology|internet|\bit\b|digital/i, '#3b82f6'], // code blue
  [/telecom.*infra|\btowers?\b/i,                    '#d946ef'], // tower magenta
  [/telecom|communication|network/i,                 '#9333ea'], // signal purple
  [/furniture|furnishing|\bwood|plywood/i,           '#6f4a2c'], // wood
  [/textile|apparel|garment|fabric|yarn/i,           '#db2777'], // fabric rose
  [/bank|financ|insurance|broking|capital market/i,  '#047857'], // money green
  [/other industrial/i,                              '#9aa3ad'], // light machine grey
  [/industrial|machinery|equipment|capital goods|engineering/i, '#7a8290'], // machine grey
  [/services/i,                                      '#0d9488'], // business teal
];
const hashColor=s=>{let h=0;for(const ch of s)h=(h*31+ch.charCodeAt(0))>>>0;
  return `hsl(${h%360} 45% 48%)`;};
const industryColor=s=>s?((INDUSTRY_COLORS.find(([rx])=>rx.test(s))||[])[1]||hashColor(s)):'';
const noval=r=>`<span class="nd" title="${esc(r.extraction_notes||'no value extracted')}">Not disclosed</span>`;
const dots=a=>a.filter(Boolean).join(' · ');
const exch=r=>(r.exchanges||[r.exchange]).filter(Boolean);
const CHEV='<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const DOC='<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M4 1.75h5.2L12.5 5v8.25a1 1 0 0 1-1 1h-8.5a1 1 0 0 1-1-1V2.75a1 1 0 0 1 1-1Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M9 2v3.2h3.3M5.5 8.5h5M5.5 11h3.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
const DOC_SM=DOC.replace('width="15" height="15"','width="13" height="13"');
const pdfHref=id=>'/api/pdf/'+encodeURIComponent(id);
const EXT='<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M9 3h4v4M13 3 7.5 8.5M11 9.5V13H3V5h3.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
// screener.in keys a company by its NSE symbol, or by BSE code for BSE-only listings
const screenerHref=c=>{const k=c.nse_symbol||c.bse_code;
  return k?'https://www.screener.in/company/'+encodeURIComponent(k)+'/':'';};
// one link per exchange filing of an order, each opening that filing's own PDF
const filingLinks=o=>(o.filings||[{exchange:o.exchange,order_id:o.order_id}])
  .filter(f=>f.order_id)
  .map(f=>`<a class="pdfchip" href="${pdfHref(f.order_id)}" target="_blank" rel="noopener"
     title="Open the ${esc(f.exchange||'')} filing PDF">${DOC_SM}${esc(f.exchange||'PDF')}</a>`)
  .join('');

async function load(){
  /* The exchange, period and threshold controls were removed. Rather than
     leave their old defaults applied invisibly -- a filter with no control is
     a trap: rows vanish with nothing on screen explaining why -- the request
     is deliberately unconstrained: all time, both exchanges, no minimums. */
  // tier is applied in the browser (applyTier), so the tier tiles can count every tier
  const p=new URLSearchParams({q:q.value,tier:'',exchange:'',
    days:3650,min_score:0,min_ratio:0,
    include_amendments:amd.checked,has_value:hasval.checked});
  $('card').classList.add('busy');   // hold the old rows, dimmed, while fetching
  try{
    allRows=await (await fetch('/api/orders?'+p)).json();
    applyTier();
  }finally{ $('card').classList.remove('busy'); }
}

/* Tier filter. The table shows one tier; the tier tiles keep counting all of
   them, so picking High never turns the Medium tile into a misleading 0. */
function applyTier(){
  rows=tierV?allRows.filter(r=>r.tier===tierV):allRows.slice();
  render();
}
function setTier(v,{toggle=false}={}){
  tierV=toggle&&v&&v===tierV?'':v;      // clicking the active tier tile clears it
  $('tier').querySelectorAll('button').forEach(o=>
    o.setAttribute('aria-pressed',String(o.dataset.v===tierV)));
  document.querySelectorAll('.kpi[data-tier]').forEach(k=>{
    if(k.dataset.tier) k.setAttribute('aria-pressed',String(k.dataset.tier===tierV));});
  $('kpis').classList.toggle('filtered',!!tierV);
  applyTier();
}

/* count and value tiles describe the rows the table is showing; the tier
   tiles count every tier matching the other filters */
function kpis(){
  const n=rows.length, cos=new Set(rows.map(r=>r.company_id)).size;
  const tierCount=t=>allRows.filter(r=>r.tier===t).length;
  const valued=rows.filter(r=>r.order_value_inr!=null);
  const tot=valued.reduce((s,r)=>s+r.order_value_inr,0);
  $('k-n').textContent=n.toLocaleString('en-IN');
  $('k-n-sub').textContent=`from ${cos} compan${cos==1?'y':'ies'}`
    +(tierV?` · ${cap(tierV)} tier only`:'');
  $('k-high').textContent=tierCount('HIGH');
  $('k-med').textContent=tierCount('MEDIUM');
  $('k-val').textContent=valued.length?big(tot):'—';
  $('k-val-sub').textContent=`across ${valued.length} order${valued.length==1?'':'s'} with a stated value`;
}

function render(){
  rows.sort((a,b)=>{const x=a[sortK],y=b[sortK];
    if(x==null)return 1; if(y==null)return -1;
    return (x>y?1:x<y?-1:0)*sortD;});
  kpis();
  tb.innerHTML=rows.map(r=>{
    let bd={};try{bd=JSON.parse(r.score_breakdown||'{}')}catch(e){}
    const comps=Object.entries(bd.components||{}), pens=Object.entries(bd.penalties||{});
    const why=[`score ${(r.score??0).toFixed(1)} · ${cap(r.tier)}`,
      r.data_quality?`data quality: ${r.data_quality}`:'',
      ...(comps.length?['',...comps.map(([k,v])=>
        `${k.replace(/_/g,' ')}  ${(+v.points).toFixed(1)}/${v.max}`)]:[]),
      ...(pens.length?['','penalties: '+pens.map(([k,v])=>k+' ×'+v).join(', ')]:[]),
      ...(bd.guidance&&bd.guidance.summary?['','guidance '+bd.guidance.summary]:[])
      ].join('\n');
    const [d1,d2]=dshort(r.filed_at);
    const exs=exch(r).map(x=>`<span class="ex">${esc(x)}</span>`).join('');
    // label: the finest classification (Pharmaceuticals); colour: its broad sector (Healthcare)
    const sector=r.industry||r.sector||'';
    const sectorTip=[...new Set([r.sector,r.industry].filter(Boolean))].join(' · ');
    const q=r.ratio_to_quarter;
    const ratio=q==null?'<span class="muted">—</span>'
      :`<span class="val">${pct(q)}</span><span class="rbar${q>1?' over':''}"><i style="width:${Math.max(4,Math.min(100,q*100)).toFixed(0)}%"></i></span>`;
    const ratioTitle=q==null?'':`${pct(q)} of quarterly revenue`
      +(r.annualised_ratio==null?'':` · ${pct(r.annualised_ratio)} annualised`);
    const ct=CTYPE[r.customer_type]||'';
    return `<tr class="row t-${esc(r.tier)}" data-oid="${esc(r.order_id)}" data-cid="${esc(r.company_id)}">
      <td class="date" title="${esc((r.filed_at||'').slice(0,16).replace('T',' '))}">
        <span class="d1">${d1}</span><span class="d2">${d2}</span></td>
      <td><div class="cowrap">
        <button class="cotoggle" type="button" aria-expanded="false"
            aria-controls="det-${esc(r.order_id)}"
            title="show every order on record for this company">
          <span class="chev">${CHEV}</span>
          <span class="cotext"><span class="coline"><span class="co">${esc(r.company_name)}</span>${sector?`<span class="sector" style="--sc:${industryColor(sector)}" title="${esc(sectorTip)}">${esc(sector)}</span>`:''}${exs?`<span class="exs">${exs}</span>`:''}</span></span></button>
        ${r.pdf_url?`<a class="pdf" href="${pdfHref(r.order_id)}" target="_blank" rel="noopener"
            title="Open the order PDF" aria-label="Open the order PDF">${DOC}</a>`:''}
      </div></td>
      <td class="num">${(r.order_value_inr==null?noval(r)
          :`<span class="val">${inr(r.order_value_inr)}</span>`
           +(r.value_is_range?'<span class="minor">approx.</span>':''))
          +(bd.guidance&&bd.guidance.summary?`<span class="minor guide" title="${esc(bd.guidance.summary)}">+ ${esc(bd.guidance.fy)} guidance</span>`:'')}</td>
      <td class="num" title="${esc(ratioTitle)}">${ratio}</td>
      <td class="num">${mon(r.execution_months)}</td>
      <td><span class="clip cname${r.customer?'':' muted'}">${esc(r.customer||'—')}</span>
        ${ct?`<span class="ctype">${ct}</span>`:''}</td>
      <td title="${esc(why)}"><div class="sig">
        <span class="ring" style="--p:${Math.min(100,Math.max(0,r.score||0)).toFixed(0)}"
          ><b>${(r.score??0).toFixed(0)}</b></span>
        <span class="pill"><i class="dot"></i>${cap(r.tier)}</span></div></td>
    </tr>
    <tr class="det" id="det-${esc(r.order_id)}" hidden><td colspan="7">
      <div class="stage"><div class="detbox"></div></div></td></tr>`}).join('');
  empty.hidden=rows.length>0;
}

/* ---- company drill-down: every order on record, duplicates collapsed ---- */
const coCache={};
function renderCompany(d,oid){
  const c=(d.company||[])[0]||{}, f=(d.fundamentals||[])[0]||{}, os=d.orders||[];
  const extra=(d.n_filings||os.length)-os.length;
  const ids=dots([c.nse_symbol?'NSE:'+esc(c.nse_symbol):'',
                  c.bse_code?'BSE:'+esc(c.bse_code):'', esc(c.isin||'')]);
  const tot=os.reduce((s,o)=>s+(o.order_value_inr||0),0);
  const fact=(k,v)=>`<div class="fact"><span>${k}</span><b>${v}</b></div>`;
  const sec=c.industry||c.sector;
  const secTip=[...new Set([c.sector,c.industry].filter(Boolean))].join(' · ');
  const scr=screenerHref(c);
  const head=`<div class="dethead"><div><div class="dtitle">${esc(c.name||'')}${sec?`<span class="sector" style="--sc:${industryColor(sec)}" title="${esc(secTip)}">${esc(sec)}</span>`:''}${scr?`<a class="extlink" href="${scr}" target="_blank" rel="noopener" title="Open ${esc(c.name||'')} on screener.in">screener.in ${EXT}</a>`:''}</div>`
    +(ids?`<div class="dids">${ids}</div>`:'')+`</div><div class="facts">`
    +fact('Quarterly revenue',inr(f.q_revenue_inr))
    +fact('TTM revenue',inr(f.ttm_revenue_inr))
    +fact('Orders on record',os.length)
    +fact('Disclosed value',tot?big(tot):'—')+`</div></div>`;
  const note=dots([f.source?'revenue via '+esc(f.source):'no revenue data',
    extra>0?`${extra} duplicate cross-exchange filing${extra==1?'':'s'} merged`:'']);
  if(!os.length) return head+`<p class="detnote">${note} · No orders on record.</p>`;
  const body=os.map(o=>{const [d1,d2]=dshort(o.filed_at);
    return `<tr class="t-${esc(o.tier)}${o.order_id===oid?' cur':''}">
    <td class="nowrap">${d1} ${d2}</td>
    <td class="num">${o.order_value_inr==null?noval(o):inr(o.order_value_inr)}</td>
    <td class="num">${pct(o.ratio_to_quarter)}</td>
    <td class="num">${pct(o.annualised_ratio)}</td>
    <td class="num">${mon(o.execution_months)}</td>
    <td><span class="clip">${esc(o.customer||'—')}</span></td>
    <td class="nowrap">${esc(otype(o.order_type)||'—')}</td>
    <td><span class="pill sm"><i class="dot"></i>${cap(o.tier)} · ${(o.score??0).toFixed(0)}</span></td>
    <td class="nowrap muted">${esc(cap(o.data_quality)||'—')}</td>
    <td class="nowrap">${filingLinks(o)}${o.is_amendment?'<span class="muted"> · amended</span>':''}</td>
    </tr>`}).join('');
  const cur=os.find(o=>o.order_id===oid||(o.filings||[]).some(f=>f.order_id===oid));
  let g=null;try{g=JSON.parse(cur.score_breakdown||'{}').guidance}catch(e){}
  const guide=g&&g.summary?`<p class="guidance"><b>Management guidance</b>${esc(g.summary)}</p>`:'';
  return head+`<p class="detnote">${note}</p>`+guide
    +`<table class="inner"><thead><tr><th>Filed</th>
    <th class="num">Order</th><th class="num">vs Q revenue</th><th class="num">Annualised</th>
    <th class="num">Exec</th><th>Customer</th><th>Type</th><th>Signal</th><th>Data</th>
    <th>Filing PDF</th></tr></thead><tbody>${body}</tbody></table>`;
}

async function toggleCompany(btn){
  const tr=btn.closest('tr'), oid=tr.dataset.oid, cid=tr.dataset.cid;
  const det=document.getElementById('det-'+oid); if(!det) return;
  const opening=det.hidden;
  det.hidden=!opening;
  tr.classList.toggle('open',opening);
  btn.setAttribute('aria-expanded',String(opening));
  if(!opening||det.dataset.loaded) return;
  det.dataset.loaded='1';
  const box=det.querySelector('.detbox');
  box.innerHTML='<p class="detnote" style="margin:0">Loading…</p>';
  try{
    if(!coCache[cid]) coCache[cid]=await (
      await fetch('/api/company/'+encodeURIComponent(cid))).json();
    box.innerHTML=renderCompany(coCache[cid],oid);
  }catch(e){
    det.dataset.loaded='';
    box.innerHTML='<p class="detnote" style="margin:0">Could not load this company’s history.</p>';
  }
}
tb.addEventListener('click',e=>{
  const b=e.target.closest('.cotoggle');
  if(b) toggleCompany(b);
});

document.querySelectorAll('thead th[data-k]').forEach(th=>th.onclick=()=>{
  const k=th.dataset.k; sortD=(sortK===k)?-sortD:-1; sortK=k;
  document.querySelectorAll('thead th[data-k]').forEach(o=>o.removeAttribute('aria-sort'));
  th.setAttribute('aria-sort',sortD<0?'descending':'ascending');
  render();});

const tierG=$('tier');
tierG.addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b) return;
  setTier(b.dataset.v);
});
document.querySelectorAll('.kpi[data-tier]').forEach(k=>{
  const go=()=>setTier(k.dataset.tier,{toggle:true});
  k.addEventListener('click',go);
  k.addEventListener('keydown',e=>{
    if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}});
});
['q','amd','hasval'].forEach(id=>{
  const el=$(id);
  el.addEventListener(el.type==='checkbox'?'change':'input',
    ()=>{clearTimeout(window._t);window._t=setTimeout(load,250)});});

fetch('/api/stats').then(r=>r.json()).then(s=>{
  const last=(s.runs||[])[0];
  since.textContent=last
    ? 'Last scan '+(last.finished_at||'').slice(0,16).replace('T',' ')
    : 'No scans yet';
  setBarH();
}).catch(()=>{});

load();
</script>
</body></html>
"""


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn
    init_db()
    print(f"Order Scanner dashboard -> http://{host}:{port}  (db: {DB_PATH})")
    uvicorn.run(app, host=host, port=port, log_level="warning")
