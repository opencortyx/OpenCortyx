"""The gold set's business: a licensing company, spread over every kind of source a customer has.

    licences.csv          structured, managed      licence_id, customer_id, type, status, region,
                                                    issued, expiry_date, fee
    customers.json        semi-structured, managed  customers with nested contact, free_pass, tier
    orders.sqlite         a live database, external orders(order_id, customer_id, status, amount, created)
    sales/ (Delta)        an external lake table    sales(licence_id, quarter, revenue)
    archive.sqlite        a live database that is GONE by question time (unreachable source)
    docs/                 unstructured              the licensing policy, one terms letter per sampled
                                                    licence (ten of them disagree with licences.csv)

Everything comes from one seeded generator, so the data and every expected answer are reproducible
and computed, never typed by hand. Nothing here is real: names are drawn from fixed lists.
"""
from __future__ import annotations

import csv
import json
import os
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

SEED = 20261027
TYPES = ("Broadcast", "Retail", "Streaming", "Education", "Events")
REGIONS = ("EU", "US", "APAC", "LATAM", "MEA")
STATUSES = ("ACTIVE", "EXPIRED", "SUSPENDED")
QUARTERS = ("2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2")
FIRST = ("Northwind", "Bluebell", "Granite", "Silverline", "Harbor", "Juniper", "Kestrel", "Lumen",
         "Meridian", "Oakridge", "Pinecrest", "Quarry", "Redwood", "Saffron", "Tidewater", "Umber",
         "Vantage", "Willow", "Yarrow", "Zephyr", "Alder", "Brook", "Cedar", "Dune", "Ember", "Fjord",
         "Glade", "Heath", "Iris", "Jasper")
SECOND = ("Media", "Retail", "Studios", "Learning", "Events", "Holdings", "Group", "Partners")
#: Two customers share this surname-style name on purpose: questions naming it alone are ambiguous.
AMBIGUOUS = "Smith Media"


@dataclass
class Domain:
    customers: list[dict] = field(default_factory=list)
    licences: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    sales: list[dict] = field(default_factory=list)
    archive_orders: list[dict] = field(default_factory=list)
    #: licence_id -> (terms text, expiry the letter states)
    letters: dict = field(default_factory=dict)
    #: licences whose letter disagrees with the CSV: [(licence_id, csv expiry, letter expiry)]
    conflicts: list = field(default_factory=list)
    policy: dict = field(default_factory=dict)

    def customer(self, cid: str) -> dict:
        return next(c for c in self.customers if c["customer_id"] == cid)


def build() -> Domain:
    rnd = random.Random(SEED)
    d = Domain()
    names = [f"{a} {b}" for a in FIRST for b in SECOND]
    rnd.shuffle(names)
    names = [n for n in names if n != AMBIGUOUS][:58]
    for i, name in enumerate(names + [AMBIGUOUS, AMBIGUOUS], start=1):
        cid = f"C-{100 + i}"
        has_pass = rnd.random() < 0.3
        d.customers.append({
            "customer_id": cid, "name": name, "region": rnd.choice(REGIONS),
            "tier": rnd.choice(("gold", "silver", "bronze")),
            "contact": {"email": f"licensing@{name.split()[0].lower()}{i}.example",
                        "country": rnd.choice(("IE", "DE", "US", "SG", "BR", "AE", "JP", "FR"))},
            "free_pass": ({"active": True, "valid_until": (date(2026, 1, 1) + timedelta(days=rnd.randint(0, 700))).isoformat()}
                          if has_pass else {"active": False}),
        })
    for i in range(1, 301):
        cust = rnd.choice(d.customers)
        issued = date(2023, 1, 1) + timedelta(days=rnd.randint(0, 1200))
        expiry = issued + timedelta(days=rnd.choice((365, 730, 1095)))
        status = "EXPIRED" if expiry < date(2026, 10, 1) else rnd.choice(("ACTIVE",) * 6 + ("SUSPENDED",))
        d.licences.append({
            "licence_id": f"LIC-{1000 + i}", "customer_id": cust["customer_id"],
            "type": rnd.choice(TYPES), "status": status, "region": cust["region"],
            "issued": issued.isoformat(), "expiry_date": expiry.isoformat(),
            "fee": round(rnd.uniform(500, 25000), 2)})
    # Both customers named AMBIGUOUS hold licences, so a question naming only that name has two
    # equally good answers.
    for lic, cust in zip(d.licences[:2], [c for c in d.customers if c["name"] == AMBIGUOUS]):
        lic["customer_id"], lic["region"] = cust["customer_id"], cust["region"]
    for i in range(1, 401):
        cust = rnd.choice(d.customers)
        d.orders.append({"order_id": f"O-{5000 + i}", "customer_id": cust["customer_id"],
                         "status": rnd.choice(("open", "open", "closed", "closed", "closed", "cancelled")),
                         "amount": round(rnd.uniform(50, 9000), 2),
                         "created": (date(2025, 6, 1) + timedelta(days=rnd.randint(0, 480))).isoformat()})
    for lic in d.licences:
        for q in QUARTERS:
            if rnd.random() < 0.7:
                d.sales.append({"licence_id": lic["licence_id"], "quarter": q,
                                "revenue": round(rnd.uniform(100, 12000), 2)})
    for i in range(1, 41):
        cust = rnd.choice(d.customers)
        d.archive_orders.append({"order_id": f"A-{9000 + i}", "customer_id": cust["customer_id"],
                                 "amount": round(rnd.uniform(50, 9000), 2), "year": rnd.choice((2019, 2020, 2021))})
    # 25 terms letters that agree with licences.csv, 10 that move the expiry by a quarter.
    sample = rnd.sample(d.licences[2:], 35)
    for n, lic in enumerate(sample):
        cust = d.customer(lic["customer_id"])
        if n < 25:
            d.letters[lic["licence_id"]] = (_letter(lic, cust, lic["expiry_date"]), lic["expiry_date"])
            continue
        moved = (date.fromisoformat(lic["expiry_date"]) + timedelta(days=90)).isoformat()
        d.letters[lic["licence_id"]] = (_letter(lic, cust, moved, amended=True), moved)
        d.conflicts.append((lic["licence_id"], lic["expiry_date"], moved))
    d.policy = POLICY
    return d


