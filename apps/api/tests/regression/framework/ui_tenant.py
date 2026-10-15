"""The throwaway tenant the UI suite signs in to, and the files it uploads.

    python -m tests.regression.framework.ui_tenant create   # → $RESULTS/ui/tenant.json + fixtures/
    python -m tests.regression.framework.ui_tenant drop     # deletes the tenant and everything in it

Users get generated passwords, recorded only in the (gitignored) results folder; the tenant exists
only for the run, so the browser never sees or changes an operator's data.
"""
from __future__ import annotations

import json
import secrets
import sys
import uuid

import httpx

from tests.regression.conftest import API, RESULTS
from tests.regression.framework import data as D

OUT = RESULTS / "ui"
FIXTURES = {
    "records.json": lambda: D.semi_bytes("json", D.SEMI_VARIANTS["record_array"][0]),
    "records.jsonl": lambda: D.semi_bytes("jsonl", D.SEMI_VARIANTS["record_array"][0]),
    "nested.yaml": lambda: D.semi_bytes("yaml", D.SEMI_VARIANTS["nested_object"][0]),
    "catalog.xml": lambda: D.semi_bytes("xml", D.SEMI_VARIANTS["record_array"][0]),
    "orders.csv": lambda: D.table_bytes("csv", *D.TABLE_VARIANTS["basic_5_rows"][:2]),
    "orders.tsv": lambda: D.table_bytes("tsv", *D.TABLE_VARIANTS["basic_5_rows"][:2]),
    "orders.xlsx": lambda: D.table_bytes("xlsx", *D.TABLE_VARIANTS["basic_5_rows"][:2]),
    "orders.parquet": lambda: D.table_bytes("parquet", *D.TABLE_VARIANTS["basic_5_rows"][:2]),
    "notes.md": lambda: D.doc_bytes("md", D.DOC_VARIANTS["normal"][1]),
    "notes.txt": lambda: D.doc_bytes("txt", D.DOC_VARIANTS["normal"][1]),
    "report.pdf": lambda: D.doc_bytes("pdf", D.DOC_VARIANTS["normal"][1]),
    "report.docx": lambda: D.doc_bytes("docx", D.DOC_VARIANTS["normal"][1]),
    "broken.json": lambda: b'{"a": [1, 2,',
    "empty.csv": lambda: b"",
}


def create() -> dict:
    from opencortyx.core.security.auth import create_access_token
    org = uuid.uuid4()
    admin = {"Authorization": f"Bearer {create_access_token(uuid.uuid4(), org, 'admin')}"}
    users = {}
    with httpx.Client(base_url=f"{API}/api/v2", headers=admin, timeout=60) as c:
        for role in ("admin", "editor", "viewer"):
            email, pw = f"ui-{role}-{org.hex[:8]}@example.test", secrets.token_urlsafe(18)
            r = c.post("/users", json={"email": email, "password": pw, "role": role})
            r.raise_for_status()
            users[role] = {"email": email, "password": pw}
        key = c.post("/apikeys", json={"app": "ui-regression", "role": "admin"}).json()["key"]
    fx = OUT / "fixtures"
    fx.mkdir(parents=True, exist_ok=True)
    for name, make in FIXTURES.items():
        (fx / name).write_bytes(make())
    info = {"org": str(org), "users": users, "api_key": key, "fixtures": sorted(FIXTURES)}
    (OUT / "tenant.json").write_text(json.dumps(info, indent=1))
    return info


def drop() -> None:
    from tests.conftest import _drop_org
    info = json.loads((OUT / "tenant.json").read_text())
    _drop_org(uuid.UUID(info["org"]))
    (OUT / "tenant.json").unlink()


if __name__ == "__main__":
    {"create": lambda: print(json.dumps({"org": create()["org"]})), "drop": drop}[sys.argv[1]]()
