r"""Cards on the SDS tools (`get_sds_section`, `get_sds_document`): the tools point at the shared
template, and their results carry a card payload built only from what the text answer prints.

| guard | what reverting makes it fail |
|---|---|
| `test_card_tools_point_at_the_template` | drop `meta=ui_compat_card.tool_meta()` from one of the `CARD_TOOLS` decorators |
| `test_section_card_carries_the_text_and_its_source` | build the section payload without the text, the source or the disclosure lines |
| `test_section_card_marks_a_borrowed_text` | drop `cited_only` / `source_differs_note` (the card then credits the cited supplier with borrowed text) |
| `test_section_card_without_text_carries_the_why` | drop `note` (an empty card reads as "nothing hazardous") |
| `test_unresolved_section_card_says_what_the_answer_says` | build the card's note from the payload instead of the answer's own sentence (an unresolved name then gets an empty card) |
| `test_a_broken_payload_drops_the_card_not_the_answer` | build the payload inline instead of through `card_meta` |
| `test_section_card_is_cut_and_says_so` | drop the `_MAX_SECTION_CHARS` cut or the `truncated` flag |
| `test_document_card_opens_only_the_public_origin` | build the card link from `API_URL` (an internal address) instead of `_CARD_LINK_ORIGIN` |
| `test_document_card_states_the_price` | drop `open_cost_credits` from the document payload |
| `test_missing_document_card_says_why` | drop the card on the not-available branch, or its `message` |
| `test_template_renders_each_kind` | drop a `render*` branch from the template |
| `test_compat_verdicts_are_never_green` | map any verdict to the `ok` tone (the web app's rule: green reads as "approved") |

Async cases use `asyncio.run` (no pytest-asyncio in requirements-dev.txt).
"""
import asyncio
import re

import server
import ui_compat_card as card

_SECTION = {
    "chemical": "Acetone", "cas": "67-64-1",
    "content": "Keep container tightly closed. Store in a cool, well-ventilated place.",
    "supplier": "Supplier X", "region": "EU", "revision_date": "2024-01-01",
    "physical_form_disclosure": "This SDS describes the liquid form.",
    "data_source": "canonical",
}


def _fake(payload):
    async def _f(*a, **kw):
        return payload
    return _f


def _section(monkeypatch, payload, section=7):
    monkeypatch.setattr(server, "_direct_sds_section", _fake(payload))
    return asyncio.run(server.get_sds_section(chemical="acetone", section=section))


def _document(monkeypatch, payload):
    monkeypatch.setattr(server, "_direct_sds_document", _fake(payload))
    monkeypatch.setattr(server, "_require_api_key", lambda: None)
    return asyncio.run(server.get_sds_document(chemical="acetone"))


def _text(result):
    return "\n".join(getattr(b, "text", "") or "" for b in result.content)


def test_card_tools_point_at_the_template():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in card.CARD_TOOLS:
        meta = tools[name].meta or {}
        assert meta.get("openai/outputTemplate") == card.CARD_URI, (name, meta)
        assert meta.get("ui", {}).get("resourceUri") == card.CARD_URI, (name, meta)


def test_section_card_carries_the_text_and_its_source(monkeypatch):
    res = _section(monkeypatch, _SECTION)
    p = res.meta[card.CARD_META_KEY]
    assert p["kind"] == "sds_section" and p["section"] == 7 and p["title"] == "Handling and storage"
    assert p["text"] == _SECTION["content"] and p["truncated"] is False
    assert p["source"] == {"supplier": "Supplier X", "region": "EU", "revision_date": "2024-01-01",
                           "cited_only": False}
    assert p["notices"] == ["This SDS describes the liquid form."]
    # Nothing in the card that the text answer does not print.
    for s in (p["text"], p["source"]["supplier"], p["notices"][0]):
        assert s in _text(res), s


