"""A readable one-sentence description of an order.

The dashboard used to show `scope`, which the old rule took as "whatever
follows the first 'for' anywhere in the text". On real filings that produced
"the purpose of manufacture,", "Fair Disclosure of Unpublished Price
Sensitive Information," and "your information and records".

Filings do say what the order is for, in one of three places:

  * the covering sentence   "has received an order from X for Y"
  * the Reg-30 table        "Nature of order(s)/contract(s)" and
                            "Significant terms and conditions ... in brief",
                            whose label words end up interleaved with the
                            value once the PDF table is flattened to text
  * a press-release title   'titled "Vikram Solar Secures 124 MW ..."'

This module collects candidates from all three, drops boilerplate, keeps the
best one, and composes it with the already-extracted value, customer and
execution period into a sentence.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- cleanup
# PDF text extraction emits some ligatures as (cid:N). These cover the glyphs
# seen in filings: "Erec(cid:415)on", "Pla(cid:414)orm", "Intermi(cid:425)ent".
_CID_GLYPHS = {"414": "tf", "415": "ti", "425": "tt"}
_CID = re.compile(r"\(cid:(\d+)\)")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = _CID.sub(lambda m: _CID_GLYPHS.get(m.group(1), ""), text)
    text = text.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    # BSE's text feed drops apostrophes: "Raymond Limited s Aerospace Subsidiary"
    text = re.sub(r"\b(Limited|Ltd|Company|India)\s+s\b", r"\1's", text)
    return re.sub(r"\s+", " ", text).strip()


# "Rs. 27.82", "M/s. Argentera", "No. 4200017209" and "approx. Rs 600" all put
# a full stop mid-sentence; none of them ends one.
_ABBREV = re.compile(
    r"(?:\b(?:rs|no|nos|ltd|pvt|co|inc|approx|viz|dt|sr|mr|ms|dr|st|vs|etc)|m/s)$",
    re.IGNORECASE)
_BOUNDARY = re.compile(r"[.;!?]\s+(?=[A-Z\"(])")


def _boundaries(s: str) -> list[int]:
    """End offsets of real sentence breaks in s."""
    return [m.end() for m in _BOUNDARY.finditer(s)
            if not _ABBREV.search(s[:m.start()])]


# ---------------------------------------------------------------- patterns
_ORDER_NOUN = re.compile(
    r"\b(?:orders?|contracts?|letters? of (?:award|intent|acceptance|commencement)|"
    r"lo[ai]s?|awards?|tenders?|agreements?|mou)\b", re.IGNORECASE)

_RECEIPT = re.compile(
    r"\b(?:receiv\w*|receipt|secur\w*|bag(?:s|ged|ging)?|award\w*|won|wins?|"
    r"placed|issued|signed|emerg\w*|obtain\w*)\b", re.IGNORECASE)

# What introduces the object of the order.
_LEAD = re.compile(
    r"\b(?:for\s+the\s+purpose\s+of|pertain(?:s|ing)\s+to|relating\s+to|"
    r"in\s+respect\s+of|towards|for)\s+"
    # "the contract of supply of Control Cables"
    r"|\b(?:orders?|contracts?)\s+of\s+(?!(?:rs|inr|usd|rupees|approx)\b|[\d₹])",
    re.IGNORECASE)

# How far "for" may sit from the word "order" and still describe that order.
# "an order from X Limited having an aggregate order value in the range of
# Rs. 5-10 crores, for the purpose of" is well inside; "The pilot order
# represents the beginning of the relationship ... opportunities for
# additional programmes" (about 200 characters) is not.
_MAX_LEAD_DISTANCE = 150

# Where the object ends.
_STOP = re.compile(
    # the next clause: money, period, date, reference, manner of award, terms
    r",?\s+(?:amounting|worth|valued|aggregating|totall?ing|having|dated|vide|"
    r"which|who|reinforcing|subject\s+to|pursuant\s+to|in\s+the\s+(?:ordinary|normal)|"
    r"on\s+the\s+basis\s+of|inclusive|exclusive|excluding|including\s+(?:gst|taxes|all)|"
    r"as\s+per|at\s+a\s+(?:total|value|cost)|"
    r"for\s+a\s+(?:total|period|value|consideration)|"
    r"to\s+be\s+(?:executed|completed|delivered)|"
    r"with(?:in)?\s+(?:a\s+)?(?:period|stipulated|completion|timeline)|"
    r"with\s+(?:an?|the)\b|within\s+\d|on\s+or\s+before|from\s+the\s+date|"
    r"under\s+(?:the\s+)?(?:terms|purchase\s+order|po\b)|to\s+m/s\b|"
    r"order\s+value|contract\s+value|total\s+value|on\s+\d{1,2}(?:st|nd|rd|th)?\s)"
    r"|,\s+(?:delivery|payment)\s+(?:shall|is|will|to)\b"
    r"|\s*[(,]?\s*(?:\brs\.?|\binr|\busd|\brupees|₹)\s*[\d.,]"
    r"|\s*\((?:incl|excl)|\s*;"
    # Reg-30 table labels and field lettering bleeding into the sentence
    r"|\s*\b(?:whether|nature\s+of\s+(?:the\s+)?order|time\s+period|"
    r"broad\s+consideration|name\s+of\s+the\s+entity|significant\s+terms|"
    r"particulars|any\s+other\s+salient|in\s+brief)\b"
    r"|\s*\(\s*s\s*\)|\s+order\s*\(|\s+contract\s*\("
    r"|\s[a-h]\s*\)\s|\s[a-h]\.(?:\s|$)"
    # a signature block
    r"|\s*\b(?:yours\s+(?:faithfully|truly|sincerely)|for\s*(?:and|&)\s*on\s+behalf|"
    r"din\s*:|managing\s+director|company\s+secretary|authori[sz]ed\s+signatory)\b"
    r"|\s+www\.|\s+\S+@\S+",
    re.IGNORECASE)

# Candidates that are boilerplate, not a description of the work.
_JUNK = re.compile(
    r"\b(?:your\s+(?:kind\s+)?(?:information|reference|records?|perusal)|"
    r"information\s+and\s+records?|records?\s+purpose|"
    r"(?:quarter|half[- ]year|month|year|period)\s+ended|financial\s+year|"
    r"regulation\s*30|sebi|listing\s+obligations|disclosure|annexure|"
    r"intimation|compliance|the\s+above|the\s+following|the\s+same|"
    r"an\s+amount\s+not\s+exceeding|the\s+public|dissemination|"
    r"stock\s+exchange|the\s+exchange|unpublished\s+price|"
    # payment and delivery terms rather than the work
    r"payment|(?:is\s+to|shall|will)\s+be|"
    # financing and corporate actions that get classified as orders
    r"loans?|borrow\w*|lenders?|hypothecation|mortgage|title\s+deeds|"
    r"working\s+capital|rights\s+issue|equity\s+shares|related\s+party\s+transactions?|"
    r"regist\w*\s+office|"
    # reference numbers and signatures
    r"(?:tender|loa|agreement|ref(?:erence)?|po|order)\s+no\b|no\.\s*:|"
    r"din|managing\s+director|company\s+secretary|on\s+behalf)\b",
    re.IGNORECASE)

# Reg-30 table answers that say nothing about the work itself.
_TABLE_JUNK = re.compile(
    r"^(?:as\s+per|general|the\s+contract\s+is\s+governed|commercial|domestic|"
    r"international|not\s+applicable|n\.?a\.?$|nil|confidential|standard|usual|"
    r"the\s+company\s+(?:shall|has|will)|letter\s+of|terms\s+and\s+conditions|yes|no\b|"
    r"fixed\s+(?:cost|price)|lump\s*-?\s*sum|item\s+rate|percentage\s+rate)",
    re.IGNORECASE)

_TABLE_FIELD = re.compile(
    r"\b(?:nature\s+of\s+(?:the\s+)?order|significant\s+terms\s+and\s+conditions\s+of)\b",
    re.IGNORECASE)
_TABLE_NEXT = re.compile(
    r"\b(?:whether|time\s+period|broad\s+consideration|name\s*(?:\(\s*s\s*\))?\s+of\s+the\s+entity|"
    r"any\s+other|nature\s+of|significant\s+terms)\b|\s\d{1,2}\s*[.)]\s|\s[a-h]\s*[.)]\s",
    re.IGNORECASE)
# The label is often split around its own answer once flattened:
#   "Commissioning of ... MSDAC for order(s)/contract(s) awarded in continuous
#    track circuiting and Electronic Interlocking work brief; at New Mugma"
# so "awarded in" and "brief;" are removed as separate pieces.
_TABLE_LABEL = re.compile(
    r"\border\s*\(\s*s\s*\)\s*/?\s*|\bcontract\s*\(\s*s\s*\)\s*[;,]?|\(\s*s\s*\)\s*/?|"
    r"\bawarded\s*,?\s*(?:in\b\s*)?(?:brief\s*;?)?|\bin\s+brief\s*;?|\bbrief\s*;|"
    r"\bsignificant\s+terms\s+and\s+conditions\s+of\b|"
    r"^\s*(?:of\s+)?(?:the\s+)?(?:order|contract)s?\b",
    re.IGNORECASE)

# "Name of the entity AWARDING the order" is the customer. The same table in
# some filings asks for the entity "to which" it is awarded -- the company or
# its SPV -- which must not be read as the customer.
_ENTITY_FIELD = re.compile(r"\bname\s*(?:\(\s*s\s*\))?\s+of\s+the\s+entity\b", re.IGNORECASE)
_ENTITY_LABEL = re.compile(
    r"\bawarding\s+the\b|/\s*type\s+of\s+industry|"
    r"\border\s*(?:\(\s*s\s*\))?\s*/?|\bcontract\s*(?:\(\s*s\s*\))?\s*[;,]?|\(\s*s\s*\)\s*/?",
    re.IGNORECASE)
_ENTITY_JUNK = re.compile(
    r"^(?:not\s+disclosed|confidential|domestic|international|as\s+per|n\.?a\b|nil|"
    r"the\s+company|particulars|details)", re.IGNORECASE)

_TITLE = re.compile(r'\btitled\s+"(?P<t>[^"]{15,220})"', re.IGNORECASE)
_INFORMED = re.compile(
    r"\bhas\s+informed\s+the\s+exchange\s+(?:about|regarding)\s+(?P<t>.{15,240}?)(?:\.\s|\.?$)",
    re.IGNORECASE)
_GENERIC = re.compile(
    r"^(?:the\s+)?(?:bagging|receiving|awarding|announcement|intimation|disclosure|"
    r"press\s+release|updates?\b|general\s+updates?|outcome|submission|regulation|"
    r"reg\.?\s*30|please\s+find|with\s+reference|a\s+press\s+release|"
    r"receipt\s+of\s+(?:purchase\s+|work\s+)?orders?\s*[:.-]?\s*$)",
    re.IGNORECASE)

# Words in front of "order" that add nothing ("has been awarded a new order").
# Prepositions are here too: a modifier runs from the last of them, so
# "modules for supply order" yields "supply", not "modules for supply".
_MODIFIER_STOPWORDS = {
    "a", "an", "the", "new", "significant", "repeat", "purchase", "work",
    "prestigious", "major", "landmark", "large", "big", "its", "our", "their",
    "another", "fresh", "first", "following", "valuable", "follow-on",
    "has", "have", "had", "been", "is", "are", "was", "were", "be", "about",
    "received", "receiving", "secured", "securing", "bagged", "bagging", "won",
    "wins", "bags", "secures", "awarded", "placed", "issued", "obtained", "got",
    "signed", "company", "limited", "ltd", "and", "further",
    "of", "for", "to", "in", "with", "by", "from", "on", "at",
}

# Leading words that are ordinary nouns, so "Supply of ..." reads as
# "... for supply of ..." rather than keeping a sentence-start capital.
_COMMON_LEAD = {
    "supply", "design", "procurement", "replacement", "extension", "execution",
    "providing", "provision", "manufacture", "manufacturing", "construction",
    "installation", "commissioning", "operation", "operations", "maintenance",
    "development", "digitization", "digitisation", "refurbishment", "repair",
    "setting", "engagement", "transportation", "erection", "upgradation",
    "modernization", "modernisation", "work", "works", "services", "service",
    "comprehensive", "annual", "civil", "electrical", "mechanical", "survey",
}

# Customer candidates that are really a citation of the rules.
_CUSTOMER_JUNK = re.compile(
    r"\b(?:schedule|regulations?|para|listing|sr\.?\s*no|sebi|annexure|circular)\b",
    re.IGNORECASE)


# ---------------------------------------------------------------- candidates
def _tidy(s: str) -> str:
    s = s.replace('"', "")
    s = re.sub(r"(?:(?<=\s)|^)'|'(?=\s|$)", "", s)        # quote marks, not apostrophes
    s = s.strip(" ,;:-/'*").rstrip(".")
    # a dangling connective left by the cut: "products manufactured by"
    return re.sub(r"(?:\s+(?:of|and|or|the|for|to|in|with|an?|by|at|from|under|on|as|"
                  r"via|through|including|be|is|are|&))+$", "", s,
                  flags=re.IGNORECASE).strip(" ,;:-")


def _trim(s: str) -> str:
    """Cut a raw capture down to the object phrase."""
    s = re.sub(r"^(?:\(\s*s\s*\)|[/;:,.\-•|*\s])+", "", s)
    if s.startswith('"'):
        end = s.find('"', 1)
        if end > 0:
            s = s[1:end]                  # 'for "Digitization of ..." from X'
    cuts = [b - 2 for b in _boundaries(s)]      # before ". X"
    m = _STOP.search(s)
    if m:
        cuts.append(m.start())
    if cuts:
        s = s[:min(cuts)]
    s = _tidy(s)
    # A long phrase is cut at a clause break rather than mid-word.
    if len(s) > 220:
        head = s[:220]
        s = _tidy(head[:head.rfind(",")] if "," in head[80:] else head[:head.rfind(" ")])
    return s


_FILLER = {"purchase", "work", "supply", "new", "the", "and", "for", "this", "said"}


def _usable(s: str) -> bool:
    words = re.findall(r"[A-Za-z]{2,}", s)
    if len(words) < 2 or len(s) < 10 or _JUNK.search(s):
        return False
    # nothing but the word "order": "* Purchase Order"
    if not [w for w in re.findall(r"[A-Za-z]{3,}", _ORDER_NOUN.sub(" ", s))
            if w.lower() not in _FILLER]:
        return False
    # a period or term, not a scope: "three years for ...", "a further period of 5 years"
    if re.match(r"^(?:an?\s+|the\s+)?(?:(?:extended|initial|further)\s+)?"
                r"(?:period|term|tenure)\s+of\b", s, re.IGNORECASE):
        return False
    if re.match(r"^(?:a\s+period\s+of\s+)?\S+\s+(?:years?|months?|weeks?|days?)\b",
                s, re.IGNORECASE):
        return False
    if re.match(r"^(?:approx|rs\b|inr\b|usd\b|₹|\d)", s, re.IGNORECASE):
        return False
    # a signature line or a company name: "Ameenji Rubber Limited ..."
    if re.match(r"^(?:[A-Z][\w&.'-]*\s+){1,6}(?:Limited|Ltd|LIMITED|LTD)\b", s):
        return False
    # a whole order sentence, not the object of one
    if _ORDER_NOUN.search(s) and _RECEIPT.search(s):
        return False
    return sum(ch.isdigit() or ch in "/:" for ch in s) <= len(s) * 0.2


def _modifier(pre: str) -> str:
    """'secured a 124 MW module supply order' -> '124 MW module supply'."""
    m = re.search(r"((?:[\w&/-]+\s+){1,6})(?:orders?|contracts?)\s*$", pre, re.IGNORECASE)
    if not m:
        return ""
    words = m.group(1).split()
    last_stop = max((i for i, w in enumerate(words)
                     if w.lower() in _MODIFIER_STOPWORDS), default=-1)
    return " ".join(words[last_stop + 1:])


_FROM = re.compile(
    r"\b(?:from|with)\s+(?:m/s\.?\s*)?(?:an?\s+|the\s+)?"
    r"(?P<c>[A-Z][^,;()\"]{2,90}?)"
    r"(?=\s*\(|\s*,|\s+for\b|\s+worth\b|\s+having\b|\s+dated\b|\s+on\b|\s+to\b|\s*$)")


def _sentence_candidates(text: str, weight: float) -> list[tuple[float, str, str | None]]:
    out = []
    for m in _LEAD.finditer(text):
        window = text[max(0, m.start() - 260):m.start()]
        cut = _boundaries(window)
        pre = window[cut[-1]:] if cut else window
        if m.group(0).lower().startswith(("order", "contract")):
            mod = ""                         # "contract of supply of X" -> "supply of X"
        else:
            nouns = list(_ORDER_NOUN.finditer(pre))
            if not nouns or len(pre) - nouns[-1].end() > _MAX_LEAD_DISTANCE:
                continue
            mod = _modifier(pre)
        obj = _trim(text[m.end():m.end() + 400])
        if not _usable(obj):
            continue
        phrase = f"{mod} for {obj}" if mod else obj
        # "has received an order ... for" and "the order is for" both name the
        # object of THIS order; a bare "for" near the word order may not
        strong = _RECEIPT.search(pre) or re.search(
            r"\b(?:orders?|contracts?|lo[ai])\s+(?:is|are)\s*$", pre, re.IGNORECASE)
        froms = list(_FROM.finditer(pre))
        customer = froms[-1].group("c").strip() if froms else None
        out.append((weight + (1.0 if strong else 0.0), phrase, customer))
    return out


def _table_candidates(text: str) -> list[tuple[float, str, str | None]]:
    out = []
    for m in _TABLE_FIELD.finditer(text):
        window = text[m.end():m.end() + 420]
        # skip past the label's own "order(s)/contract(s) awarded in brief"
        # before looking for the next field, or it ends the window at once
        nxt = _TABLE_NEXT.search(window, 12)
        if nxt:
            window = window[:nxt.start()]
        else:
            window = window[:window.rfind(" ")]      # not mid-word
        value = _trim(re.sub(r"\s+", " ", _TABLE_LABEL.sub(" ", window)))
        if _usable(value) and not _TABLE_JUNK.match(value):
            out.append((2.5, value, None))
    return out


def _table_customer(text: str) -> str | None:
    """The Reg-30 'Name of the entity awarding the order' answer."""
    for m in _ENTITY_FIELD.finditer(text):
        window = text[m.end():m.end() + 220]
        if "awarding" not in window[:140].lower():
            continue
        nxt = _TABLE_NEXT.search(window, 10)
        v = _ENTITY_LABEL.sub(" ", window[:nxt.start()] if nxt else window)
        v = re.sub(r"\s+", " ", v).strip(" :;,.-/")
        v = re.sub(r"^(?:\d{1,2}|[a-h]\s*\))\s+", "", v)          # row numbering
        v = re.sub(r"^m/s\.?\s*", "", v, flags=re.IGNORECASE)
        cut = _boundaries(v)
        v = v[:cut[0] - 2] if cut else v
        v = _tidy(re.split(r";|\s\d{1,2}\s*[.)]?(?:\s|$)", v)[0])
        if 3 <= len(v) <= 100 and not _ENTITY_JUNK.match(v):
            return v
    return None


def _lowercase_lead(s: str) -> str:
    """'Supply of Control Cables' -> 'supply of Control Cables', but a
    title-cased list ('Supply, Installation and Commissioning') is left alone
    rather than coming out half-lowercased."""
    words = s.split(" ", 2)
    word = words[0].rstrip(",;:")
    nxt = words[1] if len(words) > 1 else ""
    if (word[:1].isupper() and word[1:].islower() and word.lower() in _COMMON_LEAD
            and not nxt[:1].isupper()):
        return s[0].lower() + s[1:]
    return s


def order_scope(headline: str | None, body: str | None,
                pdf_text: str | None) -> tuple[str | None, str | None]:
    """Return (what the order is for, who placed it) as stated in the filing."""
    cands: list[tuple[float, int, str, str | None]] = []
    table_customer = None
    for text, weight in ((_clean(body), 2.0), (_clean(pdf_text), 2.0),
                         (_clean(headline), 1.8)):
        if not text:
            continue
        table_customer = table_customer or _table_customer(text)
        for score, phrase, cust in _sentence_candidates(text, weight) + _table_candidates(text):
            words = len(phrase.split())
            # a fuller description beats a terse one, up to about a line
            score += min(words, 12) / 12 - (1.0 if words > 40 else 0.0)
            cands.append((score, len(cands), phrase, cust))
    if not cands:
        return None, table_customer
    _, _, phrase, sentence_customer = max(cands, key=lambda c: (c[0], -c[1]))
    return _lowercase_lead(phrase), table_customer or sentence_customer


def fallback_title(headline: str | None, body: str | None) -> str | None:
    """When no object phrase was found: the filing's own title for itself."""
    b, h = _clean(body), _clean(headline)
    for text in (b, h):
        m = _TITLE.search(text)
        if m:
            return m.group("t").strip(" .;")
    m = _INFORMED.search(b)
    if m:
        t = _tidy(m.group("t"))
        if len(t) >= 20 and not _GENERIC.match(t) and not _JUNK.search(t):
            return t[0].upper() + t[1:]
    if len(h) >= 25 and not _GENERIC.match(h) and not _JUNK.search(h):
        return h.rstrip(" .")[:220]
    return None


