"""Telegram alerting."""
from __future__ import annotations

import html
import logging

import requests

from .config import CONFIG
from .extract.money import fmt_inr

log = logging.getLogger(__name__)

TIER_ICON = {"HIGH": "\U0001F534", "MEDIUM": "\U0001F7E1",
             "WATCH": "\U0001F535", "STORE": "⚪"}


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def format_message(order: dict, score_res, fundamentals: dict | None) -> str:
    icon = TIER_ICON.get(score_res.tier, "")
    name = _esc(order.get("company_name"))
    sym = order.get("symbol") or order.get("bse_code") or ""
    val = fmt_inr(order.get("order_value_inr"))

    lines = [
        f"{icon} <b>{name}</b> {f'({_esc(sym)})' if sym else ''}",
        f"<b>Order:</b> {_esc(val)}"
        + (" <i>(range)</i>" if order.get("value_is_range") else ""),
    ]

    if order.get("customer"):
        ctype = order.get("customer_type", "")
        lines.append(f"<b>From:</b> {_esc(order['customer'])}"
                     + (f" <i>({_esc(ctype)})</i>" if ctype != "unknown" else ""))
    if order.get("execution_months"):
        m = order["execution_months"]
        lines.append(f"<b>Execution:</b> {m:.0f} months")
    about = order.get("summary") or order.get("scope")
    if about:
        lines.append(f"<b>Order:</b> {_esc(about[:300])}")
    guidance = getattr(score_res, "guidance", None) or {}
    if guidance.get("summary"):
        lines.append(f"<b>Guidance:</b> {_esc(guidance['summary'])}")

    lines.append("")
    r_q = score_res.ratio_to_quarter
    r_t = score_res.ratio_to_ttm
    r_a = score_res.annualised_ratio
    if r_q is not None:
        lines.append(f"<b>vs last quarter revenue:</b> {r_q:.0%}")
    if r_t is not None:
        lines.append(f"<b>vs TTM revenue:</b> {r_t:.0%}")
    if r_a is not None:
        lines.append(f"<b>annualised revenue impact:</b> {r_a:.0%}")
    if fundamentals:
        lines.append(
            f"<i>Q rev {fmt_inr(fundamentals.get('q_revenue_inr'))} · "
            f"TTM {fmt_inr(fundamentals.get('ttm_revenue_inr'))} · "
            f"src {_esc(fundamentals.get('source'))}</i>")

    lines.append("")
    lines.append(f"<b>Score: {score_res.score:.0f}/100</b> ({score_res.tier})")
    top = sorted(score_res.components.items(),
                 key=lambda kv: kv[1].get("points", 0), reverse=True)[:3]
    lines.append("  " + " · ".join(
        f"{k.replace('_', ' ')} {v['points']:.0f}/{v['max']:.0f}" for k, v in top))

    if score_res.penalties:
        lines.append("  ⚠ penalties: " + ", ".join(
            f"{k} ×{v}" for k, v in score_res.penalties.items()))
    if score_res.data_quality != "good":
        lines.append(f"  ⚠ data quality: {score_res.data_quality}")
    if order.get("extraction_notes"):
        lines.append(f"  <i>{_esc(str(order['extraction_notes'])[:200])}</i>")

    lines.append("")
    meta = [order.get("exchange"), order.get("order_type"),
            str(order.get("filed_at") or "")[:16]]
    lines.append("<i>" + " · ".join(_esc(m) for m in meta if m) + "</i>")
    if order.get("pdf_url"):
        lines.append(f'<a href="{_esc(order["pdf_url"])}">Filing PDF</a>')

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
    return (err is None), err


def send_digest(rows: list[dict]) -> tuple[bool, str | None]:
    """One message summarising the WATCH-tier items from a run."""
    if not rows:
        return True, None
    lines = [f"\U0001F4CB <b>Watchlist digest</b> ({len(rows)} items)", ""]
    for r in rows[:25]:
        lines.append(
            f"• <b>{_esc(r.get('company_name'))}</b> — "
            f"{fmt_inr(r.get('order_value_inr'))} "
            f"({(r.get('ratio_to_quarter') or 0):.0%} of Q rev) — "
            f"score {r.get('score', 0):.0f}")
    return send_telegram("\n".join(lines))
