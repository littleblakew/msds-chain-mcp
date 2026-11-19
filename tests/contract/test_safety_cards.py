r"""Cards on the safety tools (PPE, regulatory check, storage, emergency, transport, waste,
exposure limits, alternatives): each tool points at the shared template, and its result
carries the backend's per-chemical `view` in `_meta` (the channel the host's model cannot
rewrite) together with the source line the result already holds.

| guard | what reverting makes it fail |
|---|---|
| `test_card_tools_are_registered` | drop a tool from `CARD_TOOLS` (its empty-wallet result then has no card) |
| `test_each_tool_points_at_the_template` | drop `meta=ui_compat_card.tool_meta()` from the tool decorator |
| `test_result_carries_the_view_payload` | drop `meta=` / the `card_meta` call on the result |
| `test_result_without_view_has_no_card` | build a payload for results that have no usable `view` |
| `test_a_malformed_view_is_not_drawn` | skip the shape check in `_usable` |
| `test_payload_builder_failure_drops_the_card_not_the_answer` | build the payload inline instead of through `card_meta` |
| `test_source_name_links_only_on_the_allowed_origin` | skip `_allowed_link` on the document URL |
| `test_source_link_is_not_borrowed_from_another_suppliers_document` | link a document of a different supplier than the one the text came from |
| `test_form_disclosures_ride_along` | drop `notices` from the item |
| `test_cap_reports_what_it_left_out` | drop the `omitted` count |
| `test_compliance_card_survives_the_usage_rebuild` | drop `meta=result.meta` in `_with_usage` |
| `test_template_dispatches_each_new_kind` | drop a `SAFETY.<kind>` renderer from the template |
| `test_template_strips_em_dashes_and_escapes` | render `view` text without `esc` / `sx` |
| `test_credits_exhausted_gets_the_card_on_new_tools` | leave a tool out of `CARD_TOOLS` |

Async cases use `asyncio.run` (no pytest-asyncio in requirements-dev.txt).
"""
import asyncio

import pytest

import server
import ui_compat_card as card
from msds_mcp import ui_card_views
import card_view_fixtures as fx

# tool name -> (card kind, patched direct call, how to call it, payload that the backend returns)
_CASES = {
    "get_ppe_recommendation": ("ppe", "_direct_ppe", {"chemicals": ["sulfuric acid", "acetone"]},
                               {"results": [fx.PPE_ACID, fx.PPE_ACETONE], "documents": fx.PPE_DOCS}),
    "check_regulatory_compliance": ("compliance", "_direct_compliance", {"chemicals": ["benzene"]}, fx.COMPLIANCE),
    "get_storage_guidance": ("storage", "_direct_storage", {"chemicals": ["acetone"]}, {"results": [fx.STORAGE]}),
    "get_emergency_response": ("emergency", "_direct_emergency", {"chemical": "hydrogen fluoride", "scenario": "exposure"},
                               {**fx.EMERGENCY, "chemical": "Hydrogen fluoride"}),
    "get_transport_classification": ("transport", "_direct_transport", {"chemicals": ["acetone"]}, {"results": [fx.TRANSPORT]}),
    "get_waste_disposal": ("waste", "_direct_waste", {"chemicals": ["acetone"]}, {"results": [fx.WASTE]}),
    "get_exposure_limits": ("exposure", "_direct_exposure", {"chemicals": ["acetone"]}, {"results": [fx.EXPOSURE]}),
    "get_chemical_alternatives": ("substitution", "_direct_alternatives", {"chemical": "benzene"}, fx.SUBSTITUTION),
}


def _fake(payload):
    async def _f(*a, **kw):
        return payload
    return _f


def _call(monkeypatch, tool, payload=None):
    kind, direct, kwargs, default = _CASES[tool]
    data = default if payload is None else payload
    monkeypatch.setattr(server, direct, _fake(data))
    monkeypatch.setattr(server, "_require_api_key", lambda: None)
    return asyncio.run(getattr(server, tool)(**kwargs))


def _text(result):
    return "\n".join(getattr(b, "text", "") or "" for b in result.content)


def _results_of(tool, data):
    return data.get("results") if "results" in data and tool != "check_regulatory_compliance" else [data]


def _strip_views(tool, data):
    if tool == "check_regulatory_compliance":
        return fx.without_view(data)
    if "results" in data:
        return {**data, "results": [fx.without_view(r) for r in data["results"]]}
    return fx.without_view(data)