def _letter(lic: dict, cust: dict, expiry: str, amended: bool = False) -> str:
    when = date.fromisoformat(expiry).strftime("%d %B %Y")
    note = ("\nThis letter amends the expiry date previously communicated for this licence.\n"
            if amended else "")
    return (f"# Licence terms: {lic['licence_id']}\n\n"
            f"Licensee: {cust['name']} ({cust['customer_id']})\n"
            f"Licence type: {lic['type']}\nRegion: {lic['region']}\n\n"
            f"Dear {cust['name']},\n\n"
            f"This letter confirms the terms of licence {lic['licence_id']}. The licence was issued on "
            f"{date.fromisoformat(lic['issued']).strftime('%d %B %Y')} and expires on {when}. "
            f"The annual fee is {lic['fee']:.2f} EUR, payable in advance.{note}\n"
            f"Renewal requests must reach us sixty days before expiry.\n")


#: The licensing policy: one section per topic, each with the sentence a question's answer needs.
POLICY = {
    "Early termination": "A licensee may terminate early with ninety days written notice; the fee for "
                         "the current year is not refunded.",
    "Renewal": "Renewal requests must be submitted sixty days before the expiry date, through the "
               "licensing portal.",
    "Suspension": "A licence is suspended when an invoice is more than forty-five days overdue, and "
                  "reinstated within two working days of payment.",
    "Transfer": "A licence can be transferred to an affiliate only with the written consent of the "
                "licensing team.",
    "Territory": "Each licence is valid only in the region named on it; use outside that region "
                 "requires a separate licence.",
    "Fees": "Fees are reviewed every January and capped at a five percent increase per year.",
    "Free pass": "A free pass gives a customer one event licence at no charge until its validity date.",
    "Audit": "The licensing team may audit usage once per calendar year with fourteen days notice.",
    "Data retention": "Order records are kept for seven years; archived orders are moved to the "
                      "archive database after two years.",
    "Disputes": "Disputes are first referred to mediation in Dublin before any court proceedings.",
    "Sublicensing": "Sublicensing is not permitted under any licence type.",
    "Education discount": "Education licences receive a thirty percent discount on the standard fee.",
    "Streaming caps": "Streaming licences cover up to fifty thousand concurrent viewers.",
    "Events": "An events licence covers a single venue for the dates stated on it.",
    "Contact": "Licensing questions go to the licensing desk, which answers within three working days.",
}


def policy_markdown() -> str:
    out = ["# Licensing policy\n", "Applies to every licence issued by the licensing team.\n"]
    for title, text in POLICY.items():
        out.append(f"\n## {title}\n\n{text}\n")
    return "".join(out)


def write(d: Domain, root: str) -> dict:
    """Write every source under `root`; returns {name: path}. The archive database is written too -
    the runner deletes it after registering it, to make it unreachable."""
    os.makedirs(os.path.join(root, "docs"), exist_ok=True)
    paths = {}
    p = os.path.join(root, "licences.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(d.licences[0]))
        w.writeheader()
        w.writerows(d.licences)
    paths["licences"] = p
    p = os.path.join(root, "customers.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"customers": d.customers}, f, indent=1)
    paths["customers"] = p
    for name, rows, cols in (("orders", d.orders, "order_id TEXT PRIMARY KEY, customer_id TEXT, status TEXT, amount REAL, created TEXT"),
                             ("archive", d.archive_orders, "order_id TEXT PRIMARY KEY, customer_id TEXT, amount REAL, year INTEGER")):
        p = os.path.join(root, f"{name}.sqlite")
        if os.path.exists(p):
            os.remove(p)
        con = sqlite3.connect(p)
        table = "orders" if name == "orders" else "archived_orders"
        con.execute(f"CREATE TABLE {table} ({cols})")
        keys = list(rows[0])
        con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in keys)})",
                        [tuple(r[k] for k in keys) for r in rows])
        con.commit()
        con.close()
        paths[name] = p
    try:
        import pyarrow as pa
        from opencortyx.stores.data import delta
        sales = os.path.join(root, "lake", "sales")
        if not delta.exists(sales):
            delta.append(sales, pa.Table.from_pylist(d.sales))
        paths["sales"] = sales
    except ImportError:                                          # writing data only, no app installed
        p = os.path.join(root, "sales.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(d.sales[0]))
            w.writeheader()
            w.writerows(d.sales)
        paths["sales"] = p
    p = os.path.join(root, "docs", "licensing-policy.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(policy_markdown())
    paths["policy"] = p
    for lic_id, (text, _) in d.letters.items():
        p = os.path.join(root, "docs", f"terms-{lic_id}.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    paths["letters"] = os.path.join(root, "docs")
    return paths
