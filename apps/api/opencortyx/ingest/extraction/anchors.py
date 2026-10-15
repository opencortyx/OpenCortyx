"""Anchor rules — attach ingested files to nodes that ALREADY exist.

The problem
-----------
Entity extraction and resolution link a document to an existing node only if the TEXT mentions it,
by name and, after the asset, by meaning (`opencortyx.resolution`). That is inference, and it
fails exactly where the user already knows the answer: a folder of one researcher's papers belongs
to that researcher whether or not their name appears in any given PDF.

An anchor states the link instead of inferring it: "everything under `research/alice/` is authored
by this Person node". It is ADDITIVE — entities inside each file still resolve normally, so you get
the declared edge and the inferred ones.

A rule answers two independent questions
----------------------------------------
  * WHOSE is this file? → `node_id` / `resolve_as` + the edge. That is a claim about another node.
  * WHAT IS this file?  → `as_type`. The asset itself is retyped, so an Aadhaar scan ingests as an
    `Aadhaar` node rather than a generic `Source`, and `Person -HAS_ID-> Aadhaar` is a shape the
    graph can be QUERIED on ("everyone missing a PAN"). Without it, every format under every folder
    collapses into one undifferentiated `Source` type and only the edge name carries the meaning.

Either half can be used alone. `aadhaar/** → as_type: Aadhaar` with no target is a valid rule: it
says what the folder contains without claiming whose it is. Retyping is applied on MATCH, not on
resolution — the folder names the document KIND, which is true whether or not the owner resolves.

Two kinds of TARGET, and the difference is confidence
-----------------------------------------------------
  * EXPLICIT — the rule names a node id. You picked it; it is certain.
  * DERIVED  — the rule captures part of the path (`research/{person}/**`) and hands that name to
    the resolver. One rule then covers a hundred researchers, and a new folder next week needs no
    config change. But the name came from a directory, so it can be a typo: a derived anchor is
    written with lower confidence (0.7) than an explicit one.

Matching semantics (each of these was a real decision):
  * ALL matching rules apply, not first-match. A paper genuinely is authored by Alice AND is a PDF
    AND is about a topic; edges are a set. First-match would silently drop the second relationship.
  * No match is not an error. The file ingests normally under entity resolution, and the miss is
    COUNTED so a wrong glob is visible instead of silent.
  * Paths are matched case-insensitively with `/` separators, because the same corpus arrives from
    Windows shares, S3 keys and POSIX mounts and must not match differently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from opencortyx.core.substrate import NodeRepo

#: A capture name has to be a valid regex group name, and we surface a clear error rather than let
#: `re` raise something opaque about group syntax at ingestion time.
_CAPTURE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_ .\-]{0,79}$")


class AnchorRuleError(ValueError):
    """A rule that cannot be compiled — reported when the rule is SAVED, not when files ingest."""


@dataclass(frozen=True)
class AnchorRule:
    match: str
    edge_type: str = "RELATED_TO"
    node_id: str | None = None          # explicit target
    resolve_as: str | None = None       # derived: entity type for the captured name (e.g. "Person")
    capture: str | None = None          # derived: which capture group holds the name
    create_missing: bool = False        # mint the node when a derived name resolves to nothing
    as_type: str | None = None          # what the ASSET is — retypes it (e.g. Source → "Aadhaar")

    @property
    def is_derived(self) -> bool:
        """The target NAME comes from the path rather than from a node you picked.

        Reads the compiled pattern instead of `node_id is None`, because a rule may now name no
        node at all and still not be derived — an `as_type`-only rule has nothing to resolve.
        """
        return self.node_id is None and bool(_to_regex(self.match).groupindex)

    @property
    def is_type_only(self) -> bool:
        """States WHAT the file is without saying whose it is → retype, no edge."""
        return self.node_id is None and not _to_regex(self.match).groupindex


@dataclass
class AnchorHit:
    rule: AnchorRule
    node_id: str | None = None          # set for explicit rules
    name: str | None = None             # set for derived rules — the captured text
    captures: dict = field(default_factory=dict)


@lru_cache(maxsize=1024)
def _to_regex(pattern: str) -> re.Pattern:
    """Glob (+ `{capture}` groups) → regex.

    Written by hand rather than with `fnmatch` because fnmatch has no notion of a path separator:
    its `*` happily crosses `/`, so `research/*/paper.pdf` would match
    `research/alice/drafts/paper.pdf`. That difference is the whole point of a folder rule.

    Cached because it is called per RULE per FILE — a 10,000-file run against a handful of rules
    would otherwise recompile the same few patterns tens of thousands of times — and because the
    rule properties below read `groupindex` off it, which must stay free.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "{":
            close = pattern.find("}", i)
            if close == -1:
                raise AnchorRuleError(f"unclosed '{{' in pattern: {pattern!r}")
            name = pattern[i + 1:close].strip()
            if not _CAPTURE_NAME.match(name):
                raise AnchorRuleError(
                    f"capture name {name!r} must start with a letter and contain only "
                    f"letters, digits or underscores")
            out.append(f"(?P<{name}>[^/]+)")
            i = close + 1
        elif pattern.startswith("**/", i):
            out.append("(?:.*/)?")      # zero OR more directories — `a/**/b` must still match `a/b`
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def normalize_path(path: str) -> str:
    """One path shape for matching, whatever the source produced.

    Backslashes become `/` (Windows shares), leading `./` and `/` are dropped (S3 keys and absolute
    mounts describing the same relative location), so a single rule works across connectors.
    """
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def parse_rules(raw: list | None) -> list[AnchorRule]:
    """Config dicts → rules, rejecting bad ones LOUDLY.

    Validation belongs here, at save time, because the alternative is discovering a malformed glob
    part-way through a 10,000-file ingestion where it can only be reported as a per-file failure.
    """
    rules: list[AnchorRule] = []
    for i, r in enumerate(raw or []):
        if not isinstance(r, dict):
            raise AnchorRuleError(f"rule {i} is not an object")
        match = (r.get("match") or "").strip()
        if not match:
            raise AnchorRuleError(f"rule {i} has no `match` pattern")
        node_id = (r.get("node_id") or "").strip() or None
        resolve_as = (r.get("resolve_as") or "").strip() or None
        capture = (r.get("capture") or "").strip() or None
        as_type = (r.get("as_type") or "").strip() or None
        if as_type and not _TYPE_NAME.match(as_type):
            raise AnchorRuleError(
                f"rule {i}: {as_type!r} is not a usable node type — start with a letter and keep it "
                f"under 80 characters (letters, digits, spaces, _ . -)")
        rule = AnchorRule(
            match=match,
            edge_type=(r.get("edge_type") or "RELATED_TO").strip() or "RELATED_TO",
            node_id=node_id,
            resolve_as=resolve_as,
            capture=capture,
            create_missing=bool(r.get("create_missing", False)),
            as_type=as_type,
        )
        rx = _to_regex(rule.match)                    # compile now so a bad glob fails at save time
        if node_id is None:
            names = list(rx.groupindex)
            if not names and not rule.as_type:
                raise AnchorRuleError(
                    f"rule {i} names no node, captures nothing and sets no type — add a node, a "
                    f"{{capture}} group such as research/{{person}}/**, or a type for the files "
                    f"themselves")
            if rule.capture and rule.capture not in names:
                raise AnchorRuleError(
                    f"rule {i} captures {names} but is configured to use {rule.capture!r}")
        rules.append(rule)
    return rules


