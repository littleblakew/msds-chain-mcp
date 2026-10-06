"""CI-1076：OEL / 运输分类在没有字面表条目时，后端改回供应商 SDS 原文（§8.1 摘录、§14 引文）。

渲染器以前只认 `limits` 与 UN 字段 ⇒ 新证据全在 structuredContent 里，读 TextContent 的
模型一个字看不到；`transport_status: none` 时还会打出 `UN Number: None`。

判据落在模型真正读到的文本上，形状照后端 `sds_exposure_section.py` /
`sds_transport_section.py` 的返回（键名逐个对过）。

🔬 变异（做过）：
- `_transport_item_lines` 的 ambiguous 分支改成只印 `cands[0]` ⇒
  `test_ambiguous_lists_every_candidate_and_picks_none` 红。
- `_exposure_item_lines` 去掉 `_supplier_quote_lines(excerpt)` 那行 ⇒
  `test_supplier_excerpt_is_quoted_and_attributed` 红。
"""
import asyncio

import server


def _run(tool, patch_name, item, *args):
    payload = {"results": [{"chemical_name": "x", "cas": "1-1-1", **item}], "unresolved": []}

    async def _fake(*_a, **_k):
        return payload
    orig = getattr(server, patch_name)
    setattr(server, patch_name, _fake)
    try:
        return asyncio.run(tool(*args)).content[0].text
    finally:
        setattr(server, patch_name, orig)


def _exposure(item):
    return _run(server.get_exposure_limits, "_direct_exposure", item, ["x"])


def _transport(item):
    return _run(server.get_transport_classification, "_direct_transport", item, ["x"])


def _ev(section, quote, **flags):
    return {"section": section, "quote": quote, "supplier": "Acme Chem",
            "revision_date": "2024-03-01", "section_possibly_truncated": False,
            "excerpt_truncated": False, **flags}


def test_curated_rows_unchanged_and_source_note_shown():
    txt = _exposure({"limits": [{"type": "TWA", "value": 250.0, "unit": "ppm",
                                 "source": "ACGIH TLV", "region": "INT"}],
                     "data_source": "curated_literal", "oel_status": "curated_literal",
                     "source_note": "Values come from a curated table."})
    assert "- **ACGIH TLV** (INT): TWA = 250.0 ppm" in txt
    assert "Values come from a curated table." in txt


def test_regulatory_table_row_keeps_entry_name_and_version():
    txt = _exposure({"limits": [{"type": "TWA", "value": 5.0, "unit": "mg/m³",
                                 "source": "TW PEL", "region": "TW",
                                 "entry_name": "respirable", "source_version": "2024",
                                 "value_source": "regulatory_table"}],
                     "data_source": "regulatory_table", "oel_status": "regulatory_table"})
    assert "[respirable]" in txt and "2024" in txt
    assert "regulatory table" in txt  # 混合答案里分得清哪行出自值表


def test_supplier_excerpt_is_quoted_and_attributed():
    txt = _exposure({"limits": [], "data_source": "sds_section_8",
                     "oel_status": "supplier_excerpt",
                     "section_excerpt": _ev("8.1", "TWA 2 ppm (as F)", excerpt_truncated=True),
                     "note": "Quoted from the supplier's SDS Section 8.1."})
    assert "> TWA 2 ppm (as F)" in txt
    assert "Acme Chem" in txt and "2024-03-01" in txt
    assert "No values were extracted" in txt
    assert "cut short" in txt
    assert "No OEL data found" not in txt


def test_supplier_no_oel_statement_is_attributed_with_region_note():
    txt = _exposure({"limits": [], "data_source": "sds_section_8",
                     "oel_status": "supplier_states_no_oel",
                     "section_excerpt": _ev("8.1", "Contains no substances with OELs."),
                     "region_note": "Not checked against the requested region (JP)."})
    assert "supplier's SDS states that no exposure limit applies" in txt
    assert "not a check made by us" in txt
    assert "(JP)" in txt


def test_exposure_none_stays_an_honest_empty():
    txt = _exposure({"limits": [], "data_source": "none", "oel_status": "none"})
    assert "No OEL data found for this chemical." in txt


def test_ambiguous_lists_every_candidate_and_picks_none():
    txt = _transport({"un_number": None, "data_source": "sds_section_14",
                      "transport_status": "ambiguous",
                      "un_candidates": ["UN1671", "UN2312"],
                      "ambiguity_reason": "multiple_un_numbers",
                      "evidence": _ev("14", "UN1671 / UN2312")})
    assert "UN1671" in txt and "UN2312" in txt
    assert "not determined" in txt
    assert "**UN Number:** UN" not in txt
    assert "names more than one UN number" in txt


def test_supplier_not_regulated_is_the_suppliers_statement():
    txt = _transport({"un_number": None, "data_source": "sds_section_14",
                      "transport_status": "supplier_states_not_regulated",
                      "un_candidates": [], "evidence": _ev("14", "Not regulated.")})
    assert "not regulated as dangerous goods" in txt
    assert "not a classification made by us" in txt
    assert "> Not regulated." in txt


def test_un_found_from_section_14_says_not_stated_instead_of_none():
    txt = _transport({"un_number": "UN1790", "hazard_class": None, "packing_group": None,
                      "data_source": "sds_section_14", "transport_status": "un_number_found",
                      "un_candidates": ["UN1790"], "evidence": _ev("14", "UN1790")})
    assert "**UN Number:** UN1790" in txt
    assert "**Hazard Class:** not stated" in txt
    assert "Read from the supplier's SDS Section 14" in txt
    assert "None" not in txt


def test_transport_none_has_no_none_literals():
    txt = _transport({"un_number": None, "proper_shipping_name": None, "hazard_class": None,
                      "packing_group": None, "transport_modes": None,
                      "data_source": "none", "transport_status": "none"})
    assert "No transport classification on file" in txt
    assert "None" not in txt


def test_legacy_prod_empty_payload_is_not_four_not_stated_lines():
    """当前 Prod 后端（无 transport_status）查不到时字段全 None。"""
    txt = _transport({"un_number": None, "hazard_class": None, "packing_group": None,
                      "data_source": "none"})
    assert "No transport classification on file" in txt
    assert "not stated" not in txt and "None" not in txt


def test_legacy_prod_hit_still_renders():
    txt = _transport({"un_number": "UN1090", "hazard_class": "3", "data_source": "transport_database",
                      "transport_modes": {"road": "ADR 3"}})
    assert "**UN Number:** UN1090" in txt and "ROAD: ADR 3" in txt
    assert "supplier's SDS Section 14" not in txt