def test_card_tools_are_registered():
    assert {t for t in _CASES} <= card.CARD_TOOLS


def test_each_tool_points_at_the_template():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in _CASES:
        meta = tools[name].meta or {}
        assert meta.get("ui", {}).get("resourceUri") == card.CARD_URI, (name, meta)
        assert meta.get("openai/outputTemplate") == card.CARD_URI, (name, meta)


@pytest.mark.parametrize("tool", sorted(_CASES))
def test_result_carries_the_view_payload(monkeypatch, tool):
    res = _call(monkeypatch, tool)
    p = res.meta[card.CARD_META_KEY]
    assert p["kind"] == _CASES[tool][0] and p["omitted"] == 0, p
    given = [r["view"] for r in _results_of(tool, _CASES[tool][3])]
    assert [i["view"] for i in p["items"]] == given, "the view must reach the card untouched"
    assert not res.is_error and p["items"][0]["view"]["status_label"] in str(given)


@pytest.mark.parametrize("tool", sorted(_CASES))
def test_result_without_view_has_no_card(monkeypatch, tool):
    """Older backend: the text answer is unchanged and no card meta is attached."""
    res = _call(monkeypatch, tool, _strip_views(tool, _CASES[tool][3]))
    assert res.meta is None and _text(res), tool
    assert not res.is_error


def test_a_malformed_view_is_not_drawn():
    good = fx.STORAGE["view"]
    for broken in (None, "x", {**good, "status": None}, {**good, "keep_away": "oops"}):
        assert ui_card_views.safety_payload("storage", [{"view": broken}], [], fx.ORIGIN) is None
    bad_region = {**fx.PPE_ACID["view"], "regions": [{"key": "gloves", "primary": [], "pinned": []}]}
    assert ui_card_views.safety_payload("ppe", [{"view": bad_region}], [], fx.ORIGIN) is None
    assert ui_card_views.safety_payload("compliance", [{"view": {**fx.COMPLIANCE["view"], "rows": []}}], [], fx.ORIGIN) is None
    mixed = ui_card_views.safety_payload("storage", [{"view": None}, fx.STORAGE], [], fx.ORIGIN)
    assert len(mixed["items"]) == 1


@pytest.mark.parametrize("tool", sorted(_CASES))
def test_payload_builder_failure_drops_the_card_not_the_answer(monkeypatch, tool):
    def _boom(*a, **kw):
        raise ValueError("bad payload")
    monkeypatch.setattr(ui_card_views, "safety_payload", _boom)
    res = _call(monkeypatch, tool)
    assert res.meta is None and _text(res) and not res.is_error


def test_source_name_links_only_on_the_allowed_origin(monkeypatch):
    res = _call(monkeypatch, "get_ppe_recommendation")
    acid, acetone = res.meta[card.CARD_META_KEY]["items"]
    assert acid["source"] == {"supplier": "Supplier X", "url": server._CARD_LINK_ORIGIN + "/msds/token/acid",
                              "version": "7.0", "revision_date": "2024-05-01"}
    # the acetone document is on a foreign origin: the supplier stays plain text
    assert acetone["source"]["supplier"] == "Supplier Y" and acetone["source"]["url"] is None


def test_source_link_is_not_borrowed_from_another_suppliers_document():
    item = {**fx.PPE_ACID, "content_source": {"supplier": "Other Supplier", "revision_date": "2020-01-01"}}
    out = ui_card_views.safety_payload("ppe", [item], fx.PPE_DOCS, server._CARD_LINK_ORIGIN)
    assert out["items"][0]["source"]["url"] is None


def test_source_line_falls_back_to_source_info_and_is_absent_without_one():
    item = {**fx.SUBSTITUTION, "source_info": {"supplier": "Supplier Z", "revision_date": "2022-01-01"}}
    out = ui_card_views.safety_payload("substitution", [item], [], fx.ORIGIN)
    assert out["items"][0]["source"]["supplier"] == "Supplier Z"
    assert ui_card_views.safety_payload("substitution", [fx.SUBSTITUTION], [], fx.ORIGIN)["items"][0]["source"] is None