def test_section_card_marks_a_borrowed_text(monkeypatch):
    note = "This text comes from another SDS for the same substance."
    p = _section(monkeypatch, {**_SECTION, "section_source_differs": True, "section_source_note": note,
                               "corpus_source_info": {"supplier": "Supplier Y", "revision_date": "2019-05-05"}}
                 ).meta[card.CARD_META_KEY]
    assert p["source"]["cited_only"] is True and p["source_differs_note"] == note
    assert p["text_source"] == {"who": "Supplier Y", "revision_date": "2019-05-05"}


def test_section_card_without_text_carries_the_why(monkeypatch):
    why = "This SDS has no section 7 in the parsed text. This is a data gap, not a finding."
    p = _section(monkeypatch, {**_SECTION, "content": None, "no_section_text_note": why,
                               "no_section_text_reason": "section_not_present"}).meta[card.CARD_META_KEY]
    assert p["text"] is None and p["note"] == why


def test_unresolved_section_card_says_what_the_answer_says(monkeypatch):
    res = _section(monkeypatch, {"chemical": "xq-71", "unresolved": True,
                                 "no_section_text_reason": "no_sections_parsed",
                                 "no_section_text_note": "We hold this SDS but its sections were not parsed."})
    p = res.meta[card.CARD_META_KEY]
    assert p["text"] is None and p["source"] is None
    assert p["note"] and p["note"] in _text(res), p
    assert "were not parsed" not in p["note"], "the reason written for another cause leaked into the card"


def test_a_broken_payload_drops_the_card_not_the_answer(monkeypatch):
    def _boom(*a, **kw):
        raise ValueError("bad payload")
    monkeypatch.setattr(card, "sds_section_payload", _boom)
    res = _section(monkeypatch, _SECTION)
    assert res.meta is None and _SECTION["content"] in _text(res) and not res.is_error


def test_section_card_is_cut_and_says_so(monkeypatch):
    long = "x" * (card._MAX_SECTION_CHARS + 10)
    p = _section(monkeypatch, {**_SECTION, "content": long}).meta[card.CARD_META_KEY]
    assert len(p["text"]) == card._MAX_SECTION_CHARS and p["truncated"] is True


_DOC = {"available": True, "chemical_name": "Acetone", "cas": "67-64-1", "supplier": "Supplier X",
        "region": "EU", "revision_date": "2024-01-01", "pdf_url": "/msds/token/abc",
        "open_cost_credits": 1}


def test_document_card_opens_only_the_public_origin(monkeypatch):
    assert server.API_URL != server._CARD_LINK_ORIGIN, "precondition: tests point API_URL elsewhere"
    p = _document(monkeypatch, _DOC).meta[card.CARD_META_KEY]
    assert p["kind"] == "sds_document" and p["available"] is True
    assert p["url"] == server._CARD_LINK_ORIGIN + "/msds/token/abc"


def test_document_card_states_the_price(monkeypatch):
    res = _document(monkeypatch, _DOC)
    assert res.meta[card.CARD_META_KEY]["open_cost_credits"] == 1
    assert "charges 1 credit" in _text(res)


def test_missing_document_card_says_why(monkeypatch):
    msg = "No SDS document available for this chemical."
    p = _document(monkeypatch, {"available": False, "chemical_name": "Acetone", "cas": "67-64-1",
                                "message": msg}).meta[card.CARD_META_KEY]
    assert p["available"] is False and p["message"] == msg and p["url"] is None


def test_template_renders_each_kind():
    html = card.card_html("https://a.example", "https://b.example")
    for kind, fn in (("credits_exhausted", "renderExhausted"), ("sds_section", "renderSection"),
                     ("sds_document", "renderDocument")):
        assert f'p.kind === "{kind}" ? {fn}(p)' in html, kind
    assert ": renderCompat(p)" in html
    assert "--s-surface:" in html, "colour roles were not inlined"


def test_compat_verdicts_are_never_green():
    html = card.card_html("https://a.example", "https://b.example")
    table = re.search(r"var TONE = \{([^}]*)\}", html).group(1)
    assert '"ok"' not in table and "t-ok" not in html, table