def _match_anywhere(rx, p: str):
    """Match the rule against the path, or against any SUFFIX of it that starts at a folder boundary.

    Rules are written the way people describe their folders — `ids/{person}/aadhaar/**`, exactly as the
    UI's own example shows — while a real ingestion hands over whatever the connector produced:
    `/data/ids/priya/aadhaar/scan.pdf`, `share/exports/ids/priya/aadhaar/scan.pdf`. `re.match` anchors
    at the start, so those rules matched NOTHING and the feature looked broken to anyone who didn't
    happen to mount their files at the filesystem root.

    Suffix matching is what "files under an ids/<person>/aadhaar folder" actually means. The candidate
    starts are folder boundaries only, so `paids/x/aadhaar/…` can never satisfy an `ids/…` rule. A rule
    that IS absolute (`/data/**`) still normalises to a leading segment and matches at position 0 first,
    so nothing that worked before changes.
    """
    m = rx.match(p)
    if m:
        return m
    i = p.find("/")
    while i != -1:
        m = rx.match(p[i + 1:])
        if m:
            return m
        i = p.find("/", i + 1)
    return None


def match_path(path: str, rules: list[AnchorRule]) -> list[AnchorHit]:
    """Every anchor this one file earns. Empty list = ingest normally (never an error)."""
    p = normalize_path(path)
    hits: list[AnchorHit] = []
    for rule in rules:
        m = _match_anywhere(_to_regex(rule.match), p)
        if not m:
            continue
        caps = {k: v for k, v in (m.groupdict() or {}).items() if v}
        if rule.node_id:
            hits.append(AnchorHit(rule=rule, node_id=rule.node_id, captures=caps))
        elif rule.is_type_only:
            # No target at all — the rule only says what the file IS. Still a hit, because the
            # retype is the whole point of it; `apply` skips the edge for these.
            hits.append(AnchorHit(rule=rule, captures=caps))
        else:
            # Default to the sole capture when the rule did not say which — the overwhelmingly
            # common shape is one group, and requiring `capture` there is friction with no payoff.
            key = rule.capture or (next(iter(caps)) if len(caps) == 1 else None)
            name = caps.get(key) if key else None
            if not name:
                if not rule.as_type:
                    continue
                hits.append(AnchorHit(rule=rule, captures=caps))
                continue
            hits.append(AnchorHit(rule=rule, name=name, captures=caps))
    return hits


