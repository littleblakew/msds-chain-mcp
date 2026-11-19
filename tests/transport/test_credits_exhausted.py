r"""CI-1403: an empty wallet (backend 402) must reach the user as an upgrade path, not only as
an error sentence. A host's model rewrites error text and can drop its link, so the result also
carries a card payload in `_meta` (the channel the host renders without the model) with an
upgrade button restricted to one origin.

| guard | what reverting makes it fail |
|---|---|
| `test_402_is_returned_with_the_card` | drop the `except CreditsExhausted` in `_graceful_timeout` (the 402 is raised again, no card) |
| `test_chatgpt_gets_an_unflagged_result` | flag the result as an error for ChatGPT too (it then hides the card) |
| `test_other_clients_keep_the_error_flag` | drop the flag for every client (probes and retries then read an empty wallet as success), or match the Codex User-Agent as ChatGPT |
| `test_sds_section_gets_the_card_too` | leave `get_sds_section` out of `CARD_TOOLS` |
| `test_both_links_say_where_they_were_shown` | drop `with_source_params` from the text link or the card link |
| `test_card_is_on_the_wire_as_underscore_meta` | build the result without `meta=` |
| `test_text_says_not_run_and_carries_the_link` | drop the "NOT run" sentence or the link from `_credits_exhausted_result` |
| `test_off_origin_upgrade_link_is_replaced` | use the backend's `upgrade_url` without `allowed_upgrade_link` |
| `test_retold_keeps_the_upgrade_target` | re-raise the report path's longer sentence as a plain `ToolReason` |
| `test_tools_without_the_template_get_text_but_no_card` | attach the card payload regardless of `CARD_TOOLS`, or drop the error flag on those tools |
| `test_older_backend_without_link_falls_back_to_the_default_page` | drop the `or _UPGRADE_URL` fallback |
| `test_card_link_check_fails_closed_without_an_origin` | let `safeUrl` default to the SDS origin when none is given |
| `test_template_renders_the_exhausted_branch_and_pins_both_origins` | drop `renderExhausted`, or inject only one origin |
| `test_upgrade_origin_is_allowlisted_for_redirects` | leave the upgrade origin out of `openai/widgetCSP.redirect_domains` |

Async cases use `asyncio.run` (no pytest-asyncio in requirements-dev.txt).
"""
import asyncio
import contextlib

import request_identity
import server
import ui_compat_card as card

_CHATGPT_UA = "openai-mcp/1.0.0"


@contextlib.contextmanager
def _user_agent(ua):
    token = request_identity._caller_user_agent.set(ua)
    try:
        yield
    finally:
        request_identity._caller_user_agent.reset(token)

_DETAIL = {
    "error": "credit_floor", "balance": 0.0,
    "message": "Today's free credits are used up. They reset at 00:00 UTC. "
               "Upgrade to keep going now: https://msdschain.lagentbot.com/pricing",
    "upgrade_url": "https://msdschain.lagentbot.com/pricing",
    "resets_at": "2026-11-02T00:00:00+00:00",
}


class _Resp402:
    status_code = 402
    headers: dict = {}

    def __init__(self, detail):
        self._detail = detail

    def json(self):
        return {"detail": self._detail}


def _call(monkeypatch, detail=_DETAIL):
    async def _empty_wallet(*a, **kw):
        return server._billed_json(_Resp402(detail))
    monkeypatch.setattr(server, "_direct_compat", _empty_wallet)
    return asyncio.run(server.check_chemical_compatibility(chemicals=["acetone", "nitric acid"]))


def _text(result):
    return "\n".join(getattr(b, "text", "") or "" for b in result.content)


_CARD_UPGRADE = card.with_source_params(
    "https://msdschain.lagentbot.com/pricing", "card", "check_chemical_compatibility")


def test_402_is_returned_with_the_card(monkeypatch):
    res = _call(monkeypatch)
    payload = (res.meta or {}).get(card.CARD_META_KEY)
    assert payload and payload["kind"] == "credits_exhausted", res.meta
    assert payload["upgrade_url"] == _CARD_UPGRADE
    assert payload["resets_at"] == _DETAIL["resets_at"]
    # The structuredContent slot holds the tool's own data shape; an error must not pose as one.
    assert res.structured_content is None


def test_chatgpt_gets_an_unflagged_result(monkeypatch):
    """ChatGPT renders a component only on results whose `isError` is false; on an errored
    result the upgrade button never shows. The text still says the request was not run."""
    with _user_agent(_CHATGPT_UA):
        res = _call(monkeypatch)
    assert res.is_error is False and "NOT run" in _text(res)


def test_other_clients_keep_the_error_flag(monkeypatch):
    for ua in (None, "openai-mcp/1.0.0 (Codex)", "Claude-User", "codex-mcp-client/0.163.0"):
        with _user_agent(ua):
            res = _call(monkeypatch)
        assert res.is_error is True, ua
        assert res.meta[card.CARD_META_KEY]["kind"] == "credits_exhausted", ua


