"""XML parser — models XML's actual data model, not a dict approximation.

XML is not JSON with angle brackets, and the differences are exactly what a generic converter
destroys:

  * **Attributes are not children.** `<measure unit="kg">5</measure>` has a value AND a qualifier.
    Attributes are kept under an `@` prefix so both survive and stay distinguishable.
  * **Repeated tags are a collection.** `<entry/>` five times is five siblings, not one key
    overwritten four times. Repeats collapse into a list, which is what turns a 500-entry catalogue
    into 500 nodes.
  * **Mixed content is real.** Text alongside children (`<p>see <b>this</b> now</p>`) is preserved
    under `#text` rather than dropped.
  * **Namespaces are metadata, not part of the name.** ElementTree renders tags as
    `{urn:long:uri}entry`; left alone, every node label in the graph becomes unreadable. The local
    name is used and the namespace map is recorded separately.

RSS and Atom are XML dialects and parse here; they are separate feed *vocabularies*, not separate
wire formats.
"""
from __future__ import annotations

from ..base import Parser, ParseError, ParseResult, Record


def _localname(tag) -> str | None:
    if not isinstance(tag, str):
        return None                                # comments / processing instructions
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _namespace(tag) -> str:
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


class XmlParser(Parser):
    name = "xml"
    label = "XML"
    extensions = ("xml", "rss", "atom", "xsd", "xsl", "svg", "kml", "gpx", "cda", "xhtml")
    mime_types = ("application/xml", "text/xml", "application/rss+xml", "application/atom+xml")
    priority = 88

    #: Elements repeated at least this many times under one parent are treated as the document's
    #: record collection — the `<entry>`/`<item>`/`<row>` of a feed or export.
    RECORD_THRESHOLD = 2
    #: How deep to look for the record collection. Feeds nest one or two levels; anything deeper is
    #: a document tree, not a record list.
    _MAX_RECORD_DEPTH = 5

    @classmethod
    def sniff(cls, content: bytes) -> float:
        head = content[:512].lstrip()
        if not head.startswith(b"<"):
            return 0.0
        low = head[:512].lower()
        if b"<html" in low or b"<!doctype html" in low:
            return 0.0                             # HTML is prose; the document pipeline owns it
        if head.startswith(b"<?xml"):
            return 0.95
        return 0.7

    def _element(self, el, ns_seen: set) -> object:
        ns = _namespace(el.tag)
        if ns:
            ns_seen.add(ns)
        obj: dict = {}
        for k, v in (el.attrib or {}).items():
            obj[f"@{_localname(k)}"] = v
        for child in el:
            tag = _localname(child.tag)
            if tag is None:
                continue
            val = self._element(child, ns_seen)
            if tag in obj:
                if not isinstance(obj[tag], list):
                    obj[tag] = [obj[tag]]
                obj[tag].append(val)
            else:
                obj[tag] = val
        text = (el.text or "").strip()
        if text:
            if obj:
                obj["#text"] = text                # mixed content
            else:
                return text                        # a leaf is its value
        return obj

    def _find_records(self, node, path: str, depth: int = 0):
        """Deepest-first search for the largest repeated-element collection.

        Returns (records, tag, path). Bounded by `_MAX_RECORD_DEPTH` so a pathologically nested
        document cannot turn identification into a full-tree walk.
        """
        best: tuple = ([], "", path)
        if depth > self._MAX_RECORD_DEPTH or not isinstance(node, dict):
            return best
        for tag, val in node.items():
            if tag.startswith(("@", "#")):
                continue
            if isinstance(val, list) and len(val) >= self.RECORD_THRESHOLD:
                if len(val) > len(best[0]):
                    here = f"{path}/{tag}"
                    best = ([Record(data=v, index=i, location=f"{here}[{i}]", kind=tag)
                             for i, v in enumerate(val)], tag, here)
            elif isinstance(val, dict):
                inner = self._find_records(val, f"{path}/{tag}", depth + 1)
                if len(inner[0]) > len(best[0]):
                    best = inner
        return best

    def parse(self, content: bytes, filename: str = "") -> ParseResult:
        try:
            from defusedxml.ElementTree import fromstring        # entity-expansion safe
        except ImportError:
            from xml.etree.ElementTree import fromstring         # noqa: S405 — fallback
        try:
            root = fromstring(content)
        except Exception as e:                                   # noqa: BLE001
            raise ParseError(f"invalid XML: {str(e)[:200]}") from e

        ns_seen: set = set()
        body = self._element(root, ns_seen)
        root_name = _localname(root.tag) or "root"

        records, record_tag, record_path = self._find_records(body, f"/{root_name}")

        return ParseResult(
            data={root_name: body}, parser=self.name, records=records, root_name=root_name,
            metadata={"namespaces": sorted(ns_seen), "record_element": record_tag,
                      "record_path": record_path if records else "",
                      "root_element": root_name})
