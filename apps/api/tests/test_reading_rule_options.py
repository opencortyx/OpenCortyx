"""Step-1 options shared by every pipeline kind: accept newly detected shapes, and extract only."""
import pytest

from opencortyx.ingest.pipelines import signature
from opencortyx.ingest.structured.ingest import structured_step1


@pytest.mark.parametrize("cfg, bound, expected", [
    ({}, False, True),                        # nothing bound: the default walker reads everything
    ({"auto_accept": False}, False, True),    # nothing bound, option off: still the default walker
    ({}, True, True),                         # the option defaults to on
    ({"auto_accept": True}, True, True),      # bound, option on: unmatched sources use the default walker
    ({"auto_accept": False}, True, False),    # bound, option off: the mapping is an allow-list
    ({"auto_accept": "off"}, True, False),
])
def test_default_walker_reads_sources_no_mapping_matches(cfg, bound, expected):
    assert signature.auto_create_on(cfg, bound=bound) is expected


@pytest.mark.parametrize("cfg, expected", [
    ({}, False),
    ({"extract_only": True}, True),
    ({"extract_only": "false"}, False),
    ({"extract_only": ""}, False),
    ({"structure_only": True}, True),         # semi-structured spelling saved before the unified option
    ({"save_chunks": False}, True),           # document spelling saved before the unified option
    ({"save_chunks": True}, False),
])
def test_extract_only_option(cfg, expected):
    assert signature.extract_only_on(cfg) is expected


def _pipeline(config):
    return [{"id": "stage_0", "type": "base_schema", "action": "base_schema", "config": config}]


def test_structured_reading_rule_uses_the_default_mode_and_keeps_the_options(monkeypatch):
    monkeypatch.setattr("opencortyx.core.config.runtime.structured_default_mode", lambda: "row")
    step1 = structured_step1(_pipeline({"mapping_ids": [], "auto_accept": False}))
    assert step1["mode"] == "row"
    assert step1["auto_accept"] is False


def test_structured_extract_only_reads_the_schema():
    step1 = structured_step1(_pipeline({"extract_only": True, "mode": "row"}))
    assert step1["mode"] == "catalog"
    assert step1["extract_only"] is True
