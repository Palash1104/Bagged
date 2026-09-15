"""LLM fallback for announcements the regex pass couldn't resolve.

Results are cached in SQLite keyed on a hash of the input text, so a re-run
over the same window costs nothing.
"""
from __future__ import annotations

import hashlib
import json
import re

from ..config import CONFIG
from ..db import cache_llm, get_llm_cache
from .rules import ExtractedOrder
from .summary import MAX_SUMMARY, summarize

SYSTEM = """You extract structured order/contract data from Indian stock \
exchange (NSE/BSE) corporate announcements.

Return ONLY a JSON object, no prose, with these keys:
- is_order (bool): true only if the company has RECEIVED/WON a new order, \
contract, LOA, LOI or work order. False for results, board meetings, \
investor presentations, general updates.
- order_value_inr (number|null): total order value converted to INR. \
Convert crore (1e7) and lakh (1e5) yourself. If a range, use the midpoint. \
Use null if no value is stated. Do NOT use EMD, bank guarantee, GST, \
share capital or turnover figures.
- value_currency (string): the currency as stated ("INR", "USD", "EUR"...).
- value_is_range (bool)
- customer (string|null): the entity placing the order, verbatim.
- customer_type (string): one of government, psu, private, export, unknown.
- execution_months (number|null): execution/completion period in months.
- order_type (string): one of loa, loi, work_order, mou, l1, amendment, unknown. \
Use "amendment" for cancellations, revisions, short-closures or corrigenda.
- is_amendment (bool)
- is_related_party (bool): true if the customer is a subsidiary, holding, \
group or promoter-group entity of the announcing company.
- scope (string|null): one short line on what the order is for.
- summary (string|null): one plain sentence an investor can read at a glance: the value, who placed the order, what it is for and the execution period. Only facts stated in the text. At most 150 characters; drop legal suffixes such as Private Limited.
- confidence (number): 0-1, your confidence in order_value_inr.
- notes (string): anything ambiguous a human should check.
"""

USER_TMPL = """Company: {company}
Exchange: {exchange}
Headline: {headline}

Announcement text:
\"\"\"
{body}
\"\"\"
"""


def _key(company: str, headline: str, body: str, model: str) -> str:
    h = hashlib.sha1()
    h.update(f"{model}|{company}|{headline}|{body[:8000]}".encode("utf-8", "ignore"))
    return h.hexdigest()


