"""Metadata a file format carries outside the text: Office core properties, HTML <meta>, an EPUB's
package, a notebook's metadata, Markdown front matter, an RST field list, RTF's info group.

Each reader records what its format holds on the assembler (`asm.metadata`) while it has the file
parsed, so nothing is read twice. Keys are snake_case; values are short strings. What the document
says in its own header block is read afterwards, for every format alike (layout/metadata.py), and
the two are merged there.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

MAX_VALUE = 300
#: HTML <meta> names about the page's delivery, not the document.
_HTML_SKIP = {"viewport", "robots", "generator", "theme_color", "format_detection", "referrer",
              "color_scheme", "csrf_token", "msapplication_tilecolor", "apple_mobile_web_app_capable",
              "twitter_card", "google_site_verification", "og_type", "og_image", "og_url", "og_site_name"}


def key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")


def value(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x).strip() for x in v if str(x).strip())
    text = " ".join(str(v).split())
    return text[:MAX_VALUE] or None


def put(meta: dict, name: str, v: Any) -> None:
    """Record one field, keeping the first value a format gave for it."""
    k, val = key(name), value(v)
    if k and val and k not in meta:
        meta[k] = val


def office_core(props) -> dict:
    """Word and PowerPoint core properties (python-docx / python-pptx `core_properties`)."""
    meta: dict = {}
    for attr, name in (("title", "title"), ("author", "author"), ("subject", "subject"),
                       ("keywords", "keywords"), ("category", "category"), ("identifier", "identifier"),
                       ("created", "created"), ("modified", "modified"), ("version", "version"),
                       ("comments", "description")):
        try:
            put(meta, name, getattr(props, attr, None))
        except Exception:                                   # noqa: BLE001 — a malformed property is skipped
            continue
    return meta


def html_meta(soup) -> dict:
    """An HTML page's <title> and <meta name|property content> pairs (description, author, keywords,
    Dublin Core, citation_*, og:title)."""
    meta: dict = {}
    if soup.title and soup.title.string:
        put(meta, "title", soup.title.string)
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property") or ""
        k = key(name)
        if not k or k in _HTML_SKIP or not tag.get("content"):
            continue
        put(meta, k.replace("og_title", "title").replace("dc_", "").replace("citation_", ""), tag.get("content"))
    return meta


def opf_meta(opf: str) -> dict:
    """An EPUB package's Dublin Core metadata."""
    meta: dict = {}
    for name, v in re.findall(r"<dc:([a-z]+)[^>]*>(.*?)</dc:\1>", opf or "", re.IGNORECASE | re.DOTALL):
        put(meta, {"creator": "author"}.get(name.lower(), name), re.sub(r"<[^>]+>", " ", v))
    return meta


def notebook_meta(nb: dict) -> dict:
    meta: dict = {}
    md = nb.get("metadata") or {}
    put(meta, "title", md.get("title"))
    authors = md.get("authors")
    if isinstance(authors, list):
        put(meta, "author", [a.get("name") if isinstance(a, dict) else a for a in authors])
    put(meta, "kernel", (md.get("kernelspec") or {}).get("display_name"))
    put(meta, "language", (md.get("language_info") or {}).get("name"))
    return meta


def front_matter(text: str) -> tuple[dict, str]:
    """A Markdown file's leading `---` YAML block, and the text after it."""
    m = re.match(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", text, re.DOTALL)
    if not m:
        return {}, text
    meta: dict = {}
    try:
        import yaml
        data = yaml.safe_load(m.group(1))
    except Exception:                                       # noqa: BLE001 — not YAML: it is text
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    for k, v in data.items():
        if isinstance(v, (str, int, float, date, datetime)) or (isinstance(v, list) and all(
                isinstance(x, (str, int, float)) for x in v)):
            put(meta, str(k), v)
    return meta, text[m.end():]


def rst_docinfo(text: str) -> dict:
    """A reStructuredText document's leading field list (`:Author: ...`), its docinfo."""
    meta: dict = {}
    for line in text.lstrip("﻿").splitlines()[:60]:
        m = re.match(r"^:([A-Za-z][\w -]{0,40}):\s+(.+)$", line)
        if m:
            put(meta, m.group(1), m.group(2))
        elif meta and line.strip() and not line.startswith((" ", "\t")):
            break
    return meta


def rtf_info(raw: str) -> dict:
    """An RTF file's \\info group: {\\title ...}{\\author ...}{\\subject ...}."""
    meta: dict = {}
    for name in ("title", "author", "subject", "keywords", "company"):
        m = re.search(r"\{\\" + name + r"\s+([^{}]*)\}", raw or "")
        if m:
            put(meta, name, m.group(1))
    return meta
