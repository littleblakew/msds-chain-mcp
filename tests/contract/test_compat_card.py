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
| `test_card_basis_is_the_field_the_text_prints` | read `citation` (or anything but `source`) for the pair basis |
| `test_only_https_links_on_the_allowed_origin_survive` | drop the `_allowed_link` filter in `card_payload` |
| `test_caps_report_what_they_left_out` | drop the `omitted` counts |
| `test_verdict_is_lowercased_for_the_css_class` | drop `.lower()` on the verdict |
| `test_template_initializes_checks_sender_and_pins_origin` | drop the `ui/initialize` handshake, the `e.source` check or the origin injection |
| `test_link_origin_is_the_public_one_not_the_backend_call_url` | derive the card's link origin from `API_URL` again |
| `test_template_change_ships_under_a_new_uri` | edit the template without bumping `CARD_URI` (or bump without re-pinning) |
| `test_template_follows_host_theme_and_reports_size` | drop the `color-scheme` declaration, the `hostContext.theme` read or the `size-changed` report |

Async cases use `asyncio.run` (no pytest-asyncio in requirements-dev.txt, see test_response_kind.py).
"""
import asyncio
from pathlib import Path

import server
import ui_compat_card as card

_PAYLOAD = {
    "pairs": [{"chem1": "thionyl chloride", "chem2": "ethanol", "level": "incompatible",
               "reason": "r", "source": "Supplier X SDS rev 2020-01-01",
               "citation": "lit:internal-rule-id",
               "traceability": "rule_based"}],
    "unresolved": [],
    "precursor_disclosure": [{"query_name": "thionyl chloride",
                              "tier": "CWC Schedule 3B", "statement": "Listed on CWC Schedule 3B."}],
    "documents": [
        {"chemical": "thionyl chloride", "chemical_name": "Thionyl chloride", "supplier": "Supplier X",
         "revision_date": "2020-01-01", "sds_document_url": server._CARD_LINK_ORIGIN + "/msds/token/a"},
        {"chemical": "ethanol", "chemical_name": "Ethanol", "supplier": "Supplier Y",
         "revision_date": "2021-01-01", "sds_document_url": server._CARD_LINK_ORIGIN + "/msds/token/b"},
        {"chemical": "water", "chemical_name": "Water", "supplier": "Supplier Z",
         "revision_date": "2022-01-01", "sds_document_url": server._CARD_LINK_ORIGIN + "/msds/token/c"},
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
                             "documents": [1, {"supplier": "S"}]}, server._CARD_LINK_ORIGIN)
    assert out["pairs"] == [] and out["precursor"] == []
    assert out["documents"] == [{"chemical": None, "supplier": "S", "revision_date": None, "url": None}]


def test_dockerfile_ships_the_module():
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    copy_lines = [line for line in dockerfile.splitlines() if line.startswith("COPY")]
    assert any("ui_compat_card.py" in line for line in copy_lines), copy_lines


def test_card_basis_is_the_field_the_text_prints(monkeypatch):
    result = _call(monkeypatch)
    pair = result.meta[card.CARD_META_KEY]["pairs"][0]
    assert pair["source"] == "Supplier X SDS rev 2020-01-01", pair
    assert "citation" not in pair, pair
    assert pair["source"] in result.content[0].text, "card basis must be text the answer already shows"


def test_only_https_links_on_the_allowed_origin_survive():
    origin = server._CARD_LINK_ORIGIN
    docs = [{"supplier": s, "sds_document_url": u} for s, u in [
        ("js", "javascript:alert(1)"), ("data", "data:text/html,x"),
        ("scheme", ("http://" if origin.startswith("https://") else "https://")
                   + origin.split("://", 1)[1] + "/msds/token/a"),
        ("foreign", "https://evil.example/msds/token/a"), ("bad", "https://[::1"),
        ("ok", origin + "/msds/token/ok"),
    ]]
    out = card.card_payload({"documents": docs}, origin)
    assert [d["url"] for d in out["documents"]] == [None] * 5 + [origin + "/msds/token/ok"]


def test_caps_report_what_they_left_out():
    pairs = [{"chemical_a": f"a{i}", "chemical_b": "b", "level": "caution"} for i in range(15)]
    out = card.card_payload({"pairs": pairs}, server._CARD_LINK_ORIGIN)
    assert len(out["pairs"]) == 12 and out["omitted"]["pairs"] == 3, out["omitted"]


def test_verdict_is_lowercased_for_the_css_class():
    out = card.card_payload({"pairs": [{"chemical_a": "a", "chemical_b": "b", "level": "INCOMPATIBLE"}]},
                            server._CARD_LINK_ORIGIN)
    assert out["pairs"][0]["verdict"] == "incompatible"


def test_template_initializes_checks_sender_and_pins_origin():
    html = str(list(asyncio.run(server.mcp.read_resource(card.CARD_URI)))[0].content)
    assert '"ui/initialize"' in html and '"ui/notifications/initialized"' in html
    assert "e.source !== window.parent" in html
    assert "__ALLOWED_ORIGIN__" not in html
    assert f"var ALLOWED_ORIGIN = \"{server._CARD_LINK_ORIGIN}\";" in html


def test_template_follows_host_theme_and_reports_size():
    """Without a declared colour scheme the host gives the iframe an opaque light canvas, and
    without a size report the host keeps it at its default height and clips the card."""
    html = str(list(asyncio.run(server.mcp.read_resource(card.CARD_URI)))[0].content)
    assert '<meta name="color-scheme" content="light dark">' in html
    assert "hostContext" in html and '"ui/notifications/host-context-changed"' in html
    assert '"ui/notifications/size-changed"' in html


def test_link_origin_is_the_public_one_not_the_backend_call_url():
    """The core may call the backend on an internal address while signed links are issued on the
    public domain; tying the allowlist to API_URL filters every link out of the card."""
    assert server.API_URL.startswith("http://127.0.0.1"), "precondition: tests point API_URL elsewhere"
    assert server._CARD_LINK_ORIGIN == "https://api.msdschain.lagentbot.com"


# One entry per published template URI. Add a line for the new URI; never edit an old one.
_TEMPLATE_PINS = {
    "ui://msds-chain/compat-card-v2.html": "d10c99c3aaa5db0e",
    "ui://msds-chain/compat-card-v3.html": "c951300812010d6f",
}


def test_template_change_ships_under_a_new_uri():
    """Hosts cache the template by URI, so an edited template under an unchanged URI is never
    fetched again by clients that already hold it."""
    import hashlib
    got = hashlib.sha256(card.card_html("https://api.msdschain.lagentbot.com",
                                        "https://msdschain.lagentbot.com").encode()).hexdigest()[:16]
    assert card.CARD_URI in _TEMPLATE_PINS, f"new URI {card.CARD_URI}: add it to _TEMPLATE_PINS with {got}"
    assert _TEMPLATE_PINS[card.CARD_URI] == got, (
        f"template changed under {card.CARD_URI}: bump CARD_URI and pin the new one with {got}")