def _parse_json(raw: str) -> dict | None:
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _call_anthropic(prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=CONFIG.llm_api_key)
    msg = client.messages.create(
        model=CONFIG.llm_model,
        max_tokens=1200,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _call_openai(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=CONFIG.llm_api_key)
    resp = client.chat.completions.create(
        model=CONFIG.llm_model,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=1200,
    )
    return resp.choices[0].message.content or ""


def extract_with_llm(
    conn, company: str, exchange: str, headline: str, body: str,
) -> ExtractedOrder | None:
    """Ask the model. Returns None if the call fails or LLM is disabled."""
    if not CONFIG.llm_fallback or not CONFIG.llm_api_key:
        return None

    body = (body or "")[:12000]
    key = _key(company, headline or "", body, CONFIG.llm_model)

    data = get_llm_cache(conn, key)
    if data is None:
        prompt = USER_TMPL.format(
            company=company, exchange=exchange,
            headline=headline or "", body=body or "(no body text)")
        try:
            raw = (_call_anthropic(prompt) if CONFIG.llm_provider == "anthropic"
                   else _call_openai(prompt))
        except Exception as exc:  # network, auth, rate limit
            return _failed(f"llm call failed: {exc}")
        data = _parse_json(raw)
        if data is None:
            return _failed("llm returned unparseable output")
        cache_llm(conn, key, data, CONFIG.llm_model)

    return _to_order(data)


def _failed(note: str) -> ExtractedOrder:
    o = ExtractedOrder(method="llm")
    o.notes.append(note)
    return o


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_order(d: dict) -> ExtractedOrder:
    o = ExtractedOrder(method="llm")
    o.is_order = bool(d.get("is_order"))
    o.order_value_inr = _num(d.get("order_value_inr"))
    o.value_currency = (d.get("value_currency") or "INR").upper()
    o.value_is_range = bool(d.get("value_is_range"))
    o.customer = d.get("customer")
    o.customer_type = (d.get("customer_type") or "unknown").lower()
    o.execution_months = _num(d.get("execution_months"))
    o.order_type = (d.get("order_type") or "unknown").lower()
    o.is_amendment = bool(d.get("is_amendment")) or o.order_type == "amendment"
    o.is_related_party = bool(d.get("is_related_party"))
    o.scope = d.get("scope")
    o.summary = d.get("summary")
    o.confidence = _num(d.get("confidence")) or 0.6

    firmness_map = {"loa": 1.0, "work_order": 1.0, "loi": 0.6,
                    "l1": 0.4, "mou": 0.3, "amendment": 0.0, "unknown": 0.5}
    o.firmness = firmness_map.get(o.order_type, 0.5)

    # The model reports the currency it saw; convert if it didn't.
    if o.order_value_inr and o.value_currency != "INR":
        rate = CONFIG.fx_rates.get(o.value_currency)
        if rate and o.order_value_inr < 1e9:  # looks unconverted
            o.order_value_inr *= rate
            o.notes.append(f"converted {o.value_currency} at {rate}")

    if d.get("notes"):
        o.notes.append(str(d["notes"])[:400])
    return o


def merge(rule: ExtractedOrder, llm: ExtractedOrder | None) -> ExtractedOrder:
    """Prefer the LLM where the rules were weak, keep the rules where strong."""
    if llm is None or not llm.is_order:
        return rule
    out = rule
    out.method = "regex+llm"

    if rule.order_value_inr is None and llm.order_value_inr:
        out.order_value_inr = llm.order_value_inr
        out.value_currency = llm.value_currency
        out.confidence = max(rule.confidence, llm.confidence)
        out.notes.append("value from LLM")
    elif rule.order_value_inr and llm.order_value_inr:
        diff = abs(rule.order_value_inr - llm.order_value_inr)
        rel = diff / max(rule.order_value_inr, llm.order_value_inr)
        if rel > 0.02:
            # Disagreement: trust the LLM but flag it loudly.
            out.notes.append(
                f"value disagreement rules={rule.order_value_inr:.0f} "
                f"llm={llm.order_value_inr:.0f}; used LLM")
            out.order_value_inr = llm.order_value_inr
            out.confidence = min(rule.confidence, llm.confidence) * 0.85
        else:
            out.confidence = min(0.97, max(rule.confidence, llm.confidence) + 0.1)
            out.notes.append("rules and LLM agree on value")

    out.customer = rule.customer or llm.customer
    if llm.customer_type != "unknown":
        out.customer_type = llm.customer_type
    out.execution_months = rule.execution_months or llm.execution_months
    if rule.order_type == "unknown" and llm.order_type != "unknown":
        out.order_type, out.firmness = llm.order_type, llm.firmness
    out.is_amendment = rule.is_amendment or llm.is_amendment
    out.is_related_party = rule.is_related_party or llm.is_related_party
    out.scope = rule.scope or llm.scope
    # The model's own sentence when it wrote one that fits the two-line budget;
    # otherwise rebuild the rule summary, since the value, customer or type
    # above may have just changed.
    fits = bool(llm.summary) and len(llm.summary) <= MAX_SUMMARY
    out.summary = llm.summary if fits else summarize(
        scope=out.scope, customer=out.customer, order_type=out.order_type,
        value_inr=out.order_value_inr, value_is_range=out.value_is_range,
        value_low_inr=out.value_low_inr, value_high_inr=out.value_high_inr,
        execution_months=out.execution_months)
    out.notes.extend(n for n in llm.notes if n)
    return out