def preview(paths: list[str], rules: list[AnchorRule]) -> dict:
    """What these rules WOULD do, without ingesting anything.

    Exists because rules are otherwise configured blind: a mistyped glob is invisible until after an
    ingestion, and by then it has either written thousands of wrong edges or none at all. Pure path
    matching, so it is cheap enough to run on every keystroke in the editor.
    """
    per_rule: list[dict] = [
        {"match": r.match, "edge_type": r.edge_type, "derived": r.is_derived,
         "files": 0, "names": set(),
         # Surfaced so the editor can say "412 files become Aadhaar" — the retype is the half of a
         # rule with no other visible symptom until after a run.
         "as_type": r.as_type, "type_only": r.is_type_only,
         "samples": []} for r in rules
    ]
    matched = 0
    unmatched: list[str] = []
    for path in paths:
        hits = match_path(path, rules)
        if not hits:
            unmatched.append(normalize_path(path))
            continue
        matched += 1
        for h in hits:
            slot = per_rule[rules.index(h.rule)]
            slot["files"] += 1
            if h.name:
                slot["names"].add(h.name)
            if len(slot["samples"]) < 3:
                slot["samples"].append({"path": normalize_path(path), "name": h.name})
    for slot in per_rule:
        slot["distinct_names"] = len({n.casefold() for n in slot["names"]})
        slot["names"] = sorted(slot["names"])[:20]    # bounded: this is a preview, not an export
    return {
        "total": len(paths),
        "matched": matched,
        "unmatched": len(unmatched),
        # A sample, not the whole tail — the count is the signal; the examples are for spotting the
        # pattern you forgot.
        "unmatched_sample": unmatched[:20],
        "rules": per_rule,
    }


# ── runtime: turn hits into edges ────────────────────────────────────────────────────────────────

