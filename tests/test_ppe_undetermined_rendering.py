"""CI-243: a null PPE level must not render as the word "None".

The backend now returns `minimum_ppe_level: null` + `insufficient_hazard_data: true`
when an SDS record parsed no hazards at all. This surface used
`item.get('minimum_ppe_level', 'N/A')` — which does NOT catch that, because the key
exists and its value is None — so the user saw:

    - Minimum PPE level: **None**

That is better than the original "basic" (it is no longer a wrong conclusion) but it
still fails the actual job: it reads as a glitch, and a model relaying it has nothing
telling it not to fill the gap from its own chemistry knowledge.
"""
import pytest

import server


async def _run(payload):
    async def _fake(chemicals):
        return payload
    orig = server._direct_ppe
    server._direct_ppe = _fake
    try:
        res = await server.get_ppe_recommendation(["hydrochloric acid"])
        # FastMCP wraps the handler's string in a CallToolResult
        return res.content[0].text if hasattr(res, "content") else res
    finally:
        server._direct_ppe = orig


@pytest.mark.asyncio
async def test_null_level_renders_as_cannot_be_determined():
    out = await _run({"results": [{
        "chemical_name": "Hydrochloric acid", "cas": "7647-01-0",
        "signal_word": "", "h_codes": [], "ppe": {},
        "minimum_ppe_level": None, "insufficient_hazard_data": True,
    }], "unresolved": [], "documents": []})

    assert "CANNOT BE DETERMINED" in out
    assert "**None**" not in out, "a null must never surface as the literal word None"
    assert "NOT" in out and "low-hazard" in out, "must say absence is not a benign finding"


@pytest.mark.asyncio
async def test_real_level_still_renders_normally():
    out = await _run({"results": [{
        "chemical_name": "Hydrochloric acid", "cas": "7647-01-0",
        "signal_word": "Danger", "h_codes": ["H314"], "ppe": {"hands": ["nitrile"]},
        "minimum_ppe_level": "maximum", "insufficient_hazard_data": False,
    }], "unresolved": [], "documents": []})

    assert "Minimum PPE level: **maximum**" in out
    assert "CANNOT BE DETERMINED" not in out