# ---------------------------------------------------------------- sentence
def _inr(v: float) -> str:
    if v >= 1e7:
        n, unit = v / 1e7, "Cr"
    elif v >= 1e5:
        n, unit = v / 1e5, "L"
    else:
        return f"₹{v:,.0f}"
    s = f"{n:,.2f}".rstrip("0").rstrip(".")
    return f"₹{s} {unit}"


def _value(value, is_range, low, high) -> str:
    if is_range and low and high and low < high:
        lo, hi = _inr(low), _inr(high)
        lo_n, _, lo_u = lo[1:].partition(" ")
        hi_n, _, hi_u = hi[1:].partition(" ")
        return f"₹{lo_n}–{hi_n} {hi_u}".strip() if lo_u == hi_u else f"{lo}–{hi}"
    return _inr(value) if value else ""


def _period(months) -> str:
    if not months:
        return ""
    if months < 3 and abs(months - round(months)) > 0.1:
        return f"{round(months * 30.44)} days"
    if months >= 12 and abs(months / 12 - round(months / 12)) < 0.01:
        years = round(months / 12)
        return f"{years} year" + ("s" if years != 1 else "")
    m = round(months)
    return f"{m} month" + ("s" if m != 1 else "")


def _good_customer(name: str | None) -> bool:
    if not name:
        return False
    name = name.strip()
    if not name[:1].isupper() or _CUSTOMER_JUNK.search(name):
        return False
    if re.search(r"\b(?:worth|rs\.?|crores?|lakhs?|for\s+the)\b|\d{3,}", name, re.IGNORECASE):
        return False
    quotes = len(re.findall(r"(?:^|\s)'|'(?:\s|$)", name))
    if name.count("(") != name.count(")") or quotes % 2:
        return False
    # "North", "Power", "Madhya": a lone ordinary word is a truncated parse
    return len(name.split()) > 1 or name.isupper()