def test_card_is_on_the_wire_as_underscore_meta(monkeypatch):
    wire = _call(monkeypatch).model_dump(by_alias=True, exclude_none=True)
    assert wire.get("_meta", {}).get(card.CARD_META_KEY, {}).get("kind") == "credits_exhausted", wire


def test_text_says_not_run_and_carries_the_link(monkeypatch):
    text = _text(_call(monkeypatch))
    assert "NOT run" in text
    assert "https://msdschain.lagentbot.com/pricing" in text
    assert _DETAIL["message"] in text  # the backend's sentence is kept verbatim


def test_off_origin_upgrade_link_is_replaced(monkeypatch):
    evil = dict(_DETAIL, upgrade_url="https://evil.example/pricing")
    res = _call(monkeypatch, evil)
    assert "evil.example" not in _text(res).split("\n\n", 1)[1]
    assert res.meta[card.CARD_META_KEY]["upgrade_url"] == _CARD_UPGRADE


def test_retold_keeps_the_upgrade_target():
    e = server.CreditsExhausted("x", upgrade_url="https://msdschain.lagentbot.com/pricing",
                                resets_at="r")
    told = e.retold("x and more")
    assert isinstance(told, server.CreditsExhausted) and isinstance(told, RuntimeError)
    assert (told.upgrade_url, told.resets_at, str(told)) == (e.upgrade_url, "r", "x and more")


def test_template_renders_the_exhausted_branch_and_pins_both_origins():
    html = str(list(asyncio.run(server.mcp.read_resource(card.CARD_URI)))[0].content)
    assert 'p.kind === "credits_exhausted"' in html and "renderExhausted" in html
    assert f'var UPGRADE_ORIGIN = "{server._UPGRADE_ORIGIN}";' in html
    assert f'var ALLOWED_ORIGIN = "{server._CARD_LINK_ORIGIN}";' in html


def test_upgrade_origin_is_allowlisted_for_redirects():
    meta = card.resource_meta(server._CARD_LINK_ORIGIN, server._UPGRADE_ORIGIN)
    assert server._UPGRADE_ORIGIN in meta["openai/widgetCSP"]["redirect_domains"]
    assert server._CARD_LINK_ORIGIN in meta["openai/widgetCSP"]["redirect_domains"]


def test_tools_without_the_template_get_text_but_no_card(monkeypatch):
    async def _empty_wallet(*a, **kw):
        return server._billed_json(_Resp402(_DETAIL))
    monkeypatch.setattr(server, "_direct_risk", _empty_wallet)
    res = asyncio.run(server.get_chemical_risk_warnings(chemicals=["acetone"]))
    assert res.is_error is True
    assert "NOT run" in _text(res) and "https://msdschain.lagentbot.com/pricing" in _text(res)
    assert not (res.meta or {}).get(card.CARD_META_KEY), res.meta


def test_older_backend_without_link_falls_back_to_the_default_page(monkeypatch):
    res = _call(monkeypatch, {"error": "credit_floor", "balance": 0})
    assert server._UPGRADE_URL in _text(res)
    assert res.meta[card.CARD_META_KEY]["upgrade_url"] == _CARD_UPGRADE


def test_sds_section_gets_the_card_too(monkeypatch):
    async def _empty_wallet(*a, **kw):
        return server._billed_json(_Resp402(_DETAIL))
    monkeypatch.setattr(server, "_direct_sds_section", _empty_wallet)
    with _user_agent(_CHATGPT_UA):
        res = asyncio.run(server.get_sds_section(chemical="acetone", section=7))
    assert res.is_error is False and "NOT run" in _text(res)
    payload = res.meta[card.CARD_META_KEY]
    assert payload["kind"] == "credits_exhausted"
    assert "utm_content=get_sds_section" in payload["upgrade_url"]


def test_both_links_say_where_they_were_shown(monkeypatch):
    res = _call(monkeypatch)
    assert "utm_medium=text" in _text(res) and "utm_source=mcp" in _text(res)
    assert "utm_medium=card" in res.meta[card.CARD_META_KEY]["upgrade_url"]


def test_source_params_keep_what_the_link_already_has():
    url = card.with_source_params("https://msdschain.lagentbot.com/pricing?plan=pro&utm_source=x",
                                  "card", "t")
    assert "plan=pro" in url and "utm_source=x" in url and "utm_source=mcp" not in url
    assert url.startswith("https://msdschain.lagentbot.com/pricing?plan=pro&utm_source=x&")


def test_card_link_check_fails_closed_without_an_origin():
    html = card.card_html("https://a.example", "https://b.example")
    assert "if (!origin) return null;" in html
    assert "origin || ALLOWED_ORIGIN" not in html
