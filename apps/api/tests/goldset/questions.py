"""The gold set v1: 200 questions over the licensing business, every answer computed from the data.

Eleven types, the ones docs/knowledge-layer-implementation.md (section 6) names, so a miss can be
sorted by cause:

    document_fact 25        a fact one document states                       ANSWERED
    passage_summary 15      what a policy section says                       ANSWERED
    simple_sql 30           one filter and a count                           ANSWERED
    complex_sql 25          grouping, ranking, averages                      ANSWERED
    managed_external 20     a managed CSV joined with a customer's Delta     ANSWERED
    cross_source 25         three sources at once (JSON, CSV, live DB)       ANSWERED
    missing_fact 15         a real entity, an attribute no source has        UNKNOWN
    conflicting 10          a letter and the CSV disagree on a date          ANSWERED (both stated)
    ambiguous 10            a name two customers share                       CLARIFY
    no_answer 15            an entity that does not exist                    UNKNOWN
    unreachable 10          the only source is offline                       UNKNOWN

`expect` is a list of groups; every group needs one of its members in the answer.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from tests.goldset.domain import AMBIGUOUS, QUARTERS, REGIONS, STATUSES, TYPES, Domain

#: The phrase each policy section's answer must contain.
POLICY_KEYS = {
    "Early termination": ["ninety days"], "Renewal": ["sixty days"], "Suspension": ["forty-five days"],
    "Transfer": ["written consent"], "Territory": ["separate licence"], "Fees": ["five percent"],
    "Free pass": ["no charge"], "Audit": ["once per calendar year"], "Data retention": ["seven years"],
    "Disputes": ["mediation"], "Sublicensing": ["not permitted"], "Education discount": ["thirty percent"],
    "Streaming caps": ["fifty thousand"], "Events": ["single venue"], "Contact": ["three working days"],
}


def _dates(iso: str) -> list[str]:
    """The ways an answer may write a date."""
    d = date.fromisoformat(iso)
    month = d.strftime("%B")
    return list(dict.fromkeys([iso, f"{d.day} {month} {d.year}", d.strftime("%d %B %Y"),
                               f"{month} {d.day}, {d.year}"]))


def _money(x: float) -> list[str]:
    return [f"{x:.2f}", f"{x:,.2f}"]


def _q(qs: list, type_: str, question: str, expect=None, outcome="ANSWERED", sources=(), **extra):
    qs.append({"id": f"g{len(qs) + 1:03d}", "type": type_, "question": question,
               "expect": expect or [], "outcome": outcome, "sources": list(sources), **extra})


def build(d: Domain) -> list[dict]:
    qs: list[dict] = []
    lic_by_cust = defaultdict(list)
    for lic in d.licences:
        lic_by_cust[lic["customer_id"]].append(lic)
    unique = [c for c in d.customers if c["name"] != AMBIGUOUS]
    open_orders = Counter(o["customer_id"] for o in d.orders if o["status"] == "open")
    conflict_ids = {c[0] for c in d.conflicts}

    # ── document_fact ────────────────────────────────────────────────────────────────────────
    for lic_id, (_, expiry) in d.letters.items():
        if lic_id in conflict_ids:
            continue
        _q(qs, "document_fact", f"According to its terms letter, when does licence {lic_id} expire?",
           [_dates(expiry)], sources=[f"terms-{lic_id}.md"])

    # ── passage_summary ──────────────────────────────────────────────────────────────────────
    for title, keys in POLICY_KEYS.items():
        _q(qs, "passage_summary", f"What does the licensing policy say about {title.lower()}?",
           [keys], sources=["licensing-policy.md"])

    # ── simple_sql ───────────────────────────────────────────────────────────────────────────
    for status in STATUSES:
        for region in REGIONS:
            n = sum(1 for x in d.licences if x["status"] == status and x["region"] == region)
            _q(qs, "simple_sql", f"How many {status} licences are there in region {region}?",
               [[str(n)]], sources=["licences.csv"])
    for t in TYPES:
        n = sum(1 for x in d.licences if x["type"] == t)
        _q(qs, "simple_sql", f"How many {t} licences are there in total?", [[str(n)]],
           sources=["licences.csv"])
    for c in [c for c in unique if open_orders[c["customer_id"]]][:10]:
        _q(qs, "simple_sql", f"How many open orders does {c['name']} have?",
           [[str(open_orders[c["customer_id"]])]], sources=["orders.sqlite"])

    # ── complex_sql ──────────────────────────────────────────────────────────────────────────
    expired = Counter(x["region"] for x in d.licences if x["status"] == "EXPIRED")
    _q(qs, "complex_sql", "Which region has the most EXPIRED licences?",
       [[expired.most_common(1)[0][0]]], sources=["licences.csv"])
    for t in TYPES:
        fees = [x["fee"] for x in d.licences if x["type"] == t]
        _q(qs, "complex_sql", f"What is the average fee of {t} licences, rounded to two decimals?",
           [_money(round(sum(fees) / len(fees), 2))], sources=["licences.csv"])
    for region in REGIONS:
        tot = Counter()
        for x in d.licences:
            if x["region"] == region:
                tot[x["type"]] += x["fee"]
        _q(qs, "complex_sql", f"Which licence type has the highest total fee in region {region}?",
           [[tot.most_common(1)[0][0]]], sources=["licences.csv"])
    for year in (2023, 2024, 2025):
        n = sum(1 for x in d.licences if x["issued"].startswith(str(year)) and x["status"] == "ACTIVE")
        _q(qs, "complex_sql", f"How many licences issued in {year} are still ACTIVE?", [[str(n)]],
           sources=["licences.csv"])
    for region in REGIONS:
        tot = round(sum(x["fee"] for x in d.licences if x["region"] == region and x["status"] == "ACTIVE"), 2)
        _q(qs, "complex_sql", f"What is the total fee of ACTIVE licences in region {region}, rounded "
           f"to two decimals?", [_money(tot)], sources=["licences.csv"])
    per_cust = Counter(x["customer_id"] for x in d.licences)
    _q(qs, "complex_sql", "How many customers hold more than 5 licences?",
       [[str(sum(1 for n in per_cust.values() if n > 5))]], sources=["licences.csv"])
    _q(qs, "complex_sql", "How many licences expire in 2027?",
       [[str(sum(1 for x in d.licences if x["expiry_date"].startswith("2027")))]], sources=["licences.csv"])
    stream = [x for x in d.licences if x["type"] == "Streaming"]
    share = round(100 * sum(1 for x in stream if x["status"] == "SUSPENDED") / len(stream), 1)
    _q(qs, "complex_sql", "What percentage of Streaming licences are SUSPENDED, to one decimal?",
       [[f"{share:.1f}"]], sources=["licences.csv"])
    _q(qs, "complex_sql", "How many distinct customers hold Education licences?",
       [[str(len({x["customer_id"] for x in d.licences if x["type"] == "Education"}))]],
       sources=["licences.csv"])
    avg = {r: sum(x["fee"] for x in d.licences if x["region"] == r) /
           max(1, sum(1 for x in d.licences if x["region"] == r)) for r in REGIONS}
    _q(qs, "complex_sql", "Which region has the highest average licence fee?",
       [[max(avg, key=avg.get)]], sources=["licences.csv"])
    _q(qs, "complex_sql", "How many licences were issued in the second half of 2024?",
       [[str(sum(1 for x in d.licences if "2024-07-01" <= x["issued"] <= "2024-12-31"))]],
       sources=["licences.csv"])

    # ── managed_external ─────────────────────────────────────────────────────────────────────
    region_of = {x["licence_id"]: x["region"] for x in d.licences}
    for q in QUARTERS:
        for region in REGIONS:
            tot = round(sum(s["revenue"] for s in d.sales
                            if s["quarter"] == q and region_of[s["licence_id"]] == region), 2)
            _q(qs, "managed_external", f"What was the total revenue in {q} from licences in region "
               f"{region}?", [_money(tot)], sources=["licences.csv", "sales"])

    # ── cross_source ─────────────────────────────────────────────────────────────────────────
    name_of = {c["customer_id"]: c["name"] for c in d.customers}
    for region in REGIONS:
        hits = sorted({name_of[c] for c in open_orders
                       if any(x["status"] == "EXPIRED" for x in lic_by_cust[c])
                       and next(cu for cu in d.customers if cu["customer_id"] == c)["region"] == region
                       and name_of[c] != AMBIGUOUS})
        if hits and len(hits) <= 8:
            _q(qs, "cross_source", f"Which customers in region {region} have open orders and an "
               f"EXPIRED licence?", [[h] for h in hits], sources=["customers.json", "orders.sqlite", "licences.csv"])
    for c in unique[:10]:
        fp = c["free_pass"]
        exp = [["yes"], _dates(fp["valid_until"])] if fp["active"] else [["no"]]
        _q(qs, "cross_source", f"Does customer {c['customer_id']} have a free pass, and until when?",
           exp, sources=["customers.json"])
    for region in REGIONS:
        holders = {x["customer_id"] for x in d.licences if x["type"] == "Streaming" and x["region"] == region}
        tot = round(sum(o["amount"] for o in d.orders if o["status"] == "open" and o["customer_id"] in holders), 2)
        _q(qs, "cross_source", f"What is the total amount of open orders from customers holding a "
           f"Streaming licence in region {region}?", [_money(tot)],
           sources=["licences.csv", "orders.sqlite"])
    for c in unique[10:20]:
        active = sum(1 for x in lic_by_cust[c["customer_id"]] if x["status"] == "ACTIVE")
        _q(qs, "cross_source", f"Which tier is {c['name']} on, and how many ACTIVE licences do they hold?",
           [[c["tier"]], [str(active)]], sources=["customers.json", "licences.csv"])
        if sum(1 for x in qs if x["type"] == "cross_source") >= 25:
            break

    # ── missing_fact ─────────────────────────────────────────────────────────────────────────
    for c in unique[20:25]:
        _q(qs, "missing_fact", f"What is the credit score of {c['name']}?", outcome="UNKNOWN",
           unknown_reason="no source has the attribute")
    for c in unique[25:30]:
        _q(qs, "missing_fact", f"What is the VAT number of {c['name']}?", outcome="UNKNOWN",
           unknown_reason="no source has the attribute")
    for lic in d.licences[10:15]:
        _q(qs, "missing_fact", f"Who is the account manager for licence {lic['licence_id']}?",
           outcome="UNKNOWN", unknown_reason="no source has the attribute")

    # ── conflicting ──────────────────────────────────────────────────────────────────────────
    for lic_id, csv_expiry, letter_expiry in d.conflicts:
        _q(qs, "conflicting", f"When does licence {lic_id} expire?",
           [_dates(csv_expiry), _dates(letter_expiry)], sources=["licences.csv", f"terms-{lic_id}.md"])

    # ── ambiguous ────────────────────────────────────────────────────────────────────────────
    ids = [c["customer_id"] for c in d.customers if c["name"] == AMBIGUOUS]
    for text in ("When does the {n} licence expire?", "Which region is {n} in?",
                 "How many licences does {n} hold?", "What tier is {n} on?", "Does {n} have a free pass?",
                 "What is the contact email of {n}?", "How many open orders does {n} have?",
                 "What type of licence does {n} hold?", "When was the {n} licence issued?",
                 "What is the fee of the {n} licence?"):
        _q(qs, "ambiguous", text.format(n=AMBIGUOUS), outcome="CLARIFY", candidates=ids)

    # ── no_answer ────────────────────────────────────────────────────────────────────────────
    for n in range(5):
        _q(qs, "no_answer", f"When does licence LIC-{9100 + n} expire?", outcome="UNKNOWN",
           unknown_reason="no matching rows")
        _q(qs, "no_answer", f"Which region is customer C-{900 + n} in?", outcome="UNKNOWN",
           unknown_reason="no matching rows")
        _q(qs, "no_answer", f"What was the amount of order O-{99000 + n}?", outcome="UNKNOWN",
           unknown_reason="no matching rows")

    # ── unreachable ──────────────────────────────────────────────────────────────────────────
    for o in d.archive_orders[:10]:
        _q(qs, "unreachable", f"What was the amount of archived order {o['order_id']}?",
           outcome="UNKNOWN", unknown_reason="the source is unavailable", sources=["archive.sqlite"])

    return qs
