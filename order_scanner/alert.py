"""Telegram alerting.

The message is built to be read in a notification: tier colour and company
first, then the order in one line, then how big it is against revenue as bars
you can judge without reading the numbers. Everything needed to argue with the
score -- components, penalties, extraction evidence -- goes into an expandable
quote, so the alert stays a few lines until you tap it.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime

import requests

from .config import CONFIG

log = logging.getLogger(__name__)

# Tier colour: the first thing the eye lands on in a chat list.
TIER_ICON = {"HIGH": "🔴", "MEDIUM": "🟠", "WATCH": "🔵", "STORE": "⚪"}
BAR_FILLED, BAR_EMPTY, BAR_WIDTH = "▰", "▱", 10

_OTYPE = {"work_order": "Work order", "loa": "LoA", "loi": "LoI",
          "mou": "MoU", "l1": "L1 bidder"}
_CTYPE = {"government": "Govt", "psu": "PSU", "private": "Private",
          "export": "Export"}
_PENALTY = {"stale": "filed over 48h ago", "related_party": "related-party customer",
            "implausible_ratio": "size looks implausible",
            "pump": "order far above market cap",
            "sme_pump": "SME order far above market cap"}
_QUALITY = {"partial": "some fields missing", "low": "extraction unreliable"}
# "₹27.72 Cr order from X for ...", "₹4,928 Cr Letter of Award for ..." -- the
# value already has its own line above, so only the scope is worth repeating
_SUMMARY_PREFIX = re.compile(
    r"^₹[\d.,]+\s*(?:Cr|L|lakhs?|crores?)\s+"
    r"(?:order|contract|work\s+order|letter\s+of\s+(?:award|intent)|loa|loi|mou)\s+",
    re.I)
# the extractor's confidence arithmetic: "+0.12 o&m", "-0.05 estimated"
_NOTE_DELTA = re.compile(r"^[+-]\d")
_EVIDENCE_PREFIX = re.compile(r"^order evidence:\s*", re.I)


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def money(value) -> str:
    """₹985 Cr, ₹10.07 L -- trailing zeros trimmed."""
    if value is None:
        return "not disclosed"
    if value >= 1e7:
        n, unit = value / 1e7, " Cr"
    elif value >= 1e5:
        n, unit = value / 1e5, " L"
    else:
        return f"₹{value:,.0f}"
    return "₹" + f"{n:,.2f}".rstrip("0").rstrip(".") + unit


def bar(ratio: float | None) -> str:
    """A ten-cell bar; anything at or above 100% fills it."""
    filled = min(BAR_WIDTH, int((ratio or 0) * BAR_WIDTH + 0.5))
    return BAR_FILLED * filled + BAR_EMPTY * (BAR_WIDTH - filled)


def _value(order: dict) -> str:
    lo, hi = order.get("value_low_inr"), order.get("value_high_inr")
    if order.get("value_is_range") and lo and hi:
        return f"{money(lo)} – {money(hi)}"
    return money(order.get("order_value_inr"))


def _period(months) -> str:
    m = float(months)
    if m >= 24 and abs(m % 12) < 0.01:
        return f"{m / 12:.0f} years"
    return f"{m:.0f} month" + ("" if round(m) == 1 else "s")


def _screener_url(order: dict, company: dict) -> str:
    key = (company.get("nse_symbol") or order.get("symbol")
           or company.get("bse_code") or order.get("bse_code"))
    return f"https://www.screener.in/company/{key}/" if key else ""


def _when(filed_at) -> str:
    try:
        return datetime.fromisoformat(str(filed_at)).strftime("%d %b, %H:%M")
    except (TypeError, ValueError):
        return str(filed_at or "")[:16].replace("T", " ")


def _evidence(notes) -> list[str]:
    """The readable half of the extraction notes, without the score deltas."""
    parts = [p.strip() for p in str(notes or "").split(";")]
    parts = [_EVIDENCE_PREFIX.sub("", p) for p in parts
             if p and not _NOTE_DELTA.match(p)]
    return [_clip(p, 110) for p in parts[:2]]


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def format_message(order: dict, score_res, fundamentals: dict | None,
                   company: dict | None = None) -> str:
    company = company or {}
    icon = TIER_ICON.get(score_res.tier, "")
    lines = [f"{icon} <b>{_esc(order.get('company_name'))}</b>"]

    sub = [order.get("symbol") or company.get("nse_symbol") or order.get("bse_code"),
           company.get("industry") or company.get("sector"),
           order.get("exchange")]
    sub = " · ".join(_esc(s) for s in sub if s)
    if sub:
        lines.append(f"<i>{sub}</i>")

    lines.append("")
    otype = _OTYPE.get(order.get("order_type") or "")
    lines.append(f"💰 <b>{_value(order)}</b>" + (f" · {otype}" if otype else ""))

    about = _SUMMARY_PREFIX.sub(
        "", (order.get("summary") or order.get("scope") or "")).strip()
    if about:
        about = about[0].upper() + about[1:]
        lines.append(f"🧾 {_esc(_clip(about, 200))}")
    if order.get("customer"):
        ctype = _CTYPE.get(order.get("customer_type") or "")
        lines.append(f"🏢 {_esc(order['customer'])}"
                     + (f" <i>({ctype})</i>" if ctype else ""))
    if order.get("execution_months"):
        lines.append(f"⏳ {_period(order['execution_months'])}")
    guidance = (getattr(score_res, "guidance", None) or {}).get("summary")
    if guidance:
        lines.append(f"📈 <b>Guidance</b> · {_esc(guidance)}")

    # Size against revenue, as bars: the whole point of the alert.
    ratios = [(score_res.ratio_to_quarter, "of quarterly revenue", True),
              (score_res.ratio_to_ttm, "of TTM revenue", False),
              (score_res.annualised_ratio, "annualised impact", False)]
    ratios = [(r, label, strong) for r, label, strong in ratios if r is not None]
    if ratios:
        lines.append("")
        for r, label, strong in ratios:
            pct = f"{r:.0%}"
            lines.append(f"{bar(r)} " + (f"<b>{pct}</b>" if strong else pct)
                         + f" {label}")
    if fundamentals:
        src = fundamentals.get("source")
        lines.append(f"<i>Q {money(fundamentals.get('q_revenue_inr'))} · "
                     f"TTM {money(fundamentals.get('ttm_revenue_inr'))}"
                     + (f" · {_esc(src)}" if src else "") + "</i>")

    lines.append("")
    lines.append(f"{icon} <b>{score_res.tier.title()}</b> · "
                 f"score {score_res.score:.0f}/100")

    detail = []
    top = [(k, v) for k, v in sorted(score_res.components.items(),
                                     key=lambda kv: kv[1].get("points", 0),
                                     reverse=True)[:3] if v.get("points")]
    if top:
        detail.append(" · ".join(
            f"{k.replace('_', ' ')} {v['points']:.0f}/{v['max']:.0f}"
            for k, v in top))
    for k, v in (score_res.penalties or {}).items():
        detail.append(f"⚠ {_PENALTY.get(k, k.replace('_', ' '))} ×{v}")
    if score_res.data_quality != "good":
        detail.append("⚠ " + _QUALITY.get(score_res.data_quality,
                                          score_res.data_quality))
    shown = 0
    for reason in getattr(score_res, "reasons", None) or []:
        # the guidance line above already said this one
        if guidance and reason.startswith("guidance carries the score"):
            continue
        detail.append(f"• {_esc(_clip(reason, 110))}")
        shown += 1
        if shown == 3:
            break
    for note in _evidence(order.get("extraction_notes")):
        detail.append(f"🔎 {_esc(note)}")
    if detail:
        lines.append("<blockquote expandable>" + "\n".join(detail) + "</blockquote>")

    foot = []
    if order.get("pdf_url"):
        foot.append(f'📄 <a href="{_esc(order["pdf_url"])}">Filing PDF</a>')
    url = _screener_url(order, company)
    if url:
        foot.append(f'📊 <a href="{_esc(url)}">screener.in</a>')
    foot.append(f"<i>{_esc(_when(order.get('filed_at')))}</i>")
    lines += ["", " · ".join(foot)]
    return "\n".join(lines)


def _api(method: str, token: str | None = None,
         **params) -> tuple[object | None, str | None]:
    """Call one Bot API method.  Returns (result, error)."""
    token = token or CONFIG.telegram_token
    if not token:
        return None, "no bot token set (TELEGRAM_BOT_TOKEN)"
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, timeout=15, json=params)
        data = r.json()
    except requests.RequestException as exc:
        return None, str(exc)
    except ValueError:
        return None, f"non-JSON reply from Telegram ({r.status_code})"
    if not data.get("ok"):
        return None, f"{data.get('error_code')}: {data.get('description')}"
    return data.get("result"), None


def verify_token(token: str | None = None) -> tuple[dict | None, str | None]:
    """getMe — confirms the bot token is live.  Returns (bot_info, error)."""
    return _api("getMe", token=token)


def discover_chat_ids(token: str | None = None
                      ) -> tuple[list[tuple[str, str]], str | None]:
    """Read recent updates and pull out every chat the bot can see.

    The bot only receives updates after someone talks to it, so DM it /start,
    or add it to the group/channel and post once.  Returns
    ([(chat_id, human_label), ...], error).

    Note: Telegram drops updates after ~24h, and getUpdates conflicts with a
    running webhook or a second polling process.
    """
    updates, err = _api("getUpdates", token=token, limit=100, timeout=0)
    if err:
        return [], err
    seen: set = set()
    out: list[tuple[str, str]] = []
    for upd in updates or []:
        for key in ("message", "edited_message", "channel_post",
                    "edited_channel_post", "my_chat_member"):
            chat = (upd.get(key) or {}).get("chat")
            if not chat or chat.get("id") in seen:
                continue
            seen.add(chat["id"])
            label = (chat.get("title")
                     or " ".join(filter(None, [chat.get("first_name"),
                                               chat.get("last_name")]))
                     or (f"@{chat['username']}" if chat.get("username") else "")
                     or "?")
            out.append((str(chat["id"]), f"{label} [{chat.get('type')}]"))
    return out, None


def send_telegram(text: str, chat_id: str | None = None,
                  token: str | None = None) -> tuple[bool, str | None]:
    """Send one HTML message.  Falls back to the configured token/chat."""
    token = token or CONFIG.telegram_token
    chat_id = chat_id or CONFIG.telegram_chat_id
    if not token or not chat_id:
        return False, "telegram not configured"
    _, err = _api("sendMessage", token=token, chat_id=chat_id,
                  text=text[:4000], parse_mode="HTML",
                  disable_web_page_preview=True)
    if err and "parse" in err.lower() and "<blockquote expandable>" in text:
        # Collapsible quotes need a recent Bot API; fall back to a plain one.
        text = text.replace("<blockquote expandable>", "<blockquote>")
        _, err = _api("sendMessage", token=token, chat_id=chat_id,
                      text=text[:4000], parse_mode="HTML",
                      disable_web_page_preview=True)
    return (err is None), err


def send_digest(rows: list[dict]) -> tuple[bool, str | None]:
    """One message summarising the WATCH-tier items from a run."""
    if not rows:
        return True, None
    lines = [f"🔵 <b>Watchlist</b> · {len(rows)} "
             f"order{'' if len(rows) == 1 else 's'}", ""]
    for r in sorted(rows, key=lambda r: r.get("score") or 0, reverse=True)[:25]:
        q = r.get("ratio_to_quarter")
        bits = [money(r.get("order_value_inr"))]
        if q is not None:
            bits.append(f"{q:.0%} of Q rev")
        lines.append(f"<b>{_esc(r.get('company_name'))}</b> · " + " · ".join(bits)
                     + f" · <b>{(r.get('score') or 0):.0f}</b>")
    return send_telegram("\n".join(lines))
