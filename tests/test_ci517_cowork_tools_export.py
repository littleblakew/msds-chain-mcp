"""CI-517: the Cowork tool-description export must be in MCP wire shape.

Microsoft reads this file out of the submitted package and has no runtime discovery, so
anything wrong in it is wrong until the next submission — and wrong quietly, because a
key Copilot does not recognise is simply ignored rather than rejected.

The specific trap: mcp 2.x exposes `input_schema` / `read_only_hint` on the python model
while the wire format (and every previously shipped package) uses `inputSchema` /
`readOnlyHint`. `model_dump()` defaults to the python names, so the snake_case version is
one forgotten argument away and looks completely normal in a diff. It was in fact produced
on the first attempt at regenerating this file on 2026-08-19.
"""
import asyncio

import pytest

from scripts.export_cowork_tools import build


@pytest.fixture(scope="module")
def exported():
    return asyncio.run(build())


def test_every_tool_carries_the_wire_schema_key(exported):
    tools = exported["tools"]
    assert tools, "export produced no tools at all"
    for t in tools:
        assert "inputSchema" in t, f"{t['name']}: wire key must be inputSchema"
        assert "input_schema" not in t, f"{t['name']}: python attr name leaked into the file"


def test_annotation_hints_use_wire_casing(exported):
    """Copilot reads readOnlyHint. A file full of read_only_hint parses fine and silently
    tells Copilot nothing about which of our tools are safe to call speculatively."""
    annotated = [t for t in exported["tools"] if t.get("annotations")]
    assert annotated, "no tool carried annotations — the hints are how Copilot judges safety"
    for t in annotated:
        for key in t["annotations"]:
            assert "_" not in key, (
                f"{t['name']}: annotation key {key!r} is snake_case; the wire format is "
                "camelCase (pass by_alias=True to model_dump)")


def test_export_is_json_serialisable_and_complete(exported):
    """The export is piped straight into the package; a non-serialisable value would be
    discovered at submission time, not here."""
    import json
    json.dumps(exported, ensure_ascii=False)
    for t in exported["tools"]:
        assert t["name"] and t["description"], f"{t.get('name')}: empty name/description"
        assert t["inputSchema"].get("type") == "object", t["name"]
