"""A 300-file corpus over the gold set's business: 100 structured, 100 semi-structured, 100 documents.

    structured (100)       50 CSV   licences, 6 rows each
                           25 Parquet sales, one quarter of one region each
                           25 TSV   orders, 16 rows each
    semi-structured (100)  30 JSON  customers, 2 each ("crm-*")
                           30 JSON  the same customers again, from a second system with one more
                                    field ("billing-*") - duplicates the resolution engine must find
                           20 NDJSON orders, 20 each
                           10 YAML  projects
                           10 XML   devices
    documents (100)        the licensing policy, 35 terms letters (10 contradict licences.csv), and
                           64 RFC / PEP files from the 1000-file bench, chosen so that many name each
                           other in their headers (Obsoletes / Replaces / Requires) - links the
                           resolution engine draws

Every expected count is computed here and written to `expected.json`, so the check compares with
what the files hold, never with numbers typed by hand.
"""
from __future__ import annotations

import csv
import json
import random
import shutil
from collections import Counter
from pathlib import Path

from tests.goldset import domain

SEED = 300


def _chunks(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _xml(device: dict) -> str:
    from xml.sax.saxutils import escape
    body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in device.items())
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<device>{body}</device>\n'


def _bench_docs(bench: Path, n: int) -> list[Path]:
    """`n` RFC/PEP files whose headers name each other: every gold pair of the bench's linking
    questions that is in the corpus, both ends, until `n` files."""
    qs = [json.loads(line) for line in (bench / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    linking = ("pep_replaced_title", "pep_requires_status", "pep_superseded_author", "rfc_obsoleted_author",
               "rfc_obsoleted_title", "rfc_obsoleted_year")
    files = {p.name: p for p in (bench / "corpus").rglob("*") if p.is_file()}
    out: list = []
    for q in qs:
        if q["kind"] in linking and all(g in files for g in q["gold"]):
            for g in q["gold"]:
                if files[g] not in out:
                    out.append(files[g])
        if len(out) >= n:
            break
    return out[:n]


def write_mixed(root: str) -> dict:
    """120 files over one business, so every kind connects to the others: 10 CSV, 10 JSON, 100 documents.

        structured (10)       licences, 30 rows each              licence_id, customer_id (-> customers)
        semi-structured (10)  5 crm files, 12 customers each       customer_id, name, nested contact
                              5 billing files, the same customers  customer_id, name, billing_currency
                              (duplicates resolution must find)
        documents (100)       the licensing policy, 35 terms letters (10 contradict the CSV), and 64
                              account notes, one per customer, naming the customer and its licences
                              by id - what links a document to the records it is about

    Every expected count and answer is computed here and written to `expected.json`."""
    rnd = random.Random(SEED)
    d = domain.build()
    base = Path(root)
    if base.exists():
        shutil.rmtree(base)
    S, M, U = base / "structured", base / "semistructured", base / "unstructured"
    for p in (S, M, U):
        p.mkdir(parents=True)
    exp: dict = {"files": {}, "rows": {}, "records": {}, "profile": "mixed"}
    keys = list(d.licences[0])
    for i, part in enumerate(_chunks(d.licences, 30)):
        name = f"licences-{i:02d}.csv"
        with open(S / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(part)
        exp["rows"][name] = len(part)
    for i, part in enumerate(_chunks(d.customers, 12)):
        name = f"crm-{i:02d}.json"
        (M / name).write_text(json.dumps({"customers": part}, indent=1), encoding="utf-8")
        exp["records"][name] = 1
        billed = [{"customer_id": c["customer_id"], "name": c["name"], "region": c["region"],
                   "billing_currency": rnd.choice(("EUR", "USD", "SGD"))} for c in part]
        name = f"billing-{i:02d}.json"
        (M / name).write_text(json.dumps(billed, indent=1), encoding="utf-8")
        exp["records"][name] = len(billed)
    (U / "licensing-policy.md").write_text(domain.policy_markdown(), encoding="utf-8")
    for lic_id, (text, _) in d.letters.items():
        (U / f"terms-{lic_id}.md").write_text(text, encoding="utf-8")
    held: dict = {}
    for x in d.licences:
        held.setdefault(x["customer_id"], []).append(x["licence_id"])
    notes = []
    for c in d.customers:
        if len(notes) >= 100 - 1 - len(d.letters):
            break
        if c["name"] == domain.AMBIGUOUS:
            continue
        lic = held.get(c["customer_id"], [])
        lines = [f"# Account note: {c['name']}", "",
                 f"Customer {c['customer_id']} ({c['name']}) is a {c['tier']} account in region {c['region']}. "
                 f"The licensing contact is {c['contact']['email']}.", "",
                 f"They hold {len(lic)} licence(s): {', '.join(lic)}." if lic else "They hold no licence yet."]
        if c["free_pass"].get("active"):
            lines.append(f"A free pass is active until {c['free_pass']['valid_until']}.")
        text = "\n".join(lines) + "\n"
        name = f"account-{c['customer_id']}.md"
        (U / name).write_text(text, encoding="utf-8")
        notes.append({"file": name, "customer_id": c["customer_id"], "name": c["name"], "licences": lic})
    # Renewal notices, for licences without a terms letter, until there are 100 documents.
    for x in d.licences:
        if 1 + len(d.letters) + len(notes) >= 100:
            break
        if x["licence_id"] in d.letters:
            continue
        c = d.customer(x["customer_id"])
        name = f"renewal-{x['licence_id']}.md"
        (U / name).write_text(
            f"# Renewal notice: {x['licence_id']}\n\nLicensee: {c['name']} ({c['customer_id']})\n\n"
            f"Licence {x['licence_id']} ({x['type']}, {x['region']}) expires on {x['expiry_date']}. "
            f"Renewal requests must reach us sixty days before expiry.\n", encoding="utf-8")
        notes.append({"file": name, "customer_id": c["customer_id"], "name": c["name"], "licences": [x["licence_id"]]})
    for kind, folder in (("structured", S), ("semistructured", M), ("unstructured", U)):
        exp["files"][kind] = sorted(p.name for p in folder.iterdir())
    lic = d.licences
    exp["facts"] = {
        "licences_total": len(lic), "customers": len(d.customers),
        "expired_total": sum(1 for x in lic if x["status"] == "EXPIRED"),
        "policy_key": "ninety days", "conflicts": [list(c) for c in d.conflicts],
        "notes": notes, "ambiguous": domain.AMBIGUOUS,
    }
    exp["counts"] = {k: len(v) for k, v in exp["files"].items()}
    (base / "expected.json").write_text(json.dumps(exp, indent=1), encoding="utf-8")
    return exp


def write(root: str, bench: str) -> dict:
    rnd = random.Random(SEED)
    d = domain.build()
    base = Path(root)
    if base.exists():
        shutil.rmtree(base)
    S, M, U = base / "structured", base / "semistructured", base / "unstructured"
    for p in (S, M, U):
        p.mkdir(parents=True)
    exp: dict = {"files": {}, "rows": {}, "records": {}}

    # ── structured ──
    keys = list(d.licences[0])
    for i, part in enumerate(_chunks(d.licences, 6)):
        name = f"licences-{i:02d}.csv"
        with open(S / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(part)
        exp["rows"][name] = len(part)
    region_of = {x["licence_id"]: x["region"] for x in d.licences}
    import pyarrow as pa
    import pyarrow.parquet as pq
    for q in domain.QUARTERS:
        for r in domain.REGIONS:
            rows = [s for s in d.sales if s["quarter"] == q and region_of[s["licence_id"]] == r]
            if not rows:
                continue
            name = f"sales-{q}-{r}.parquet"
            pq.write_table(pa.Table.from_pylist(rows), S / name)
            exp["rows"][name] = len(rows)
    while len(list(S.glob("sales-*.parquet"))) < 25:            # 4 quarters x 5 regions = 20; pad to 25
        k = len(list(S.glob("sales-*.parquet")))
        rows = rnd.sample(d.sales, 12)
        name = f"sales-extra-{k}.parquet"
        pq.write_table(pa.Table.from_pylist(rows), S / name)
        exp["rows"][name] = len(rows)
    okeys = list(d.orders[0])
    for i, part in enumerate(_chunks(d.orders, 16)):
        name = f"orders-{i:02d}.tsv"
        with open(S / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=okeys, delimiter="\t")
            w.writeheader()
            w.writerows(part)
        exp["rows"][name] = len(part)

    # ── semi-structured ──
    for i, part in enumerate(_chunks(d.customers, 2)):
        name = f"crm-{i:02d}.json"
        (M / name).write_text(json.dumps({"customers": part}, indent=1), encoding="utf-8")
        exp["records"][name] = 1
        billed = [{"customer_id": c["customer_id"], "name": c["name"], "region": c["region"],
                   "billing_currency": rnd.choice(("EUR", "USD", "SGD"))} for c in part]
        name = f"billing-{i:02d}.json"
        (M / name).write_text(json.dumps(billed, indent=1), encoding="utf-8")
        exp["records"][name] = len(billed)
    for i, part in enumerate(_chunks(d.orders, 20)):
        name = f"orders-{i:02d}.ndjson"
        (M / name).write_text("".join(json.dumps(o) + "\n" for o in part), encoding="utf-8")
        exp["records"][name] = len(part)
    import yaml
    for i in range(10):
        owner = d.customers[i]
        proj = {"project": {"id": f"P-{i:03d}", "name": f"{owner['name']} rollout {i}",
                            "customer_id": owner["customer_id"], "status": rnd.choice(("green", "amber", "red")),
                            "milestones": [{"name": f"M{j}", "done": j < 2} for j in range(3)]}}
        name = f"project-{i:02d}.yaml"
        (M / name).write_text(yaml.safe_dump(proj, sort_keys=False), encoding="utf-8")
        exp["records"][name] = 1
    for i in range(10):
        dev = {"device_id": f"D-{i:03d}", "customer_id": d.customers[i]["customer_id"],
               "model": rnd.choice(("Encoder X", "Encoder Y", "Player Z")), "status": rnd.choice(("online", "offline"))}
        name = f"device-{i:02d}.xml"
        (M / name).write_text(_xml(dev), encoding="utf-8")
        exp["records"][name] = 1

    # ── documents ──
    (U / "licensing-policy.md").write_text(domain.policy_markdown(), encoding="utf-8")
    for lic_id, (text, _) in d.letters.items():
        (U / f"terms-{lic_id}.md").write_text(text, encoding="utf-8")
    bench_files = _bench_docs(Path(bench), 100 - 1 - len(d.letters))
    for p in bench_files:
        shutil.copy(p, U / p.name)

    for kind, folder in (("structured", S), ("semistructured", M), ("unstructured", U)):
        exp["files"][kind] = sorted(p.name for p in folder.iterdir())
    lic = d.licences
    exp["facts"] = {
        "active_eu": sum(1 for x in lic if x["status"] == "ACTIVE" and x["region"] == "EU"),
        "expired_total": sum(1 for x in lic if x["status"] == "EXPIRED"),
        "licences_total": len(lic),
        "orders_open": sum(1 for o in d.orders if o["status"] == "open"),
        "customers": len(d.customers),
        "duplicate_customers": len(d.customers),          # each appears in a crm- and a billing- file
        "bench_docs": [p.name for p in bench_files],
        "policy_key": "ninety days",
        "conflicts": [list(c) for c in d.conflicts],
    }
    counts = Counter(k for k in exp["files"] for _ in exp["files"][k])
    exp["counts"] = dict(counts)
    (base / "expected.json").write_text(json.dumps(exp, indent=1), encoding="utf-8")
    return exp
