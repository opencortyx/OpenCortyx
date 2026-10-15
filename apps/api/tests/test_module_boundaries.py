"""The dependency rules that keep the layering from rotting back.

These are enforced as tests rather than documented as a convention because the violations they
catch are invisible in review: one `from ...core.db import V2Session` inside a domain module looks
like every other import, and nothing fails until the import graph has a cycle in it again. Import
direction is a property of the whole package, so only a whole-package check can hold it.
"""
from __future__ import annotations

import ast
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1] / "opencortyx"
APP = "opencortyx"


def _module_name(path: Path) -> str:
    rel = path.relative_to(PKG_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    """Every `opencortyx.*` module this file imports, at module level or inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mod = _module_name(path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = ".".join(mod.split(".")[: -node.level])
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            if target.startswith("opencortyx"):
                found.add(target)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("opencortyx"))
    return found


def _py_files(*parts: str) -> list[Path]:
    return [p for p in (PKG_ROOT.joinpath(*parts)).rglob("*.py") if "__pycache__" not in p.parts]


def test_domain_imports_nothing_from_the_application():
    """`domain` is the bottom of the graph: it defines what a row IS and depends on no layer.

    The moment a domain module imports `core`, `core` can no longer own a `User` without a cycle,
    which is exactly the knot this package was created to cut.
    """
    offenders = {}
    for path in _py_files("domain"):
        bad = {i for i in _imports(path)
               if i.startswith(APP) and not i.startswith(f"{APP}.domain")}
        if bad:
            offenders[_module_name(path)] = sorted(bad)
    assert not offenders, (
        "domain must not import from the application layers; move the definition it needs into "
        f"domain, or move the code that needs it out of domain: {offenders}")


def test_core_does_not_import_the_layers_built_on_top_of_it():
    """`core` is infrastructure the capabilities are built FROM, so it may not reach back into them.

    There is no allowlist any more. The last two entries — asset_purge and progress — were
    ingest-shaped jobs sitting in core for historical reasons, and they now live in `ingest/`.
    An empty list is the point: a new violation has nowhere to hide.
    """
    forbidden = tuple(f"{APP}.{layer}" for layer in (
        "ingest", "retrieval", "capabilities", "ontology", "knowledge", "drafting", "tasks", "api",
        "bootstrap", "main"))
    offenders = {}
    for path in _py_files("core"):
        bad = sorted(i for i in _imports(path) if i.startswith(forbidden))
        if bad:
            offenders[_module_name(path)] = bad
    assert not offenders, (
        f"core must not import the layers above it: {offenders}")


def test_every_table_is_registered_on_the_one_metadata():
    """One declarative base, or Alembic autogenerate silently stops seeing half the schema."""
    from opencortyx import domain

    names = {t.name for t in domain.V2Base.metadata.tables.values()}
    schemas = {t.schema for t in domain.V2Base.metadata.tables.values()}
    assert schemas == {domain.V2_SCHEMA}, f"tables outside {domain.V2_SCHEMA}: {schemas}"
    assert {"app_user", "collection", "policy", "type_registry", "ingest_run"} <= names


def test_bootstrap_is_the_only_place_that_composes_start_up():
    """Creating the schema is infrastructure; choosing what a fresh install ships with is not.

    If `core.db` seeds the ontology again, `core` and the ontology import each other and neither can
    be loaded or tested alone — which is the knot `bootstrap` exists to keep untied.
    """
    from opencortyx import bootstrap
    from opencortyx.core import db

    assert hasattr(bootstrap, "init_v2_schema")
    assert not hasattr(db, "init_v2_schema"), "init_v2_schema belongs in bootstrap, not core.db"
    assert hasattr(db, "create_schema_and_tables")


def test_the_layer_checks_see_the_code_they_guard():
    """A layer rule over a path that no longer exists passes forever while checking nothing.

    That happened: after `arch_api_v2/` was flattened away, both checks above scanned zero files."""
    assert _py_files("domain") and _py_files("core")


def test_no_package_shadows_a_module_of_the_same_name():
    """`foo/` beside `foo.py` makes `foo.py` unimportable, silently.

    Python resolves `from opencortyx import authoring` to the PACKAGE when both exist, so every
    caller of the module's functions gets `AttributeError` at call time, not at import — this is
    exactly how an `authoring/` drafting package once took down node creation, the glossary and
    memory writes while every test of the new package passed.
    """
    clashes = sorted(
        str(p.relative_to(PKG_ROOT))
        for p in PKG_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts and p.stem != "__init__"
        and p.with_suffix("").is_dir() and (p.with_suffix("") / "__init__.py").exists())
    assert not clashes, f"a package hides these modules: {clashes}"
