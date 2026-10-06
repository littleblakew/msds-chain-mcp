"""Two "the backend says it, the text the model reads does not" gaps.

1. Taiwan has no inventory copy (TCSI). The backend now returns a pointer to the
   official CSNN query page — `inventory.lookup` on `/compliance`, top-level
   `inventory_lookups` on `/regulatory-lists` — with `on_inventory` still null.
   The renderer only knew True/False, so TW rendered nothing at all: the silence
   the backend change was meant to end. Criterion: every TW result shows the CSNN
   link, and nothing says the inventory was checked.

2. `get_sds_section` can carry section text borrowed from a corpus record. The
   tool is `structured_output=False`, so if the provenance of that text lives only
   in structuredContent, the model reads "ACME's text + Source: <canonical
   supplier>", or (no canonical row) safety text with no source at all.

The `note` strings below are assembled from the backend's `inventory.lookup_only`
template (compliance_text.py) so the assertions test the real wording, not a
paraphrase of it.
"""
import asyncio

import server
from server import _format_region_results, _format_regulatory_lists

_URL = "https://csnn.osha.gov.tw/content/home/Substance_Query_Q.aspx"
_NAME = "Taiwan OSHA CSNN chemical substance query (TCSI)"
_NOTE = " ".join([
    "We hold no copy of the TW existing-substance inventory, so this",
    "service does NOT determine whether the substance is an existing or a",
    "new chemical substance there. Check it yourself on the official",
    f"{_NAME}: {_URL}",
])


def _tw(**inv):
    return {"region": "TW", "status": "not_restricted", "flags": [], "details": "",
            "inventory": {"lists_checked": [], "on_inventory": None, "coverage": "none", **inv}}


def test_tw_compliance_result_carries_the_csnn_link():
    out = "\n".join(_format_region_results([_tw(note=_NOTE, lookup={"name": _NAME, "url": _URL})]))
    assert _URL in out, f"TW result rendered without the CSNN link: {out!r}"
    assert "does NOT determine" in out
    assert "listed (" not in out and "NOT listed" not in out, \
        f"a lookup pointer must not render as an inventory finding: {out!r}"


def test_lookup_url_is_kept_even_if_the_note_stops_carrying_it():
    out = "\n".join(_format_region_results([_tw(note="Not determined.",
                                                 lookup={"name": _NAME, "url": _URL})]))
    assert _URL in out


def test_null_inventory_without_lookup_still_renders_nothing():
    """Regions with no inventory and no lookup page keep the previous behaviour."""
    out = "\n".join(_format_region_results([_tw(note="No inventory.")]))
    assert "Inventory" not in out


def _reg_lists(**extra):
    base = {"chemical": "benzene", "cas": "71-43-2", "count": 1,
            "lists": [{"list": "Taiwan MOENV Listed Toxic", "region": "TW"}]}
    base.update(extra)
    return base


def test_regulatory_lists_renders_inventory_lookups():
    data = _reg_lists(inventory_lookups=[{"region": "TW", "name": _NAME, "url": _URL, "note": _NOTE}])
    out = _format_regulatory_lists(data, "benzene", "en")
    assert _URL in out, f"inventory_lookups not rendered: {out!r}"
    assert "TCSI" in out


def test_regulatory_lists_zero_hits_still_renders_inventory_lookups():
    data = _reg_lists(lists=[], count=0,
                      inventory_lookups=[{"region": "TW", "name": _NAME, "url": _URL, "note": _NOTE}])
    assert _URL in _format_regulatory_lists(data, "benzene", "en")


def _section(payload):
    async def _fake(*_a, **_k):
        return payload
    orig = server._direct_sds_section
    server._direct_sds_section = _fake
    try:
        res = asyncio.run(server.get_sds_section("acetone", 4))
        return res.content[0].text if hasattr(res, "content") else res
    finally:
        server._direct_sds_section = orig


_BORROWED = {
    "chemical": "Acetone", "cas": "67-64-1",
    "content": "SECTION 4: First-aid measures\nRinse with water.",
    "supplier": "CanonicalCo", "revision_date": "2024-01-01", "region": "US",
    "data_source": "corpus_sections",
    "corpus_source_info": {"source": "vendor_site", "supplier": "ACME",
                           "revision_date": "2021-05-05", "shape": "sections"},
}


def test_borrowed_section_text_is_disclosed_before_the_text():
    note = "This section's text comes from a different SDS (ACME, 2021-05-05)."
    txt = _section({**_BORROWED, "section_source_differs": True, "section_source_note": note})
    assert note in txt
    assert txt.index(note) < txt.index("Rinse with water"), "note must precede the text"
    assert "**Source:** CanonicalCo" not in txt, \
        f"canonical supplier still presented as the source of borrowed text: {txt!r}"
    assert "Cited record (not the source of the text above):** CanonicalCo" in txt
    assert "**Text source:** ACME" in txt
    assert "2021-05-05" in txt


def test_corpus_text_without_canonical_row_still_has_a_source():
    """Path that is already live: no canonical row, so `supplier` is empty."""
    payload = {**_BORROWED, "supplier": None, "revision_date": None, "region": None}
    txt = _section(payload)
    assert "**Text source:** ACME · vendor_site" in txt, f"text shipped with no source: {txt!r}"
    assert "**Text revision date:** 2021-05-05" in txt


def test_canonical_text_keeps_the_plain_source_line():
    payload = {k: v for k, v in _BORROWED.items() if k != "corpus_source_info"}
    payload["data_source"] = "canonical_sections"
    txt = _section(payload)
    assert "- **Source:** CanonicalCo · US" in txt
    assert "Text source" not in txt and "Cited record" not in txt