_NOUN = {
    "loa": "Letter of Award", "loi": "Letter of Intent", "l1": "lowest bid (L1)",
    "mou": "MoU", "amendment": "change to an order",
}

# The dashboard shows the summary in two lines, which hold about 180
# characters at desktop width. Summaries are written to fit rather than
# relying on the clamp, so the end of the sentence is never the part cut.
MAX_SUMMARY = 150

# Legal suffixes add length, not meaning.
_LEGAL_TAIL = re.compile(
    r"(?:[\s,]+(?:private|pvt\.?))?[\s,]+(?:limited|ltd\.?|llc|plc|inc\.?|gmbh|ltda)\.?$",
    re.IGNORECASE)
_ACRONYM = re.compile(r"\(([A-Z][A-Z0-9&.\- ]{2,15})\)")
_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")


def _short_customer(name: str) -> str:
    """'Ingka Centres Private Limited' -> 'Ingka Centres';
    'Northern Power Distribution Company of Telangana Limited (TG NPDCL)' -> 'TG NPDCL'."""
    name = re.sub(r"\s+", " ", name).strip(" ,.")
    if len(name) > 45 and "," in name:
        name = name.split(",")[0]            # "Steel Authority of India Limited, IISCO STEEL PLANT"
    if len(name) > 45:
        acro = _ACRONYM.search(name)
        if acro:
            return acro.group(1).strip()
    while len(name) > 45 and _TRAILING_PAREN.search(name):
        name = _TRAILING_PAREN.sub("", name)
    return _LEGAL_TAIL.sub("", name).strip(" ,.")


