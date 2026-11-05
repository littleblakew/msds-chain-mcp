"""review_synthesis_route and assess_thermal_hazard: request shape and rendered text.

The judgement is made on what the caller reads (the rendered text) and on the payload
that actually leaves the process, not on the helper signatures.
"""
import asyncio
import re

import pytest

import server
from request_identity import set_caller_credential

_STEPS = [
    {"label": "Coupling", "chemicals": ["acetone", "sulfuric acid"]},
    {"label": "Workup", "chemicals": ["sodium hydroxide"], "carryover": ["acetone"]},
]


@pytest.fixture(autouse=True)
def _cred(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(server, "_log_call", _noop)
    set_caller_credential("sk-msds-test")
    yield
    set_caller_credential(None)


def _pair(a, b, level, reason="r"):
    return {"chem1": a, "chem2": b, "cas_a": None, "cas_b": None, "level": level,
            "reason": reason, "traceability": "rule_based", "citation": "CAMEO:test"}


def _route(overall, step_pairs, tr_pairs, tr_status):
    return {
        "overall_status": overall, "pairs_checked": 3,
        "steps": [
            {"step": 1, "label": "Coupling", "status": "caution",
             "chemicals": [{"name": "acetone", "cas": "67-64-1", "resolved": True}],
             "pairs": step_pairs},
            {"step": 2, "label": "Workup", "status": "no_known_incompatibility_among_checked",
             "chemicals": [], "pairs": []},
        ],
        "transitions": [{"from_step": 1, "to_step": 2, "carryover": ["acetone"],
                         "carryover_assumed": True, "status": tr_status, "pairs": tr_pairs}],
        "assumptions": ["Step 2 carryover not stated: worst case assumed."],
        "unresolved": [], "scope_note": "Scope: pairwise screening only.",
    }


def _run_route(monkeypatch, payload, steps=_STEPS, **kw):
    sent = {}

    async def fake(s, lang=None, include_handling=False):
        sent["steps"], sent["lang"], sent["include_handling"] = s, lang, include_handling
        return payload

    monkeypatch.setattr(server, "_direct_route_review", fake)
    res = asyncio.run(server.review_synthesis_route(steps=steps, **kw))
    return res, sent


def test_transition_hazard_is_rendered_with_pair_reason_and_source(monkeypatch):
    payload = _route("hazard_found", [],
                     [_pair("acetone", "sodium hydroxide", "incompatible", "Aldol condensation, exotherm")],
                     "hazard_found")
    res, sent = _run_route(monkeypatch, payload)
    text = res.content[0].text
    assert "HAZARD FOUND" in text.split("\n")[1]
    # 名字取自后端配对行的 `chem1` / `chem2`（batch-safety 同形）；读错键会退回 CAS（这里是 None）⇒ "?"。
    assert "**acetone** + **sodium hydroxide**: INCOMPATIBLE" in text
    assert "INCOMPATIBLE" in text and "Aldol condensation, exotherm" in text
    assert "CAMEO:test" in text
    assert "worst case" in text and "Scope: pairwise" in text
    assert "assess_thermal_hazard" in text
    assert sent["steps"][1]["carryover"] == ["acetone"]
    assert "carryover" not in sent["steps"][0] and sent["lang"] is None


def test_source_line_is_readable_not_an_internal_key(monkeypatch):
    rule = {**_pair("sodium azide", "dichloromethane", "incompatible", "forms diazidomethane"),
            "citation": "lit:ehs-azide-polyhalomethane", "source": "pair_rule",
            "source_detail": "Institutional EHS guidance on azide compounds"}
    unknown = {**_pair("THF", "sodium azide", "unverified", "insufficient data to verify"),
               "citation": None, "source": "unverified_insufficient_data",
               "source_detail": "insufficient data to verify"}
    payload = _route("hazard_found", [rule, unknown], [], "incomplete")
    text = _run_route(monkeypatch, payload)[0].content[0].text
    assert "Source: Institutional EHS guidance on azide compounds" in text
    assert not re.search(r"lit:|pair_rule|unverified_insufficient_data", text)
    assert text.count("insufficient data to verify") == 1


def test_clean_route_never_says_safe(monkeypatch):
    payload = _route("no_known_incompatibility_among_checked",
                     [_pair("a", "b", "no_known_incompatibility")], [], "no_known_incompatibility_among_checked")
    text = _run_route(monkeypatch, payload)[0].content[0].text
    assert "No known incompatibility among the pairs checked" in text
    assert not re.search(r"\bsafe\b|no hazard|all clear", text, re.I), text
    assert "—" not in text and "–" not in text


def test_invalid_route_input_does_not_reach_backend(monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("backend called")
    monkeypatch.setattr(server, "_direct_route_review", boom)
    r1 = asyncio.run(server.review_synthesis_route(steps=[{"chemicals": []}]))
    r2 = asyncio.run(server.review_synthesis_route(steps=[{"chemicals": ["a"]}] * 11))
    assert "between 1 and 12" in r1 and "at most 10 steps" in r2


class _Resp:
    status_code = 200
    headers: dict = {}

    def raise_for_status(self): ...
    def json(self): return {"screened": False, "screening": {}, "process": {}}


class _Client:
    def __init__(self, box): self.box = box
    def __call__(self, *a, **kw): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, headers=None, **kw):
        self.box.append((url, json))
        return _Resp()


def test_thermal_payload_only_carries_given_values(monkeypatch):
    box: list = []
    monkeypatch.setattr(server.httpx, "AsyncClient", _Client(box))
    asyncio.run(server.assess_thermal_hazard(chemical="x", dsc_onset_c=150.0, accumulation_fraction=0.5))
    url, body = box[0]
    assert url.endswith("/api/v2/thermal-hazard")
    assert set(body) == {"chemical", "dsc_onset_c", "accumulation_fraction", "lang"}


def test_route_payload_shape_at_http_boundary(monkeypatch):
    box: list = []
    monkeypatch.setattr(server.httpx, "AsyncClient", _Client(box))
    asyncio.run(server.review_synthesis_route(steps=_STEPS))
    url, body = box[0]
    assert url.endswith("/api/v2/route-review")
    assert body["steps"] == _STEPS and body["lang"] == "en"


def _thermal(monkeypatch, payload, **kw):
    async def fake(inputs, lang=None):
        return payload
    monkeypatch.setattr(server, "_direct_thermal_hazard", fake)
    return asyncio.run(server.assess_thermal_hazard(**kw))


def test_thermal_not_screened_says_so(monkeypatch):
    payload = {"chemical": "mystery", "cas": None, "screened": False,
               "structure": {"smiles": None, "source": "none"},
               "screening": {"a6_outcome": "chemical_not_resolved", "groups": [],
                             "not_a_safety_conclusion": True},
               "process": {"values": {}, "missing": ["dsc_onset_c"], "assumptions": [],
                           "stoessel_class": None},
               "red_flags": [], "scope_note": "Scope text."}
    text = _thermal(monkeypatch, payload, chemical="mystery").content[0].text
    assert "Screening was NOT performed" in text
    assert "could not be resolved" in text
    assert "DSC onset (C)" in text and "Missing inputs" in text
    assert "process safety specialist" in text
    assert not re.search(r"\bsafe\b|no hazard|all clear", text, re.I), text


def test_thermal_full_render(monkeypatch):
    payload = {"chemical": "nitromethane", "cas": "67-64-1", "screened": True,
               "structure": {"smiles": "C[N+](=O)[O-]", "source": "database"},
               "screening": {"a6_outcome": "explosive_property_testing_indicated",
                             "criterion": "A.6 group screen",
                             "groups": [{"key": "nitro", "family": "Nitro compound", "contains_oxygen": True}],
                             "oxygen_balance": {"value_pct": -39.3, "formula_scope": "CH3NO2"},
                             "red_flag": True, "not_a_safety_conclusion": True, "reference": "Ref A"},
               "yoshida": {"computable": True, "explosive_propagation": "possible",
                           "shock_sensitivity": "possible", "ep_flag": True, "ss_flag": False,
                           "caveat": "Correlation only.", "reference": "Ref Y"},
               "process": {"values": {"dsc_onset_c": {"value": 200, "source": "user"}},
                           "missing": ["td24_c"], "assumptions": ["Cp assumed given"],
                           "stoessel_class": 3, "stoessel_meaning": "Critical", "reference": "Ref S"},
               "red_flags": ["Explosive-properties testing is indicated"], "scope_note": "Scope text."}
    text = _thermal(monkeypatch, payload, chemical="nitromethane").content[0].text
    assert text.index("Red flags") < text.index("Structural screening")
    for needle in ("Nitro compound", "-39.3%", "Yoshida", "(source: user)", "TD24 (C)",
                   "Stoessel criticality class: 3", "Critical", "Scope text.", "specialist"):
        assert needle in text, needle



def test_handling_block_keeps_the_single_tool_honesty_rules(monkeypatch):
    payload = _route("no_known_incompatibility_among_checked", [], [], "no_known_incompatibility_among_checked")
    payload["handling"] = {
        "ppe": {"results": [
            {"chemical_name": "acetone", "minimum_ppe_level": "standard",
             "ppe": {"gloves": ["butyl rubber"], "eye": "goggles"}},
            {"chemical_name": "mystery salt", "minimum_ppe_level": None, "insufficient_hazard_data": True},
        ]},
        "waste": {"failed": True},
    }
    res, sent = _run_route(monkeypatch, payload, include_handling=True)
    text = res.content[0].text
    assert sent["include_handling"] is True
    assert "**acetone** PPE level standard (gloves: butyl rubber; eye: goggles)" in text
    # CI-243：没有危害数据不是低危
    assert "**mystery salt** PPE: CANNOT BE DETERMINED" in text and "not a low-hazard finding" in text
    assert "Waste lookup failed" in text
    # 「no hazard data」是诚实的那句（记录里没有数据），不算断言安全。
    assert not re.search(r"\bsafe\b|no hazard(?! data)|all clear", text, re.I), text


def test_handling_off_by_default_sends_nothing(monkeypatch):
    payload = _route("no_known_incompatibility_among_checked", [], [], "no_known_incompatibility_among_checked")
    res, sent = _run_route(monkeypatch, payload)
    assert sent["include_handling"] is False
    assert "Handling by chemical" not in res.content[0].text