async def _existing_node(*, workspace_id, name: str, etype: str | None):
    """Find an already-known node for a derived name WITHOUT creating one.

    `named_node` is an upsert — it mints a node when nothing matches, which is precisely what
    `create_missing=False` must not do. A typo'd folder would otherwise silently become a new
    Person, and the graph fills with near-duplicates that are far harder to remove than to prevent.
    """
    found = await NodeRepo().find(workspace_id, label=(name or "").strip(), entity_type=etype or None, limit=1)
    return found[0] if found else None


async def _retype_asset(db, *, workspace_id, node_id, as_type: str, rule: str) -> bool:
    """Make the asset BE what the folder says it is: a generic `Source` becomes an `Aadhaar`.

    The type it was ingested as is kept in `__base_type`, which every "the ingested assets" query
    reads through, so a retyped file stays an asset. The type is also registered in the ontology as a
    candidate, so it can be picked in type dropdowns.
    """
    from opencortyx.ontology.extractors import register_emit_types

    repo = NodeRepo()
    node = await repo.get(workspace_id, node_id)
    if node is None:
        return False
    props = dict(node.properties or {})
    if node.type != as_type:
        # setdefault: a re-ingest rewrites the node as "Source" and retypes it again
        props.setdefault("__base_type", node.type)
    props["__typed_by"] = rule
    node.properties = props
    node.type = as_type
    await repo.save(node)
    await register_emit_types(db, workspace_id, {"nodes": [{"type": as_type}]}, source="anchor")
    return True


async def apply(db, *, workspace_id, asset_node_id, path: str, rules: list[AnchorRule],
                collection_id=None) -> dict:
    """Type this asset and create its anchor edges. Never raises — a bad rule must not fail an
    ingestion.

    Edge direction is anchor → asset (`Alice -AUTHORED-> paper.pdf`), which is how the relationship
    reads in the ontology and keeps traversal from the person to their work one hop.
    """
    import uuid as _uuid
    from opencortyx.ingest.extraction.templates import named_node

    out = {"edges": 0, "unresolved": [], "matched_rules": 0,
           "typed_as": None, "type_conflicts": []}
    if not rules or not asset_node_id:
        return out
    for hit in match_path(path, rules):
        out["matched_rules"] += 1
        rule = hit.rule

        if rule.as_type:
            if out["typed_as"] is None:
                if await _retype_asset(db, workspace_id=workspace_id, node_id=asset_node_id, as_type=rule.as_type,
                                       rule=rule.match):
                    out["typed_as"] = rule.as_type
            elif rule.as_type != out["typed_as"]:
                out["type_conflicts"].append({"rule": rule.match, "as_type": rule.as_type,
                                              "kept": out["typed_as"]})
        if rule.is_type_only or (not hit.node_id and not hit.name):
            # Either the rule never had a target, or its capture came back empty on this path (an
            # `as_type` rule is still a hit in that case — see match_path). Nothing to point at.
            continue

        target = None
        if hit.node_id:
            try:
                target = _uuid.UUID(str(hit.node_id))
            except (ValueError, AttributeError):
                out["unresolved"].append({"rule": rule.match, "reason": "invalid node id"})
                continue
        else:
            row = await _existing_node(workspace_id=workspace_id, name=hit.name, etype=rule.resolve_as)
            if row is not None:
                target = row.id
            elif rule.create_missing:
                target = await named_node(workspace_id, hit.name, entity_type=rule.resolve_as,
                                          collection_id=collection_id)
            else:
                # Counted, not silent: an unmatched derived name is the signal that a folder is
                # named differently from the entity it refers to.
                out["unresolved"].append({"rule": rule.match, "name": hit.name})
                continue
        props = {"__anchor": True, "__rule": rule.match, "__derived": rule.is_derived}
        ok = await NodeRepo().add_edge(workspace_id=workspace_id, source_id=target, target_id=asset_node_id,
            type=rule.edge_type, properties=props,
            confidence=1.0 if not rule.is_derived else 0.7)
        if ok:
            out["edges"] += 1
    return out
