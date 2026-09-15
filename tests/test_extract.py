"""Extraction tests against announcement text in the shapes NSE/BSE actually use."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from order_scanner.extract.money import find_amounts, fmt_inr, pick_order_value
from order_scanner.extract.rules import extract, looks_like_order
from order_scanner.extract.summary import MAX_SUMMARY

CR = 1e7
L = 1e5

# (text, expected_value_inr or None, note)
VALUE_CASES = [
    ("The Company has received a Letter of Award (LoA) from NTPC Limited for "
     "supply of BOP packages. The order value is Rs. 425.60 Crore (excluding GST).",
     425.60 * CR, "crore with LoA"),

    ("We wish to inform that the Company has bagged a work order worth "
     "INR 1,250 Lakhs from Maharashtra State Electricity Distribution Co. Ltd.",
     1250 * L, "lakhs, Indian grouping"),

    ("Receipt of Order: The Company has secured an export order valued at "
     "USD 12.5 Million from a leading customer in the United States.",
     12.5e6 * 88, "USD converted"),

    ("Intimation under Regulation 30: order aggregating to ₹ 1,25,50,00,000/- "
     "received from Indian Railways.", 1.2550e9, "rupee digits, Indian grouping"),

    ("The Company has received a purchase order of Rs 85 crore. The earnest money "
     "deposit of Rs 2.5 crore has been submitted and a bank guarantee of "
     "Rs 8.5 crore furnished.", 85 * CR, "must ignore EMD and BG"),

    ("Paid-up equity share capital of the Company is Rs. 45.20 Crore. "
     "The Company has been awarded a contract worth Rs. 312 Crore by NHAI.",
     312 * CR, "must ignore paid-up capital"),

    ("Company emerged as L1 bidder for a project of approximately "
     "Rs 100 - 120 crore from CPWD.", 110 * CR, "range midpoint"),

    ("Board Meeting intimation: the Board will consider unaudited financial "
     "results for the quarter ended June 30, 2026.", None, "not an order at all"),

    ("The Company has received an order for supply of transformers. "
     "Total value of the order is Rs. 9.87 Cr, to be executed within 9 months.",
     9.87 * CR, "abbreviated Cr"),
]


def test_values():
    print(f"\n{'VALUE EXTRACTION':-^88}")
    passed = failed = 0
    for text, expected, note in VALUE_CASES:
        amt = pick_order_value(text)
        got = amt.value_inr if amt else None
        if expected is None:
            ok = got is None or not looks_like_order(text)
        else:
            ok = got is not None and abs(got - expected) / expected < 0.02
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {note:<38} "
              f"got={fmt_inr(got):>18}  want={fmt_inr(expected):>18}")
    return passed, failed


DURATION_CASES = [
    ("to be executed within 18 months from the date of LoA", 18),
    ("over a period of 3 years", 36),
    ("completion period of 24 months", 24),
    ("The project shall be completed within 52 weeks", 12),
    ("execution period of 30 months from the date of the work order", 30),
]


def test_durations():
    print(f"\n{'DURATION':-^88}")
    passed = failed = 0
    for text, expected in DURATION_CASES:
        full = "The Company has received a work order. " + text
        r = extract(None, full)
        got = r.execution_months
        ok = got is not None and abs(got - expected) <= 1.0
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {text[:56]:<58} got={got}  want={expected}")
    return passed, failed


CLASSIFY_CASES = [
    ("Receipt of Letter of Award from Power Grid Corporation of India Limited "
     "for Rs 220 crore", "loa", "psu", False),
    # an MoU for a proposed investment reports no order received
    ("Company has signed an MoU with a state government for a proposed "
     "investment of Rs 500 crore", "unknown", "unknown", False),
    ("Intimation of cancellation of the work order of Rs 75 crore received "
     "earlier from the customer", "amendment", None, True),
    ("Received export order worth USD 5 Million from a customer in Germany "
     "for supply of components", None, "export", False),
    ("The Company has received a work order of Rs 40 crore from its wholly "
     "owned subsidiary", "work_order", None, False),
]


def test_classification():
    print(f"\n{'CLASSIFICATION':-^88}")
    passed = failed = 0
    for text, otype, ctype, is_amd in CLASSIFY_CASES:
        r = extract(None, text)
        ok = True
        if otype and r.order_type != otype:
            ok = False
        if ctype and r.customer_type != ctype:
            ok = False
        if r.is_amendment != is_amd:
            ok = False
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {text[:46]:<48} "
              f"type={r.order_type:<11} cust={r.customer_type:<11} amd={r.is_amendment}")
    return passed, failed


def test_related_party():
    r = extract(None, "The Company has received a work order of Rs 40 crore from "
                      "its wholly owned subsidiary.")
    ok = r.is_related_party
    print(f"\n{'PASS' if ok else 'FAIL'}  related-party detection")
    return (1, 0) if ok else (0, 1)


# (headline, body, text the summary must contain, text it must not contain)
# Shapes taken from real filings. The summary is None when the filing names
# neither the work nor the customer, so the dashboard shows the headline.
SUMMARY_CASES = [
    (None,
     "We are pleased to inform you that the Company has bagged a significant order "
     "from Ingka Centres Private Limited having an aggregate order value in the range "
     "of Rs. 5-10 crores, for the purpose of manufacture, supply, and delivery of LED "
     "Lights and Fixtures. The disclosure is for your information and records.",
     ["from Ingka Centres for",
      "for manufacture, supply, and delivery of LED Lights and Fixtures"],
     ["your information", "purpose of"]),

    (None,
     "Pursuant to Regulation 30, we wish to inform you that Hindalco Industries "
     "Limited has awarded an order of Rs. 27.82 crores for Supply of 1 BTAP rake "
     "alongwith 1 Brake Van for Aditya Expansion Project.",
     ["for supply of 1 BTAP rake alongwith 1 Brake Van for Aditya Expansion Project"],
     []),

    ("Intimation of receipt of purchase order",
     "Name of the entity awarding the Bajaj Finance order(s)/contract(s) 2. "
     "Significant terms and conditions of Supply of office order(s)/contract(s) "
     "awarded in brief chairs and workstations. 3. Whether order(s) / contract(s) "
     "have been awarded by domestic/ international entity Domestic 4.",
     ["from Bajaj Finance", "supply of office chairs and workstations"],
     ["order(s)", "in brief"]),

    (None,
     "The Company has received an order worth approximately Rupees 62.80 Lakhs "
     "excluding taxes from Transworld Systems India Private Limited for Office "
     "Chairs. Rs. 62,80,750/- excluding taxes.",
     ["from Transworld Systems India for", "for Office Chairs"],
     ["Chai,", "Chai."]),

    (None,
     "The Company has received a Letter of Intent for the establishment of a "
     "transmission system for evacuation of power. Name of the entity to which the "
     "order is awarded: HC Concessions Limited. Yours faithfully, For Ceigall India "
     "Limited, Company Secretary",
     ["for the establishment of a transmission system for evacuation of power"],
     ["HC Concessions", "Company Secretary", "Ceigall"]),

    (None,
     "The Company has received a Letter of Award from Northern Power Distribution "
     "Company of Telangana Limited (TG NPDCL) for procurement, supply, installation, "
     "integration, testing, commissioning, operation & maintenance of smart energy "
     "meters, PP Box maintenance and network operation & monitoring under cloud-based "
     "HES, MDAS & MDMS.",
     ["Letter of Award", "smart energy meters"],
     ["Limited"]),

    (None, "The Company has received an order worth Rs. 50 crore.", None, []),
]


def test_summary():
    print(f"{chr(10)}{'ORDER SUMMARY':-^88}")
    passed = failed = 0
    for headline, body, must, must_not in SUMMARY_CASES:
        r = extract(headline, body)
        got = r.summary
        if must is None:
            ok = got is None
        else:
            ok = (got is not None and all(m in got for m in must)
                  and not any(n in got for n in must_not)
                  and len(got) <= MAX_SUMMARY)
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {got}")
    return passed, failed

def test_reg30_answers():
    from order_scanner.extract.reg30 import clean_customer, parse_period, parse_size, yes_no

    print(f"{chr(10)}{'REG-30 TABLE ANSWERS':-^88}")
    filed = "2026-09-12T10:00:00+05:30"
    cr = lambda a: round(a.value_inr / CR, 2) if a else None
    cases = [
        ("period: duration", parse_period("12 months from LOA", filed), 12),
        ("period: words with numerals", parse_period("One (01) Year", filed), 12),
        ("period: range takes the upper bound", parse_period("Within 6 - 9 Months.", filed), 9),
        ("period: execution inside a longer term",
         parse_period("48 Months (24 Months Execution and 24 months Warranty)", filed), 24),
        ("period: typo Moths", parse_period("3 Moths", filed), 3),
        ("period: completion date", round(parse_period("By 30-09-2027", filed)), 13),
        ("period: not applicable", parse_period("Not Applicable", filed), None),
        ("size: incl. and excl. GST",
         cr(parse_size("Rs 85.53 Crore including GST (Rs 72.48 Crore excluding GST)")), 85.53),
        ("size: spaced R s.", cr(parse_size("R s. 6,91,22,294.10 (Rupees Six Crore)")), 6.91),
        ("size: lakhs", cr(parse_size("INR 1,388 Lacs (inclusive of taxes)")), 13.88),
        ("size: not money", parse_size("124 MW"), None),
        ("answer: No", yes_no("No"), False),
        ("answer: N.A.", yes_no("N.A."), False),
        ("answer: Yes", yes_no("Yes, the promoter is a director"), True),
        ("customer: undisclosed", clean_customer("Not disclosed due to confidentiality"), None),
        ("customer: quoted", clean_customer('"Modern Coach Factory (MCF), Raebareli"'),
         "Modern Coach Factory (MCF), Raebareli"),
    ]
    passed = failed = 0
    for label, got, want in cases:
        ok = got == want
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {label:<44} got={got!r}  want={want!r}")
    return passed, failed


def test_reg30_in_extraction():
    from order_scanner.extract.reg30 import Reg30

    print(f"{chr(10)}{'REG-30 TABLE IN EXTRACTION':-^88}")
    question_only = extract(None,
        "The Company has received a purchase order of Rs 40 crore from NTPC Limited. "
        "Whether the promoter/ promoter group/ group companies have any interest in the "
        "entity that awarded the order(s)/contract(s)? No. Whether the order(s)/contract(s) "
        "would fall within related party transactions? If yes, whether the same is done at "
        "arm's length: No")
    table = extract(None, "The Company has received an order.",
                    reg30=Reg30(customer="Hindalco Industries Limited",
                                period="Within 8 months from the date of PO.",
                                size="Rs. 27.82 crores (including taxes)",
                                related_party="No", nature="Supply of 1 BTAP rake"),
                    filed_at="2026-09-12T10:00:00+05:30")
    cases = [
        ("the related-party question alone is not a flag", question_only.is_related_party, False),
        ("table customer", table.customer, "Hindalco Industries Limited"),
        ("table period", table.execution_months, 8),
        ("table value", round(table.order_value_inr / CR, 2), 27.82),
        ("table related-party answer", table.is_related_party, False),
        ("order disclosure defaults to a firm order", table.order_type, "work_order"),
    ]
    passed = failed = 0
    for label, got, want in cases:
        ok = got == want
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {label:<48} got={got!r}")
    return passed, failed


# (headline, body, subcategory, counts as an order received, is an amendment)
RECEIVED_CASES = [
    ("With reference to the discrepancy communicated by the exchange regarding the "
     "Consolidated Review Report submitted along with the financial results for the "
     "quarter ended June 30, 2026",
     "Reply to discrepancy in financial results. The report was uploaded on the portal.",
     "General", False, None),
    ("The company has executed working capital loan agreement with UCO Bank.",
     "Details of the loan amount are attached.", "General", False, None),
    (None, "The company has received the approval order from the Regional Director for "
           "shifting of its registered office from Mumbai to Ahmedabad.", "General", False, None),
    (None, "BCPL Railway Infrastructure Limited emerges as the Single Lowest Bidder (L1) "
           "from Eastern Railway for the tender of Rs 5.89 crore. Time period: to be "
           "finalized after receipt of Letter of Acceptance.", "Updates", False, None),
    (None, "Company has signed an MoU with a state government for a proposed investment "
           "of Rs 500 crore.", "Memorandum of Understanding /Agreements", False, None),
    ("Systematic Industries Limited secures landmark Order from POWERGRID for supply of OPGW",
     None, "Press Release / Media Release", True, False),
    ("Please find attached", "Name of the entity awarding the order(s)/contract(s) Hindalco "
     "Industries Limited", "Award of Order / Receipt of Order", True, False),
    (None, "As per the Policy for Determination of Materiality, the Company has received a "
           "purchase order of Rs 12 crore from NTPC Limited.", None, True, False),
    ("Cancellation of earlier work order", "Intimation regarding cancellation of the work "
     "order of Rs 150 crore received earlier from the customer.",
     "Award of Order / Receipt of Order", True, True),
]


def test_order_received():
    print(f"{chr(10)}{'ORDER RECEIVED OR NOT':-^88}")
    passed = failed = 0
    for headline, body, subcat, want_order, want_amd in RECEIVED_CASES:
        r = extract(headline, body, subcategory=subcat)
        ok = r.is_order == want_order and (want_amd is None or r.is_amendment == want_amd)
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        why = next((n for n in r.notes if "order" in n and ("evidence" in n or "not an" in n)), "")
        print(f"{'PASS' if ok else 'FAIL'}  {(headline or body)[:48]:<50} "
              f"order={r.is_order} amd={r.is_amendment}  {why[:60]}")
    return passed, failed


# Tesseract's reading of Veerhealth's filing (11 Sep 2026), kept with its errors:
# the rupee sign as %, ~, ¢, = and 7; "11" as "I1"; the full stop after the FY27
# PAT guidance read as a comma, which runs it into the client projection.
VEER_TEXT = (
    "We are enclosing herewith the press release on receipt of Sample Order worth %10.07 "
    "Lacs from a Company having an Indo-Canadian joint venture for Supply of Oral Care "
    "Products. Veerhealth Care received Sample Order worth 710.07 Lacs from a Company having "
    "an Indo-Canadian joint venture for Supply of Oral Care Products. An MoU has been signed "
    "with this Client for long term Business. In Current F.Y. 2026-27 we are expecting Total "
    "Income of ~ 105 crores with PAT of minimum ¢ 5 - 7 crores, In Current F.Y. 2026-27 in "
    "next 6 months we are expecting additional turnover of around % 3-4 crores from above "
    "client. In F.Y. 2027-28 we are expecting Total Income of % 156 crores with PAT of "
    "minimum ¢ 9 - I1 crores. In F.Y. 2027-28 we are expecting additional Turnover of around "
    "= 10 crores from above client.")


def test_guidance_and_rupee():
    from order_scanner.extract.money import normalize_rupee_glyphs
    from order_scanner.extract.pdf import rupee_damage

    print(f"{chr(10)}{'RUPEE GLYPHS AND GUIDANCE':-^88}")
    r = extract(None, VEER_TEXT, filed_at="2026-09-11T16:53:35+05:30")
    g = {(i["fy_end"], i["metric"]): i for i in r.guidance}
    cr = lambda key: round((g.get(key) or {}).get("low_inr", 0) / CR, 2)
    cases = [
        ("OCR's % in front of lakh is the rupee sign",
         normalize_rupee_glyphs("worth %10.07 Lacs"), "worth ₹10.07 Lacs"),
        ("a stray letter in front of lakh is the rupee sign",
         normalize_rupee_glyphs("worth n 10.07 Lacs"), "worth ₹ 10.07 Lacs"),
        ("a real percentage is left alone",
         normalize_rupee_glyphs("grew 20% in 5 years"), "grew 20% in 5 years"),
        ("damaged rupee figures are detected", rupee_damage("worth n 0.07 Lacs"), True),
        ("clean text is not flagged", rupee_damage("worth Rs. 10.07 Lacs"), False),
        ("the order value, not the forecast", round(r.order_value_inr or 0), 1007000),
        ("FY27 total income guidance", cr((2027, "revenue")), 105.0),
        ("FY27 PAT guidance, lower bound", cr((2027, "pat")), 5.0),
        ("FY28 total income guidance", cr((2028, "revenue")), 156.0),
        ("FY28 PAT, with OCR's I1 read as 11", cr((2028, "pat")), 9.0),
        ("a 7 for the rupee sign goes when the figure recurs",
         normalize_rupee_glyphs("worth %10.07 Lacs. Received order worth 710.07 Lacs"),
         "worth ₹10.07 Lacs. Received order worth ₹10.07 Lacs"),
        ("a real leading 7 stays", normalize_rupee_glyphs("orders of 750 crores, 710 crores"),
         "orders of 750 crores, 710 crores"),
        ("a client's additional turnover is not guidance", len(r.guidance), 4),
    ]
    passed = failed = 0
    for label, got, want in cases:
        ok = got == want
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{'PASS' if ok else 'FAIL'}  {label:<48} got={got!r}")
    return passed, failed


if __name__ == "__main__":
    totals = [0, 0]
    for fn in (test_values, test_durations, test_classification, test_related_party,
               test_summary, test_reg30_answers, test_reg30_in_extraction,
               test_order_received, test_guidance_and_rupee):
        p, f = fn()
        totals[0] += p
        totals[1] += f
    print(f"\n{'':-^88}\n{totals[0]} passed, {totals[1]} failed")
    raise SystemExit(1 if totals[1] else 0)
