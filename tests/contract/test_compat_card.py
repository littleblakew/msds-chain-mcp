r"""The compatibility card (MCP Apps / ChatGPT Apps SDK): the template is registered, the tool
points at it, and the tool result carries the card payload in `_meta`, the one channel the
host's model cannot rewrite.

| guard | what reverting makes it fail |
|---|---|
| `test_tool_points_at_the_template` | drop `meta=ui_compat_card.tool_meta()` from the tool decorator |
| `test_template_is_served_with_the_mcp_app_mime` | drop the `@mcp.resource(...)` registration or change its MIME |
| `test_card_payload_survives_the_usage_rebuild` | drop `meta=result.meta` in `_with_usage` |
| `test_card_payload_survives_the_language_note_rebuild` | drop `meta=` in `_relay_language_note` |
| `test_meta_goes_on_the_wire_as_underscore_meta` | (SDK) the field serializing under another name |
| `test_dockerfile_ships_the_module` | drop `ui_compat_card.py` from the Dockerfile COPY line |

Async cases use `asyncio.run` (no pytest-asyncio in requirements-dev.txt, see test_response_kind.py).
"""
import asyncio
from pathlib import Path

import server
import ui_compat_card as card

_PAYLOAD = {
    "pairs": [{"chem1": "thionyl chloride", "chem2": "ethanol", "level": "incompatible",
               "reason": "r", "citation": "Supplier X SDS rev 2020-01-01",
               "traceability": "rule_based"}],
    "unresolved": [],
    "precursor_disclosure": [{"query_name": "thionyl chloride",
                              "tier": "CWC Schedule 3B", "statement": "Listed on CWC Schedule 3B."}],
    "documents": [
        {"chemical": "thionyl chloride", "chemical_name": "Thionyl chloride", "supplier": "Supplier X",
         "revision_date": "2020-01-01", "sds_document_url": card.SDS_LINK_ORIGIN + "/msds/token/a"},
        {"chemical": "ethanol", "chemical_name": "Ethanol", "supplier": "Supplier Y",
         "revision_date": "2021-01-01", "sds_document_url": card.SDS_LINK_ORIGIN + "/msds/token/b"},
        {"chemical": "water", "chemical_name": "Water", "supplier": "Supplier Z",
         "revision_date": "2022-01-01", "sds_document_url": card.SDS_LINK_ORIGIN + "/msds/token/c"},
    ],
    "_usage": {"cost": 3, "balance": 9},
}


def _fake(payload):
    async def _f(*a, **kw):
        return payload
    return _f


def _call(monkeypatch, payload=_PAYLOAD, **kw):
    monkeypatch.setattr(server, "_direct_compat", _fake(payload))
    return asyncio.run(server.check_chemical_compatibility(
        chemicals=["thionyl chloride", "ethanol"], **kw))


def test_tool_points_at_the_template():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    meta = tools["check_chemical_compatibility"].meta or {}
    assert meta.get("ui", {}).get("resourceUri") == card.CARD_URI, meta
    assert meta.get("openai/outputTemplate") == card.CARD_URI, meta


def test_template_is_served_with_the_mcp_app_mime():
    contents = list(asyncio.run(server.mcp.read_resource(card.CARD_URI)))
    assert contents, "template resource not registered"
    first = contents[0]
    assert getattr(first, "mime_type", None) == card.CARD_MIME, first
    assert "ui/notifications/tool-result" in str(getattr(first, "content", "")), \
        "template does not listen for the MCP Apps tool-result notification"


def test_card_payload_survives_the_usage_rebuild(monkeypatch):
    result = _call(monkeypatch)
    assert result.structured_content.get("usage"), "precondition: the usage rebuild ran"
    payload = (result.meta or {}).get(card.CARD_META_KEY)
    assert payload, f"card payload dropped: {result.meta}"
    assert payload["pairs"][0]["a"] == "thionyl chloride", payload["pairs"]
    assert payload["pairs"][0]["verdict"] == "incompatible"
    assert payload["precursor"][0]["statement"] == "Listed on CWC Schedule 3B."
    # at most two calls to action on an inline card
    assert [d["url"] is not None for d in payload["documents"]] == [True, True, False]


def test_card_payload_survives_the_language_note_rebuild(monkeypatch):
    payload = {**_PAYLOAD, "language_note": "Parts of this answer are in English."}
    result = _call(monkeypatch, payload=payload, lang="ja")
    assert result.content[0].text.count("Parts of this answer are in English.") >= 1, \
        "precondition: the language-note relay rewrote the result"
    assert (result.meta or {}).get(card.CARD_META_KEY), f"card payload dropped: {result.meta}"


def test_meta_goes_on_the_wire_as_underscore_meta(monkeypatch):
    wire = _call(monkeypatch).model_dump(by_alias=True, exclude_none=True)
    assert card.CARD_META_KEY in wire.get("_meta", {}), sorted(wire)


def test_malformed_entries_are_skipped_not_raised():
    out = card.card_payload({"pairs": ["x", None], "precursor_disclosure": "nope",
                             "documents": [1, {"supplier": "S"}]})
    assert out["pairs"] == [] and out["precursor"] == []
    assert out["documents"] == [{"chemical": None, "supplier": "S", "revision_date": None, "url": None}]


def test_dockerfile_ships_the_module():
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    copy_lines = [line for line in dockerfile.splitlines() if line.startswith("COPY")]
    assert any("ui_compat_card.py" in line for line in copy_lines), copy_lines
