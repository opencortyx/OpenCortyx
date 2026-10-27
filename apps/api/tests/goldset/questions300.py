"""200 questions over the 300-file corpus (corpus.py), every answer computed from the files.

    documents         60   a terms letter's expiry (25), the policy (15), RFC / PEP facts and the
                           links between them, from the bench's own questions (20)
    structured        45   counts and totals over one licence, sales or order file (SQL), and one
                           record's value (a licence's fee, an order's amount)
    semi-structured   45   a customer's region or billing currency, a project's status, a device's
                           model, an order's status
    across sources    20   a project's or device's customer by name (record -> customer), a letter
                           against licences CSV (conflicts, both dates)
    unknown           20   an attribute no source has, a record that does not exist
    ambiguous         10   the name two customers share

Same scoring as the gold set (run.py): `expect` groups, outcome ANSWERED / UNKNOWN / CLARIFY.
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

from tests.goldset import domain
from tests.goldset.questions import POLICY_KEYS, _dates, _money, _q

SEED = 2000


def build(root: str, bench: str) -> list[dict]:
    rnd = random.Random(SEED)
    d = domain.build()
    base = Path(root)
    exp = json.loads((base / "expected.json").read_text(encoding="utf-8"))
    qs: list[dict] = []
    conflict_ids = {c[0] for c in d.conflicts}

    # ── documents ────────────────────────────────────────────────────────────────────────────
    for lic_id, (_, expiry) in d.letters.items():
        if lic_id not in conflict_ids:
            _q(qs, "document_fact", f"According to its terms letter, when does licence {lic_id} expire?",
               [_dates(expiry)], sources=[f"terms-{lic_id}.md"])
    for title, keys in POLICY_KEYS.items():
        _q(qs, "passage_summary", f"What does the licensing policy say about {title.lower()}?", [keys],
           sources=["licensing-policy.md"])
    have = set(exp["facts"]["bench_docs"])
    bench_q = [json.loads(line) for line in (Path(bench) / "questions.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    picked = [q for q in bench_q if q["gold"] and all(g in have for g in q["gold"]) and q["kind"] != "pdf_text"]
    rnd.shuffle(picked)
    for q in picked[:20]:
        _q(qs, "bench_" + q["kind"], q["question"], q["expect"], sources=q["gold"])

    # ── structured ───────────────────────────────────────────────────────────────────────────
    S = base / "structured"
    lic_files = sorted(p for p in S.glob("licences-*.csv") if p.name not in ("licences-01.csv", "licences-02.csv"))
    for p in lic_files[:15]:
        rows = list(csv.DictReader(open(p, encoding="utf-8")))
        st = rnd.choice(["ACTIVE", "EXPIRED"])
        n = sum(1 for r in rows if r["status"] == st)
        _q(qs, "sql_count", f"How many {st} licences are listed in {p.stem}?", [[str(n)]], sources=[p.name])
    for p in lic_files[15:25]:
        rows = list(csv.DictReader(open(p, encoding="utf-8")))
        tot = round(sum(float(r["fee"]) for r in rows), 2)
        _q(qs, "sql_sum", f"What is the total fee of the licences in {p.stem}, rounded to two decimals?",
           [_money(tot)], sources=[p.name])
    for p in lic_files[25:35]:
        r = rnd.choice(list(csv.DictReader(open(p, encoding="utf-8"))))
        _q(qs, "row_value", f"What is the annual fee of licence {r['licence_id']}?",
           [_money(round(float(r["fee"]), 2)) + [str(r["fee"])]], sources=[p.name])
    for p in sorted(S.glob("orders-*.tsv"))[:10]:
        r = rnd.choice(list(csv.DictReader(open(p, encoding="utf-8"), delimiter="\t")))
        _q(qs, "row_value", f"What was the amount of order {r['order_id']}?",
           [_money(round(float(r["amount"]), 2)) + [str(r["amount"])]], sources=[p.name])

    # ── semi-structured ──────────────────────────────────────────────────────────────────────
    unique = [c for c in d.customers if c["name"] != domain.AMBIGUOUS]
    for c in unique[:15]:
        _q(qs, "record_value", f"Which region is customer {c['customer_id']} in?", [[c["region"]]])
    M = base / "semistructured"
    billing = {}
    for p in sorted(M.glob("billing-*.json")):
        for b in json.loads(p.read_text(encoding="utf-8")):
            billing[b["customer_id"]] = (b, p.name)
    for c in unique[15:30]:
        b, f = billing[c["customer_id"]]
        _q(qs, "record_value", f"What billing currency does {c['name']} use?", [[b["billing_currency"]]], sources=[f])
    import yaml
    for p in sorted(M.glob("project-*.yaml"))[:8]:
        pr = yaml.safe_load(p.read_text(encoding="utf-8"))["project"]
        _q(qs, "record_value", f"What is the status of project {pr['id']}?", [[pr["status"]]], sources=[p.name])
    import re
    for p in sorted(M.glob("device-*.xml"))[:7]:
        x = p.read_text(encoding="utf-8")
        dev = re.search(r"<device_id>(.*?)</device_id>", x).group(1)
        model = re.search(r"<model>(.*?)</model>", x).group(1)
        _q(qs, "record_value", f"What model is device {dev}?", [[model]], sources=[p.name])

    # ── across sources ───────────────────────────────────────────────────────────────────────
    name_of = {c["customer_id"]: c["name"] for c in d.customers}
    for p in sorted(M.glob("project-*.yaml"))[:5]:
        pr = yaml.safe_load(p.read_text(encoding="utf-8"))["project"]
        _q(qs, "cross_source", f"Which customer does project {pr['id']} belong to? Give the name.",
           [[name_of[pr["customer_id"]]]], sources=[p.name])
    for p in sorted(M.glob("device-*.xml"))[:5]:
        x = p.read_text(encoding="utf-8")
        dev = re.search(r"<device_id>(.*?)</device_id>", x).group(1)
        cid = re.search(r"<customer_id>(.*?)</customer_id>", x).group(1)
        _q(qs, "cross_source", f"Which customer runs device {dev}? Give the name.", [[name_of[cid]]], sources=[p.name])
    for lic_id, csv_expiry, letter_expiry in d.conflicts:
        _q(qs, "conflicting", f"When does licence {lic_id} expire?", [_dates(csv_expiry), _dates(letter_expiry)])

    # ── unknown ──────────────────────────────────────────────────────────────────────────────
    for c in unique[30:40]:
        _q(qs, "missing_fact", f"What is the credit score of {c['name']}?", outcome="UNKNOWN",
           unknown_reason="no source has the attribute")
    for n in range(10):
        _q(qs, "no_answer", f"What is the annual fee of licence LIC-{9100 + n}?", outcome="UNKNOWN",
           unknown_reason="no matching rows")

    # ── ambiguous ────────────────────────────────────────────────────────────────────────────
    ids = [c["customer_id"] for c in d.customers if c["name"] == domain.AMBIGUOUS]
    for text in ("Which region is {n} in?", "What tier is {n} on?", "Does {n} have a free pass?",
                 "What billing currency does {n} use?", "What is the contact email of {n}?",
                 "Which country is {n} in?", "What licence does {n} hold?", "How many orders does {n} have?",
                 "When does the {n} licence expire?", "What is the fee of the {n} licence?"):
        _q(qs, "ambiguous", text.format(n=domain.AMBIGUOUS), outcome="CLARIFY", candidates=ids)

    for i, q in enumerate(qs, start=1):
        q["id"] = f"c{i:03d}"
    return qs[:200]
