"""The format catalog is the one list of supported formats; these keep it honest."""
import importlib

from opencortyx.ingest.detection.registry import registry
from opencortyx.ingest.formats import catalog, semistructured


def test_every_family_names_a_reader_that_exists():
    for f in catalog.FAMILIES:
        importlib.import_module("opencortyx.ingest.formats." + f.reader.replace("/", "."))


def test_every_semistructured_extension_is_claimed_by_its_parser():
    owners = semistructured.extensions()
    for f in catalog.FAMILIES:
        if f.route == catalog.SEMISTRUCTURED:
            assert semistructured.get(f.decoder), f"{f.name}: no parser named {f.decoder!r}"
            for ext in f.extensions:
                assert owners.get(ext) == f.decoder, f".{ext} is read by {owners.get(ext)!r}, not {f.decoder!r}"


def test_each_extension_is_declared_once():
    exts = [e for f in catalog.FAMILIES for e in f.extensions]
    assert len(exts) == len(set(exts)) == len(catalog.FORMATS)


def test_detection_is_built_from_the_catalog():
    assert {d.name for d in registry.all_formats()} == {f.name for f in catalog.FAMILIES}
    for ext, fmt in catalog.FORMATS.items():
        assert registry.get_by_extension(ext) is not None, f".{ext} missing from detection"
