"""CI-336 — a handful of smoke cases proving the wrapper does not undo the
invariants `/api/v2` enforces.

🔴 **Scope, deliberately small.** The 4 release invariants are asserted over the
full real-user corpus in `msds-chain`'s `backend/tests/eval/`, against
`/api/v2/*` — the layer this server calls over HTTP. Replaying that corpus HERE
would bill one metered call per query, bind the assertions to whatever is in Prod
today, and add a second flaky release gate of the CI-325 shape. So this file is
**not** a gate and holds no corpus.

🔴 **And no user query text may ever be committed to this repo** — it is public.
Every input below is invented. (Blake, 2026-08-06: the real corpus is allowed in
the *private* msds-chain repo only.)

**What it is for.** CI-342 showed a defect class that lives only in this layer:
the hand-maintained `structuredContent` allowlist silently dropped a field the
backend had added. An invariant asserted downstream is worthless if the field it
reads does not survive the wrapper, or if the rendered *text* — the part a model
actually consumes — says something the structured data denies.
"""
import asyncio

import pytest

import server


def _run(tool, patch_name, payload, *args):
    """Call a tool with its backend call stubbed; return the CallToolResult."""
    async def _fake(*_a, **_k):
        return payload

    async def _no_log(*_a, **_k):
        return None

    orig, orig_log = getattr(server, patch_name), server._log_call
    setattr(server, patch_name, _fake)
    server._log_call = _no_log
    try:
        return asyncio.run(tool(*args))
    finally:
        setattr(server, patch_name, orig)
        server._log_call = orig_log


# ── ① / ② / ③ depend on these fields surviving the allowlist ────────────────

PPE_PAYLOAD = {
    "results": [{
        "chemical_name": "Acetone", "cas": "67-64-1", "signal_word": "Danger",
        "h_codes": ["H225"], "ppe": {"eye": ["goggles"]},
        "minimum_ppe_level": "standard", "data_source": "h_code_mapping",
        "insufficient_hazard_data": False, "traceability": "sds_backed",
    }],
    "unresolved": ["a-substance-we-do-not-have"],
    "documents": [{"chemical": "acetone", "chemical_name": "Acetone",
                   "cas": "67-64-1", "supplier": "Fixture Co", "record_id": 1,
                   "sds_document_url": "/msds/token/abc"}],
}


def test_fields_the_invariants_read_survive_the_wrapper():
    """`unresolved`, `documents[].chemical*`, `cas`, `insufficient_hazard_data`.

    These four are the entire input to invariants ①-④. If the allowlist drops any
    of them, the downstream suite keeps passing while the outward surface stops
    carrying the evidence — the CI-342 shape, one layer over.
    """
    res = _run(server.get_ppe_recommendation, "_direct_ppe", PPE_PAYLOAD, ["acetone"])
    sc = res.structuredContent
    assert "unresolved" in sc, "invariant ① loses its left-hand side"
    assert sc["documents"] and "chemical" in sc["documents"][0], "invariant ① loses documents"
    assert sc["documents"][0].get("cas"), "invariant ③ loses the document CAS"
    result = sc["results"][0]
    assert "cas" in result, "invariants ②/③ lose the resolved CAS"
    assert "insufficient_hazard_data" in result, "invariant ④ loses its trigger"


def test_unresolved_name_is_not_also_presented_as_having_a_document():
    """Invariant ① restated on the rendered text.

    The text is what a model reads; a name listed under **Unresolved:** must not
    also appear in the SDS-links block.
    """
    res = _run(server.get_ppe_recommendation, "_direct_ppe", PPE_PAYLOAD, ["acetone"])
    text = res.content[0].text
    assert "a-substance-we-do-not-have" in text
    links_block = text.split("Original SDS")[-1] if "Original SDS" in text else ""
    assert "a-substance-we-do-not-have" not in links_block


# ── ④ in the wrapper ────────────────────────────────────────────────────────

INSUFFICIENT_WITH_PDF = {
    "results": [{
        "chemical_name": "Invented Filler B", "cas": "9005-25-8",
        "signal_word": "", "h_codes": [], "ppe": {},
        "minimum_ppe_level": None, "data_source": "none",
        "insufficient_hazard_data": True,
        "insufficient_code": "insufficient_hazard_data",
        "insufficient_reason": "no hazard data parsed from this record",
        # 🔴 The backend deliberately emits "none", NOT "rule_based" — a rule that
        # never fired must not claim to have fired (direct_api.py, CI-243/CI-365).
        "traceability": "none",
    }],
    "unresolved": [],
    # …and a PDF nevertheless exists for it. That combination is the whole point.
    "documents": [{"chemical": "invented filler b", "chemical_name": "Invented Filler B",
                   "cas": "9005-25-8", "supplier": "Fixture Co", "record_id": 2,
                   "sds_document_url": "/msds/token/def"}],
}


def test_cannot_determine_still_says_so_in_text():
    """The passing half: the CI-360 wording survives."""
    res = _run(server.get_ppe_recommendation, "_direct_ppe",
               INSUFFICIENT_WITH_PDF, ["invented filler b"])
    text = res.content[0].text
    assert "CANNOT BE DETERMINED" in text
    assert "NOT a low-hazard finding" in text


@pytest.mark.xfail(
    strict=True,
    reason=(
        "CI-336 finding, 2026-08-08 — defect exists only in this layer, which is "
        "why the /api/v2 suite cannot see it. get_ppe_recommendation's trace_label "
        "falls back to 'is there a document for this name' when traceability is "
        "neither sds_backed nor rule_based. `none` — the value CI-243/CI-365 "
        "introduced precisely to mean 'nothing was read' — lands in that fallback, "
        "so a record with no hazard data gets stamped '[Source: SDS document]' in "
        "the text a model consumes. The backend removed the claim; the wrapper puts "
        "it back. xfail(strict) rather than a fix: the fix belongs to whoever owns "
        "the CI-365 contract, and this file must not gate a release."
    ),
)
def test_no_sds_source_label_when_nothing_was_read():
    """Invariant ④ on the wrapper's text: `traceability: none` ⇒ no source claim."""
    res = _run(server.get_ppe_recommendation, "_direct_ppe",
               INSUFFICIENT_WITH_PDF, ["invented filler b"])
    header = res.content[0].text.split("\n")[2]
    assert "[Source: SDS document]" not in header, header