def test_form_disclosures_ride_along(monkeypatch):
    res = _call(monkeypatch, "get_ppe_recommendation")
    acid, acetone = res.meta[card.CARD_META_KEY]["items"]
    assert acetone["notices"] == ["This SDS describes the liquid form."] and acid["notices"] == []
    assert "This SDS describes the liquid form." in _text(res), "card notices must be text the answer prints"


def test_cap_reports_what_it_left_out():
    results = [{**fx.STORAGE, "cas": f"67-64-{i}"} for i in range(15)]
    out = ui_card_views.safety_payload("storage", results, [], fx.ORIGIN)
    assert len(out["items"]) == 12 and out["omitted"] == 3


def test_compliance_card_survives_the_usage_rebuild(monkeypatch):
    payload = {**fx.COMPLIANCE, "_usage": {"cost": 1, "balance": 5}}
    res = _call(monkeypatch, "check_regulatory_compliance", payload)
    assert res.structured_content.get("usage"), "precondition: the usage rebuild ran"
    assert res.meta[card.CARD_META_KEY]["kind"] == "compliance"
    assert res.structured_content["results"][0]["view"] == fx.COMPLIANCE["view"]


def test_unresolved_compliance_result_has_no_card(monkeypatch):
    res = _call(monkeypatch, "check_regulatory_compliance",
                {"chemical": "xq-71", "cas": None, "region_results": [], "summary_level": "unknown", "unresolved": True})
    assert res.meta is None


def _html():
    return str(list(asyncio.run(server.mcp.read_resource(card.CARD_URI)))[0].content)


def test_template_dispatches_each_new_kind():
    html = _html()
    assert "SAFETY[p.kind] ? SAFETY[p.kind](p)" in html
    for kind in ("ppe", "compliance", "storage", "emergency", "transport", "waste", "exposure", "substitution"):
        assert f"SAFETY.{kind} = function(p)" in html, kind
    assert "__SAFETY_JS__" not in html and "__SAFETY_CSS__" not in html


def test_template_strips_em_dashes_and_escapes():
    html = _html()
    assert "function tx(x){ return esc(sx(x)); }" in html
    assert "fixLine" in html
    # view text only reaches the page through tx / md (both escape first)
    assert "innerHTML" in html and "SAFETY.ppe" in html


def test_credits_exhausted_gets_the_card_on_new_tools(monkeypatch):
    class _Resp402:
        status_code = 402
        headers: dict = {}

        def json(self):
            return {"detail": {"error": "credit_floor", "balance": 0.0, "message": "Out of credits.",
                               "upgrade_url": "https://msdschain.lagentbot.com/pricing"}}

    async def _empty(*a, **kw):
        return server._billed_json(_Resp402())
    monkeypatch.setattr(server, "_require_api_key", lambda: None)
    for tool, (kind, direct, kwargs, _) in _CASES.items():
        monkeypatch.setattr(server, direct, _empty)
        res = asyncio.run(getattr(server, tool)(**kwargs))
        assert res.meta[card.CARD_META_KEY]["kind"] == "credits_exhausted", tool


def test_document_with_another_cas_is_never_linked_even_if_the_name_matches():
    """A shared name does not make two records the same substance."""
    item = {"chemical_name": "Ethanol", "cas": "64-17-5"}
    other = {"chemical_name": "Ethanol", "cas": "67-64-1", "sds_document_url": "x"}
    assert ui_card_views._document_for(item, [other]) is None
    same = {"chemical_name": "Ethanol", "cas": "64-17-5", "sds_document_url": "y"}
    assert ui_card_views._document_for(item, [other, same]) is same


def test_name_match_without_cas_must_be_unambiguous():
    item = {"chemical_name": "Ethanol"}
    a = {"chemical_name": "Ethanol", "cas": "64-17-5"}
    b = {"chemical": "ethanol", "cas": "67-64-1"}
    assert ui_card_views._document_for(item, [a]) is a
    assert ui_card_views._document_for(item, [a, b]) is None


def test_tone_classes_come_from_an_allowlist():
    """Some tones are backend data (risk_tone); a quote in one must not reach the class attribute."""
    html = card.card_html("https://api.msdschain.lagentbot.com", "https://msdschain.lagentbot.com")
    assert '(TONES[tone] ? tone : "pending")' in html
    assert "'<span class=\"chip t-' + tone" not in html
    assert "sc-' + esc(v.scenario)" not in html