# "procurement, supply, installation, testing, commissioning, operation &
# maintenance of X" -> "procurement to operation & maintenance of X"
_LIST_LEAD = re.compile(
    r"^(?P<first>[A-Za-z]+),\s+(?:[A-Za-z]+(?:\s*&\s*[A-Za-z]+)?,\s+)+"
    r"(?:and\s+)?(?P<last>[A-Za-z]+(?:\s*&\s*[A-Za-z]+)?\s+of\s)", re.IGNORECASE)
_BREAKS = (", ", " for ", " under ", " at ", " in ", " from ", " including ",
           " through ", " with ", " as ", " via ")


def _fit(s: str, limit: int) -> str:
    """Shorten s to `limit` characters: at a clause break when that keeps most
    of the text, otherwise at a word boundary."""
    if len(s) <= limit:
        return s
    m = _LIST_LEAD.match(s)
    if m and m.group("first").lower() in _COMMON_LEAD and any(
            w.lower() in _COMMON_LEAD for w in re.findall(r"[A-Za-z]+", m.group("last"))):
        s = f"{m.group('first')} to {m.group('last')}{s[m.end():]}"
        if len(s) <= limit:
            return s
    head = s[:limit + 1]
    brk = max(head.rfind(b) for b in _BREAKS)
    cut = brk if brk >= limit * 0.6 else head.rfind(" ")
    return _tidy(s[:cut if cut > 0 else limit])


