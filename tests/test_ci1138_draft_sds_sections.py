"""CI-1138: `draft_sds_sections` rendering red lines.

The backend guarantees the data; these pin that the text an LLM reads does not drop it.

## Mutation record (a guard with no recorded mutation is treated as absent)

- In `_format_sds_draft`, render `lists_checked is None` through the normal branch
  (delete the `if checked is None:` arm) ⇒ `test_unreadable_lists_are_not_rendered_as_not_listed` red.
- Delete the `not_drafted` loop ⇒ `test_every_gap_is_listed_to_the_user` red.
- Move the `notice` line after the sections ⇒ `test_notice_comes_first_and_verbatim` red.
- Drop the `s15_basis` line ⇒ `test_s15_says_its_source_is_the_lists_not_the_supplier_sds` red.
"""
import asyncio

import server

_NOTICE = "This is a DRAFT assembled from the cited supplier SDS sections and regulatory lists."
_SCOPE = "Section 15 lines come from the regulatory lists we hold for TW, not from the supplier SDS."


def _payload(lists_checked=(), s15_ingredients=None):
    return {
        "notice": _NOTICE,
        "sections": {
            "8": {
                "basis": "supplier_sds",
                "drafted": [{
                    "chemical": "acetone", "cas": "67-64-1", "concentration": "60%",
                    "content": "8.1 Control parameters\nWEL 500 ppm",
                    "citation": {"supplier": "PANREAC", "revision_date": "2023-05-24",
                                 "region": "EU", "pdf_hash": "b2ea"},
                    "physical_form": None, "physical_form_disclosure": None,
                }],
                "not_drafted": [{"chemical": "ethanol", "cas": "64-17-5",
                                 "concentration": None, "reason": "no_original_pdf",
                                 "note": "ZZ-NO-ORIGINAL-NOTE"}],
            },
            "15": {
                "region": "TW", "basis": "regulatory_lists",
                "lists_checked": (None if lists_checked is None else [
                    {"list": "Taiwan MOENV Listed Toxic Chemical Substances", "region": "TW"}]),
                "scope_note": _SCOPE,
                "ingredients": s15_ingredients if s15_ingredients is not None else [
                    {"chemical": "acetone", "cas": "67-64-1", "concentration": "60%",
                     "sds_original": True, "listed_on": [], "lists_unavailable": False},
                ],
            },
        },
        "unresolved": [{"chemical": "xyzzy", "concentration": None,
                        "unresolved_detail": {"reason": "库中没有", "reason_en": "ZZ-UNRESOLVED-EN"}}],
        "lookup_failed": [{"chemical": "ZZ-FAILED-ONE", "concentration": None}],
    }


def test_unreadable_lists_are_not_rendered_as_not_listed():
    """① `lists_checked is None` ＝ 清单读不了，不是「不在任何清单上」（CI-507 同族）。"""
    ing = [{"chemical": "acetone", "cas": "67-64-1", "concentration": None,
            "sds_original": True, "listed_on": [], "lists_unavailable": True}]
    for lang in server._CATALOG_LANGS:
        s = server._SDS_DRAFT_STRINGS[lang]
        text = server._format_sds_draft(_payload(lists_checked=None, s15_ingredients=ing), lang)
        assert s["lists_unreadable"] in text, (lang, text)
        assert s["not_listed"] not in text, f"{lang}: 读不了的清单被渲染成了「不在清单上」"


def test_every_gap_is_listed_to_the_user():
    """③ not_drafted / unresolved / lookup_failed 是「缺什么、请上传」的入口。"""
    text = server._format_sds_draft(_payload(), "en")
    for token in ("ethanol", "ZZ-NO-ORIGINAL-NOTE", "ZZ-UNRESOLVED-EN", "ZZ-FAILED-ONE",
                  "upload_msds_pdf"):
        assert token in text, f"{token} 没到达用户读到的文本"


def test_notice_comes_first_and_verbatim():
    text = server._format_sds_draft(_payload(), "en")
    first_content = [ln for ln in text.splitlines() if ln.strip()][1]
    assert first_content == f"> {_NOTICE}", first_content


def test_s15_says_its_source_is_the_lists_not_the_supplier_sds():
    """② §15 的出处是政府清单；§8 的出处是供应商 SDS。两者别混。"""
    text = server._format_sds_draft(_payload(), "en")
    s15 = text.split("## Section 15", 1)[1]
    # Our own line, not the backend's `scope_note` (which may be absent, and whose
    # wording also contains "lists we hold for" ⇒ checking that phrase proved nothing).
    assert f"{server._SDS_DRAFT_STRINGS['en']['s15_basis']} TW" in s15
    assert _SCOPE in s15
    assert "PANREAC" not in s15, "供应商出处漏进了 §15"
    assert "PANREAC" in text.split("## Section 15", 1)[0]


def test_listed_ingredient_names_the_list():
    ing = [{"chemical": "benzene", "cas": "71-43-2", "concentration": "1%",
            "sds_original": True, "lists_unavailable": False,
            "listed_on": [{"list": "Taiwan MOENV Listed Toxic Chemical Substances"}]}]
    text = server._format_sds_draft(_payload(s15_ingredients=ing), "en")
    assert "benzene (CAS 71-43-2) — 1%**: Listed on: Taiwan MOENV" in text, text


def test_tool_forwards_the_request_and_never_the_intent(monkeypatch):
    """入参原样到后端（dict 与 model 都接）；`intent` 只记录不转发（CI-823）。"""
    seen = {}

    async def fake(ingredients, sections, region, lang=None):
        seen.update(ingredients=ingredients, sections=sections, region=region, lang=lang)
        return _payload()

    monkeypatch.setattr(server, "_direct_sds_draft", fake)
    server.set_caller_credential("sk-msds-test")
    try:
        res = asyncio.run(server.draft_sds_sections(
            [{"chemical": "acetone", "concentration": "60%"}], region="TW",
            sections=[15], lang="ja", intent="ZZ-INTENT"))
    finally:
        server.set_caller_credential(None)
    assert seen == {"ingredients": [{"chemical": "acetone", "concentration": "60%"}],
                    "sections": [15], "region": "TW", "lang": "ja"}
    assert "ZZ-INTENT" not in repr(seen)
    assert res.content[0].text.startswith(f"**{server._SDS_DRAFT_STRINGS['ja']['title']}**")