def summarize(*, scope: str | None, customer: str | None,
              order_type: str | None, value_inr: float | None = None,
              value_is_range: bool = False, value_low_inr: float | None = None,
              value_high_inr: float | None = None,
              execution_months: float | None = None,
              title: str | None = None) -> str | None:
    """Compose the description shown under the company name: one sentence of
    at most MAX_SUMMARY characters.

    Returns None rather than a bare "Rs 50 Cr order." when neither the work
    nor the customer is known: the value already has its own column, and the
    filing's headline says more about what it actually is.
    """
    noun = _NOUN.get(order_type or "", "order")
    value = _value(value_inr, value_is_range, value_low_inr, value_high_inr)
    who = _short_customer(customer) if _good_customer(customer) else None
    if who:
        who = re.sub(r"^(A|An|One)\b", lambda m: m.group(1).lower(), who)

    if not scope and title:
        # No clean object phrase, but the filing's own title says what it is.
        figure = value.split(" ")[0][1:] if value else ""
        suffix = f" ({value})" if value and figure not in title else ""
        return _fit(title, MAX_SUMMARY - len(suffix) - 1) + suffix + "."
    if not scope and not who:
        return None

    lead = f"{value} {noun}" if value else noun
    lead = lead[0].upper() + lead[1:]
    prep = " with " if order_type in ("mou", "l1") else " from "
    period = _period(execution_months)
    tail = f", over {period}." if period else "."
    if not scope:
        return lead + prep + who + tail

    joiner = " " if scope.lower().startswith("for ") else " for "
    head = lead + prep + who if who else lead
    room = MAX_SUMMARY - len(head) - len(joiner) - len(tail)
    if room < 40 and who:
        # a long customer name would squeeze out the work itself; keep the work
        head = lead
        room = MAX_SUMMARY - len(head) - len(joiner) - len(tail)
    return head + joiner + _fit(scope, room) + tail


def describe(headline: str | None, body: str | None, pdf_text: str | None, *,
             customer: str | None, order_type: str | None,
             value_inr: float | None = None, value_is_range: bool = False,
             value_low_inr: float | None = None, value_high_inr: float | None = None,
             execution_months: float | None = None) -> tuple[str | None, str | None]:
    """(scope, summary) for one filing, from its text and extracted fields."""
    scope, stated_customer = order_scope(headline, body, pdf_text)
    # The name the filing itself gives is usually cleaner than the regex pick
    # ("Ingka Centres Private Limited" vs "Ingka Centres Private").
    who = stated_customer if _good_customer(stated_customer) else customer
    summary = summarize(
        scope=scope, customer=who, order_type=order_type, value_inr=value_inr,
        value_is_range=value_is_range, value_low_inr=value_low_inr,
        value_high_inr=value_high_inr, execution_months=execution_months,
        title=None if scope else fallback_title(headline, body))
    return scope, summary
