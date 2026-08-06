"""Tests for direct-service MCP tools in server.py.

Covers tools backed by _direct_* helpers (no LLM), monkeypatching the service
layer so no real HTTP calls are made. Uses asyncio.run() (no pytest-asyncio
dependency — this repo's CI has no async plugin configured).
"""
import asyncio

import httpx
import server
from mcp.types import CallToolResult


# ---------------------------------------------------------------------------
# compare_sds_versions
# ---------------------------------------------------------------------------

def test_compare_sds_versions_has_newer(monkeypatch):
    """Tool returns structuredContent with has_newer=True and verdict_relevant."""
    async def fake_direct(chemical, supplier="", region=""):
        return {
            "has_newer": True,
            "cas": "7722-84-1",
            "from_version": 1,
            "to_version": 2,
            "from_revision_date": "2022-01-01",
            "to_revision_date": "2024-03-15",
            "hazard_changes": [{"field": "h_codes", "added": ["H272"], "removed": []}],
            "verdict_relevant": True,
        }

    monkeypatch.setattr(server, "_direct_compare_sds", fake_direct)
    res = asyncio.run(server.compare_sds_versions("hydrogen peroxide"))

    assert isinstance(res, CallToolResult)
    sc = res.structuredContent
    assert sc["has_newer"] is True
    assert sc["cas"] == "7722-84-1"
    assert sc["verdict_relevant"] is True
    text = res.content[0].text
    assert "1" in text and "2" in text
    assert "H272" in text
    assert "re-review" in text.lower() or "YES" in text


def test_compare_sds_versions_no_newer(monkeypatch):
    """Tool handles has_newer=False with resolved CAS."""
    async def fake_direct(chemical, supplier="", region=""):
        return {
            "has_newer": False,
            "cas": "7722-84-1",
            "from_version": None,
            "to_version": None,
            "from_revision_date": "",
            "to_revision_date": "",
            "hazard_changes": [],
            "verdict_relevant": False,
        }

    monkeypatch.setattr(server, "_direct_compare_sds", fake_direct)
    res = asyncio.run(server.compare_sds_versions("hydrogen peroxide", supplier="Sigma"))

    assert isinstance(res, CallToolResult)
    sc = res.structuredContent
    assert sc["has_newer"] is False
    text = res.content[0].text
    assert "latest" in text.lower() or "no newer" in text.lower()


def test_compare_sds_versions_unresolved(monkeypatch):
    """Tool handles unresolved chemical (empty cas)."""
    async def fake_direct(chemical, supplier="", region=""):
        return {
            "has_newer": False,
            "unresolved": [chemical],
            "cas": "",
        }

    monkeypatch.setattr(server, "_direct_compare_sds", fake_direct)
    res = asyncio.run(server.compare_sds_versions("xyzchemical123"))

    assert isinstance(res, CallToolResult)
    sc = res.structuredContent
    assert sc["cas"] == ""
    text = res.content[0].text
    assert "xyzchemical123" in text or "resolve" in text.lower() or "not" in text.lower()


def test_compare_sds_versions_passes_optional_args(monkeypatch):
    """supplier and region are forwarded to _direct_compare_sds."""
    captured = {}

    async def fake_direct(chemical, supplier="", region=""):
        captured["chemical"] = chemical
        captured["supplier"] = supplier
        captured["region"] = region
        return {
            "has_newer": False,
            "cas": "67-64-1",
            "from_version": None,
            "to_version": None,
            "from_revision_date": "",
            "to_revision_date": "",
            "hazard_changes": [],
            "verdict_relevant": False,
        }

    monkeypatch.setattr(server, "_direct_compare_sds", fake_direct)
    asyncio.run(server.compare_sds_versions("acetone", supplier="Merck", region="EU"))

    assert captured["chemical"] == "acetone"
    assert captured["supplier"] == "Merck"
    assert captured["region"] == "EU"


# ---------------------------------------------------------------------------
# Credit usage visibility (CI-39): cost + balance surfaced to the MCP caller
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status=200, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_parse_usage_reads_headers():
    r = _FakeResp(headers={"X-Msds-Credits-Cost": "3", "X-Msds-Credits-Balance": "197",
                           "X-Msds-Credits-Reason": "charged"})
    assert server._parse_usage(r) == {"cost": 3.0, "balance": 197.0, "reason": "charged"}


def test_parse_usage_none_without_header():
    assert server._parse_usage(_FakeResp()) is None


def test_billed_json_attaches_usage():
    r = _FakeResp(headers={"X-Msds-Credits-Cost": "3", "X-Msds-Credits-Balance": "197",
                           "X-Msds-Credits-Reason": "charged"}, body={"pairs": []})
    data = server._billed_json(r)
    assert data["pairs"] == []
    assert data["_usage"]["balance"] == 197.0


def test_billed_json_402_is_caller_friendly():
    r = _FakeResp(status=402, body={"detail": {"error": "credit_floor", "balance": 0.5}})
    try:
        server._billed_json(r)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        msg = str(e)
        assert "exhausted" in msg.lower()
        assert "0.5" in msg
        assert "msdschain.lagentbot.com" in msg


def test_usage_line_subscription_included():
    assert "plan" in server._usage_line({"cost": 0, "balance": -1, "reason": "subscription"}).lower()


def test_usage_line_charged_shows_cost_and_balance():
    line = server._usage_line({"cost": 3, "balance": 197, "reason": "charged"})
    assert "3" in line and "197" in line and "credit" in line.lower()


def test_compat_surfaces_usage_in_result(monkeypatch):
    async def fake(chemicals):
        return {
            "pairs": [{"chem1": "a", "chem2": "b", "level": "compatible",
                       "reason": "x", "source": "y"}],
            "unresolved": [],
            "_usage": {"cost": 3, "balance": 197, "reason": "charged"},
        }
    monkeypatch.setattr(server, "_direct_compat", fake)
    res = asyncio.run(server.check_chemical_compatibility(["a", "b"]))
    assert res.structuredContent["usage"]["balance"] == 197
    assert "197" in res.content[0].text and "Balance" in res.content[0].text


def test_lookup_tool_does_not_leak_usage_key(monkeypatch):
    """Lookup tools expose structuredContent=data directly; the internal _usage key
    that _billed_json attaches must be stripped (CI-39 leak fix)."""
    async def fake_ppe(chemicals):
        return {"results": [{"chemical_name": "acetone", "ppe": {}}], "unresolved": [],
                "_usage": {"cost": 0, "balance": 100, "reason": "free"}}
    monkeypatch.setattr(server, "_direct_ppe", fake_ppe)
    res = asyncio.run(server.get_ppe_recommendation(["acetone"]))
    assert "_usage" not in res.structuredContent
    assert "results" in res.structuredContent  # real data still there


def test_strip_usage_helper():
    assert server._strip_usage({"a": 1, "_usage": {"x": 1}}) == {"a": 1}
    assert server._strip_usage({"a": 1}) == {"a": 1}  # no-op when absent


# ---------------------------------------------------------------------------
# quick-chat read-timeout degrades gracefully (no opaque empty error)
#
# Root cause: a slow-but-valid backend /quick-chat turn (a gpt-5-mini reasoning
# summary legitimately takes 30-60s; an unlisted chemical was observed at ~55.7s)
# overruns the MCP client read-timeout. httpx.ReadTimeout stringifies to "", so the
# tool surfaced `Error executing tool ask_chemical_safety: ` — an opaque dead end that
# violates the "no data → tell the user, ask for the MSDS" contract. On timeout the
# tool must return an actionable message, never raise an empty error.
# ---------------------------------------------------------------------------

class _TimeoutClient:
    """Fake httpx.AsyncClient whose POST always raises a read-timeout (empty str,
    exactly as httpx.ReadTimeout stringifies in production)."""
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **k):
        raise server.httpx.ReadTimeout("")


def test_ask_chemical_safety_read_timeout_is_graceful(monkeypatch):
    from request_identity import set_caller_credential
    monkeypatch.setattr(server.httpx, "AsyncClient", _TimeoutClient)
    set_caller_credential("sk-msds-caller")  # pass _require_api_key → reach the POST
    res = asyncio.run(server.ask_chemical_safety("处理一种叫 NovaClean ZX-7 的专有清洁剂，有哪些危害？"))

    # Must NOT raise; must return a usable result the caller can act on.
    assert isinstance(res, CallToolResult)
    text = res.content[0].text
    assert text.strip(), "timeout answer must not be empty"
    # Actionable: guides the user to the MSDS/CAS path (C1/C2), not a dead end.
    lowered = text.lower()
    assert ("msds" in lowered or "sds" in lowered or "cas" in lowered
            or "上传" in text or "重试" in text or "retry" in lowered)


def test_timeout_llm_budget_exceeds_slow_reasoning_turn():
    """A single gpt-5-mini reasoning summary is documented at 30-60s and an unlisted
    chemical was measured at ~55.7s. The client read-timeout must clear that with
    headroom, otherwise valid slow responses are discarded as empty errors."""
    assert server.TIMEOUT_LLM >= 90.0


def test_timeout_multi_exceeds_default_fast_path_timeout():
    """Multi-component v2 endpoints do work linear in component count (per-component
    SDS resolution; compatibility/batch additionally make up to MAX_LLM_FALLBACK_PAIRS
    serial LLM-escalation round-trips). Prod evidence: 9-21 chemical batch_safety_check
    and 5-component get_chemical_risk_warnings calls failed at ~15,02x ms — pinned to
    the plain TIMEOUT=15.0 client ceiling, not a backend 5xx. TIMEOUT_MULTI must give
    them more headroom than the fast path, but stay under TIMEOUT_LLM (it is NOT the
    multi-turn quick-chat budget)."""
    assert server.TIMEOUT_MULTI > server.TIMEOUT
    assert server.TIMEOUT_MULTI < server.TIMEOUT_LLM
    assert server.TIMEOUT_MULTI >= 30.0


# ---------------------------------------------------------------------------
# CI-176: the long budget must cover EVERY multi-component tool, not just the two
# that happened to be raised first. A user hit the 15s wall twice on
# get_chemical_risk_warnings (5-component excipient formulation) and churned.
# Conversely single-chemical / pure-lookup tools must stay on the short budget:
# their Prod p90 is <1.5s, so a longer budget can only make a broken call spin
# longer, never turn a failure into a success.
# ---------------------------------------------------------------------------

# helper name -> (call args, expects long budget?)
_DIRECT_TIMEOUT_EXPECTATIONS = {
    # multi-component: signature takes `chemicals: list[str]`
    "_direct_compat": ((["acetone", "ethanol"],), True),
    "_direct_risk": ((["acetone", "ethanol"],), True),
    "_direct_batch": ((["acetone", "ethanol"],), True),
    "_direct_ppe": ((["acetone", "ethanol"],), True),
    "_direct_storage": ((["acetone", "ethanol"],), True),
    "_direct_exposure": ((["acetone", "ethanol"],), True),
    "_direct_transport": ((["acetone", "ethanol"],), True),
    "_direct_waste": ((["acetone", "ethanol"],), True),
    # single-chemical / pure lookup: stay on the fast budget
    "_direct_emergency": (("acetone", "spill"), False),
    "_direct_compliance": (("acetone", ["EU"]), False),
    "_direct_online_search": (("acetone",), False),
    "_direct_sds_section": (("acetone", 2), False),
    "_direct_compare_sds": (("acetone",), False),
    "_direct_sds_document": (("acetone",), False),
}


class _TimeoutCapturingClient:
    """Stand-in for httpx.AsyncClient that records the configured timeout and
    returns an empty-JSON 200 for any get/post."""

    captured: list = []

    def __init__(self, *a, timeout=None, **kw):
        type(self).captured.append(timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _respond(self, *a, **kw):
        return httpx.Response(200, json={}, request=httpx.Request("POST", "http://test"))

    post = _respond
    get = _respond


def test_every_multi_component_direct_helper_uses_long_timeout(monkeypatch):
    """Behavioral, not just constant-level: actually invoke each direct helper and
    assert which budget it configured on its httpx client."""
    for name, (args, wants_long) in _DIRECT_TIMEOUT_EXPECTATIONS.items():
        _TimeoutCapturingClient.captured = []
        monkeypatch.setattr(server.httpx, "AsyncClient", _TimeoutCapturingClient)
        asyncio.run(getattr(server, name)(*args))
        assert len(_TimeoutCapturingClient.captured) == 1, name
        got = _TimeoutCapturingClient.captured[0]
        expected = server.TIMEOUT_MULTI if wants_long else server.TIMEOUT
        assert got == expected, (
            f"{name}: expected timeout={expected} "
            f"({'multi-component' if wants_long else 'single/lookup'}), got {got}"
        )


def test_direct_timeout_expectations_cover_every_direct_helper():
    """Guard against a new _direct_* helper silently defaulting to the wrong budget."""
    helpers = {n for n in dir(server) if n.startswith("_direct_")}
    assert helpers == set(_DIRECT_TIMEOUT_EXPECTATIONS), (
        f"unclassified: {helpers - set(_DIRECT_TIMEOUT_EXPECTATIONS)}, "
        f"stale: {set(_DIRECT_TIMEOUT_EXPECTATIONS) - helpers}"
    )


# ---------------------------------------------------------------------------
# CI-61: check_regulatory_compliance defaults to EU+US when no region is given —
# a stateless tool can't ask, so it must DISCLOSE the default, never let it read
# as "checked everywhere".
# ---------------------------------------------------------------------------

def _fake_compliance():
    async def fake(chemical, regions):
        return {"chemical": chemical, "cas": "50-00-0", "summary_level": "high",
                "region_results": [{"region": r, "status": "restricted", "flags": []} for r in regions],
                "unresolved": []}
    return fake


def test_regulatory_default_regions_are_disclosed(monkeypatch):
    monkeypatch.setattr(server, "_direct_compliance", _fake_compliance())
    res = asyncio.run(server.check_regulatory_compliance(["formaldehyde"]))  # no regions
    text = res.content[0].text.lower()
    assert "no regions specified" in text and "default" in text
    assert "cn" in text and "jp" in text  # tells the user others are available
    assert res.structuredContent["regions_defaulted"] is True
    assert res.structuredContent["regions"] == ["EU", "US"]


def test_regulatory_explicit_regions_no_disclosure(monkeypatch):
    monkeypatch.setattr(server, "_direct_compliance", _fake_compliance())
    res = asyncio.run(server.check_regulatory_compliance(["formaldehyde"], regions=["EU"]))
    assert "no regions specified" not in res.content[0].text.lower()
    assert res.structuredContent["regions_defaulted"] is False
    assert res.structuredContent["regions"] == ["EU"]


# ---------------------------------------------------------------------------
# CI-55: a direct/v2 tool (batch_safety_check et al.) that hits a client read-timeout
# (backend tail-latency / cold start just after a deploy) must degrade to an
# actionable retry message, never the opaque `Error executing tool …:` (empty str).
# ---------------------------------------------------------------------------

def _timeout_direct():
    async def fake(*a, **k):
        raise server.httpx.ReadTimeout("")
    return fake


def test_batch_safety_check_timeout_is_graceful(monkeypatch):
    monkeypatch.setattr(server, "_direct_batch", _timeout_direct())
    res = asyncio.run(server.batch_safety_check(["acetone", "bleach"]))
    text = res if isinstance(res, str) else res.content[0].text
    assert text.strip(), "timeout answer must not be empty"
    low = text.lower()
    assert "retry" in low or "try again" in low or "重试" in text or "timed out" in low


# Truth source for the public tool surface. This exact set is the single
# authority for "how many tools does the MCP server expose" — every outward
# description (plugin.json / .claude-plugin / .codex-plugin / npm README +
# server.json / frontend DocsPage table / docs) must agree with it. Adding or
# removing a tool MUST update this set in the same change, so drift breaks the
# build instead of shipping an inconsistent count (SciTuu-review finding, 2026-07).
EXPECTED_TOOLS = frozenset({
    "check_chemical_compatibility",
    "get_chemical_risk_warnings",
    "check_regulatory_compliance",
    "ask_chemical_safety",
    "get_ppe_recommendation",
    "get_storage_guidance",
    "get_emergency_response",
    "get_exposure_limits",
    "get_transport_classification",
    "create_audit_session",
    "get_audit_report",
    "search_chemical_database",
    "get_sds_section",
    "get_sds_document",
    "get_chemical_alternatives",
    "validate_protocol_chemicals",
    "check_mixing_order",
    "get_waste_disposal",
    "compare_sds_versions",
    "upload_msds_pdf",
    "batch_safety_check",
    "check_regulatory_lists",
    "search_msds_online",
})


def test_direct_tools_still_registered(monkeypatch):
    """The graceful-timeout wrapper must not break FastMCP tool registration."""
    import asyncio as _a
    tools = _a.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert "batch_safety_check" in names
    # Exact match — no missing tools, no stray extras. Update EXPECTED_TOOLS
    # (and every outward count/list) when the tool surface changes.
    assert names == EXPECTED_TOOLS, (
        f"tool surface drifted: missing={EXPECTED_TOOLS - names}, "
        f"unexpected={names - EXPECTED_TOOLS}"
    )
    assert len(EXPECTED_TOOLS) == 23


# ---------------------------------------------------------------------------
# CI-83: quick-chat timeouts must log success=False, not inflate the metric
#
# _quick_chat catches httpx.TimeoutException and returns a DEGRADED dict with
# "_timed_out": True rather than raising. Without this sentinel the tools'
# finally-block logs success=True — inflating mcp_call_logs success rate and
# the growth-skill [G]/[H] aha metrics. The fix: each tool detects the
# sentinel before returning and sets success=False / error_msg="timeout".
# The user-facing graceful answer is unchanged.
# ---------------------------------------------------------------------------

def _timed_out_quick_chat(message):
    """Fake _quick_chat that returns the degraded sentinel dict (CI-83)."""
    return {
        "answer": server._TIMEOUT_ANSWER.get(server.LANG, server._TIMEOUT_ANSWER["en"]),
        "tool_results": [],
        "_timed_out": True,
    }


def _make_fake_quick_chat():
    """Async wrapper so it can be monkeypatched onto server._quick_chat."""
    async def _fake(message):
        return _timed_out_quick_chat(message)
    return _fake


def _capture_log_call():
    """Return (patched coroutine, captured-args list)."""
    captured = []

    async def _fake_log(tool_name, chemicals, duration_ms, success, error_message=None, input_params=None):
        captured.append({
            "tool_name": tool_name,
            "success": success,
            "error_message": error_message,
        })
    return _fake_log, captured


def test_ask_chemical_safety_timeout_logs_failure(monkeypatch):
    """ask_chemical_safety: timed-out _quick_chat → _log_call(success=False)."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    monkeypatch.setattr(server, "_quick_chat", _make_fake_quick_chat())
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.ask_chemical_safety("Is bleach safe?"))

    # User still gets the graceful degraded answer (not an error raise)
    assert isinstance(res, CallToolResult)
    assert res.content[0].text.strip()

    # Logged as failure
    assert len(captured) == 1
    assert captured[0]["tool_name"] == "ask_chemical_safety"
    assert captured[0]["success"] is False
    assert captured[0]["error_message"] == "timeout"


def test_get_chemical_alternatives_timeout_logs_failure(monkeypatch):
    """get_chemical_alternatives: timed-out _quick_chat → _log_call(success=False)."""
    monkeypatch.setattr(server, "_quick_chat", _make_fake_quick_chat())
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.get_chemical_alternatives("benzene", use_case="solvent"))

    assert isinstance(res, CallToolResult)
    assert captured[0]["success"] is False
    assert captured[0]["error_message"] == "timeout"


def test_validate_protocol_chemicals_timeout_logs_failure(monkeypatch):
    """validate_protocol_chemicals: timed-out _quick_chat → _log_call(success=False)."""
    monkeypatch.setattr(server, "_quick_chat", _make_fake_quick_chat())
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.validate_protocol_chemicals("Mix 10 mL acetone with ethanol."))

    assert isinstance(res, CallToolResult)
    assert captured[0]["success"] is False
    assert captured[0]["error_message"] == "timeout"


def test_check_mixing_order_timeout_logs_failure(monkeypatch):
    """check_mixing_order: timed-out _quick_chat → _log_call(success=False)."""
    monkeypatch.setattr(server, "_quick_chat", _make_fake_quick_chat())
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.check_mixing_order("sulfuric acid", "water"))

    assert isinstance(res, CallToolResult)
    assert captured[0]["success"] is False
    assert captured[0]["error_message"] == "timeout"


def test_check_regulatory_lists_timeout_logs_failure(monkeypatch):
    """check_regulatory_lists: timed-out _quick_chat → _log_call(success=False)."""
    monkeypatch.setattr(server, "_quick_chat", _make_fake_quick_chat())
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.check_regulatory_lists("formaldehyde"))

    assert isinstance(res, CallToolResult)
    assert captured[0]["success"] is False
    assert captured[0]["error_message"] == "timeout"


def test_quick_chat_timeout_user_answer_unchanged(monkeypatch):
    """The user-facing degraded answer is not affected by the sentinel fix —
    _quick_result still renders the graceful message from _TIMEOUT_ANSWER."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    monkeypatch.setattr(server, "_quick_chat", _make_fake_quick_chat())
    monkeypatch.setattr(server, "_log_call", _capture_log_call()[0])

    res = asyncio.run(server.ask_chemical_safety("test"))
    text = res.content[0].text
    # The graceful degraded answer (not an empty/opaque error)
    lowered = text.lower()
    assert ("timed out" in lowered or "retry" in lowered or "try again" in lowered
            or "重试" in text or "超时" in text)


# ---------------------------------------------------------------------------
# CI-84: get_sds_document — returns signed SDS PDF URL or parsed-text fallback
#
# Backend: GET /api/v2/sds-document-url?chemical={chemical}
# Header: X-API-Key: {per-user key}
#
# Three outcome paths:
#   available=True  → pdf_url is a *relative* path that must be prefixed with
#                     API_URL to produce a usable absolute URL.
#   available=False, message has "parsed" → parsed-only, suggest get_sds_section.
#   available=False, message has "not found" → unknown chemical, suggest upload.
#   No credential → authentication required message.
# ---------------------------------------------------------------------------

class _FakeGetClient:
    """Fake httpx.AsyncClient that stubs GET /api/v2/sds-document-url."""

    def __init__(self, status: int, body: dict):
        self._status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        return _FakeResp(status=self._status, body=self._body)


def _patch_sds_doc_client(monkeypatch, status: int, body: dict):
    monkeypatch.setattr(
        server.httpx, "AsyncClient",
        lambda **kw: _FakeGetClient(status, body),
    )


def test_get_sds_document_available_returns_absolute_url(monkeypatch):
    """available=True: pdf_url relative path is prefixed with API_URL."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    body = {
        "available": True,
        "chemical_name": "Acetone",
        "cas": "67-64-1",
        "supplier": "Sigma-Aldrich",
        "revision_date": "2023-05-01",
        "region": "US",
        "record_id": 42,
        "pdf_url": "/msds/token/abc123",
    }
    _patch_sds_doc_client(monkeypatch, 200, body)
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.get_sds_document("acetone"))

    assert isinstance(res, CallToolResult)
    sc = res.structuredContent
    assert sc["available"] is True
    assert sc["cas"] == "67-64-1"
    assert sc["supplier"] == "Sigma-Aldrich"
    # pdf_url in structuredContent must be absolute
    assert sc["pdf_url"].startswith("http")
    assert "/msds/token/abc123" in sc["pdf_url"]
    # text must contain the absolute URL
    text = res.content[0].text
    assert "/msds/token/abc123" in text
    # source attribution present
    assert "Sigma-Aldrich" in text or "Sigma" in text
    # usage hint present
    assert "5 min" in text or "browser" in text or "curl" in text
    # logged success
    assert len(captured) == 1
    assert captured[0]["success"] is True


def test_get_sds_document_parsed_only_no_pdf(monkeypatch):
    """available=False + parsed-only message → suggest get_sds_section."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    body = {
        "available": False,
        "chemical_name": "Acetone",
        "cas": "67-64-1",
        "message": "No source PDF available for this record (parsed text only); use get_sds_section to query specific sections.",
    }
    _patch_sds_doc_client(monkeypatch, 200, body)
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.get_sds_document("acetone"))

    assert isinstance(res, CallToolResult)
    sc = res.structuredContent
    assert sc["available"] is False
    text = res.content[0].text
    assert "get_sds_section" in text
    assert captured[0]["success"] is True


def test_get_sds_document_unknown_chemical(monkeypatch):
    """available=False + not-found message → suggest upload."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    body = {
        "available": False,
        "message": "Chemical 'XYZ-99' not found in the database.",
    }
    _patch_sds_doc_client(monkeypatch, 200, body)
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.get_sds_document("XYZ-99"))

    assert isinstance(res, CallToolResult)
    sc = res.structuredContent
    assert sc["available"] is False
    text = res.content[0].text
    # Should mention upload path
    assert "upload" in text.lower() or "not found" in text.lower()
    assert captured[0]["success"] is True


def test_get_sds_document_no_credential_returns_auth_message(monkeypatch):
    """No credential → authentication required message without HTTP call."""
    from request_identity import set_caller_credential
    set_caller_credential("")  # clear credential

    # The tool should bail before making any HTTP call when unauthenticated.
    # We still patch the client to ensure no real request fires.
    _patch_sds_doc_client(monkeypatch, 200, {})
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.get_sds_document("acetone"))

    assert isinstance(res, CallToolResult)
    text = res.content[0].text.lower()
    assert "api key" in text or "authenticat" in text or "msds_api_key" in text


def test_get_sds_document_includes_pdf_hash_when_backend_provides_it(monkeypatch):
    """CI-308: pdf_hash must reach structuredContent — it's the only exact key
    for reconciling get_sds_document against get_sds_section's own source."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    body = {
        "available": True,
        "chemical_name": "Acetone",
        "cas": "67-64-1",
        "supplier": "Carl Roth",
        "revision_date": "2024-09-18",
        "region": "EU",
        "record_id": 42,
        "pdf_url": "/msds/token/abc123",
        "pdf_hash": "deadbeef" * 8,
    }
    _patch_sds_doc_client(monkeypatch, 200, body)
    monkeypatch.setattr(server, "_log_call", _capture_log_call()[0])

    res = asyncio.run(server.get_sds_document("acetone"))

    assert res.structuredContent["pdf_hash"] == "deadbeef" * 8


def test_get_sds_document_pdf_hash_absent_on_old_backend(monkeypatch):
    """Old backend (pre CI-308) doesn't send pdf_hash yet — must degrade to
    None, not KeyError, and must not fabricate a placeholder hash."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    body = {
        "available": True,
        "chemical_name": "Acetone",
        "cas": "67-64-1",
        "supplier": "Carl Roth",
        "revision_date": "2024-09-18",
        "region": "EU",
        "record_id": 42,
        "pdf_url": "/msds/token/abc123",
        # no pdf_hash key at all
    }
    _patch_sds_doc_client(monkeypatch, 200, body)
    monkeypatch.setattr(server, "_log_call", _capture_log_call()[0])

    res = asyncio.run(server.get_sds_document("acetone"))

    assert res.structuredContent["pdf_hash"] is None


# ---------------------------------------------------------------------------
# CI-308: get_sds_section must surface the section's own source (supplier /
# region / revision date) in the TEXT output, not just structuredContent —
# that's what makes a mismatch against get_sds_document visible to a human
# or an LLM reading the answer, instead of requiring someone to diff two
# raw structuredContent blobs or read the SDS's own letterhead text.
# ---------------------------------------------------------------------------

def _patch_sds_section_direct(monkeypatch, data: dict):
    async def fake(chemical, section):
        return data
    monkeypatch.setattr(server, "_direct_sds_section", fake)


def test_get_sds_section_shows_source_when_backend_provides_it(monkeypatch):
    _patch_sds_section_direct(monkeypatch, {
        "chemical": "Acetone",
        "cas": "67-64-1",
        "content": "Store in a cool, well-ventilated area away from oxidizers.",
        "data_source": "canonical",
        "supplier": "GB CLP",
        "region": "UK",
        "revision_date": "2023-05-24",
    })
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.get_sds_section("acetone", 7))

    assert isinstance(res, CallToolResult)
    text = res.content[0].text
    assert "GB CLP" in text
    assert "UK" in text
    assert "2023-05-24" in text
    assert captured[0]["success"] is True


def test_get_sds_section_omits_source_line_when_backend_lacks_it(monkeypatch):
    """Old backend (pre CI-308) doesn't send supplier/region/revision_date on
    this endpoint yet — the tool must degrade to simply not showing the
    source line, never a placeholder like 'unknown supplier' that would read
    as a deliberate (and misleading) answer."""
    _patch_sds_section_direct(monkeypatch, {
        "chemical": "Acetone",
        "cas": "67-64-1",
        "content": "Store in a cool, well-ventilated area away from oxidizers.",
        "data_source": "canonical",
        # no supplier / region / revision_date keys at all
    })
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.get_sds_section("acetone", 7))

    assert isinstance(res, CallToolResult)
    text = res.content[0].text
    assert "**Source:**" not in text
    assert "unknown supplier" not in text.lower()
    assert captured[0]["success"] is True


# ---------------------------------------------------------------------------
# CI-89: traceability surface — documents links + traceability labels
#
# Backend adds a top-level `documents` list (blob-backed SDS descriptors) and
# per-conclusion `traceability` field ("sds_backed" | "rule_based") to the
# responses of: /compatibility/check, /risk-warnings, /ppe-recommendation,
# /batch-safety, and /quick-chat.
#
# Core (server.py) presents these:
# - check_chemical_compatibility: pair lines say "Basis (rule):" instead of
#   plain "Source:"; documents section appended; structuredContent carries `documents`.
# - get_chemical_risk_warnings: each warning heading appended with
#   "[Source: SDS document]" or "[Basis: rule/standard]"; documents section;
#   structuredContent carries `documents` and per-warning `traceability`.
# - get_ppe_recommendation: same labelling per result item; documents; structuredContent.
# - ask_chemical_safety (_quick_result): documents appended to text and
#   structuredContent["documents"].
# - batch_safety_check: pair labels carry "[Basis (rule)]"; risk warnings labelled;
#   documents appended; structuredContent["documents"].
#
# Red lines tested here:
# - When backend omits `documents` → section not rendered, structuredContent documents=[].
# - When backend omits `traceability` per item → fall back to documents inference.
# - URLs in documents are passed through verbatim (no domain rewrite).
# - Tool count does NOT change (still 22 tools).
# ---------------------------------------------------------------------------

_SAMPLE_DOCUMENTS = [
    {
        "chemical": "acetone",
        "chemical_name": "Acetone",
        "cas": "67-64-1",
        "supplier": "Sigma-Aldrich",
        "revision_date": "2023-05-01",
        "region": "US",
        "record_id": 1,
        "sds_document_url": "https://mcp.lagentbot.com/msds/token/tok123",
    }
]


# --- _format_sds_documents helper ---

def test_format_sds_documents_empty():
    assert server._format_sds_documents([]) == ""


def test_format_sds_documents_renders_link():
    docs = [_SAMPLE_DOCUMENTS[0]]
    out = server._format_sds_documents(docs)
    assert "📄" in out
    assert "Acetone" in out
    assert "Sigma-Aldrich" in out
    assert "2023-05-01" in out
    assert "https://mcp.lagentbot.com/msds/token/tok123" in out


def test_format_sds_documents_url_verbatim():
    """URL must be output as-is — no domain rewriting."""
    docs = [{"chemical": "x", "chemical_name": "X", "cas": "", "supplier": "",
             "revision_date": "", "region": "", "record_id": 0,
             "sds_document_url": "https://example-custom-domain.com/token/abc"}]
    out = server._format_sds_documents(docs)
    assert "https://example-custom-domain.com/token/abc" in out


# --- check_chemical_compatibility + CI-89 ---

def test_compat_ci89_basis_rule_label(monkeypatch):
    """Pair lines now say 'Basis (rule)' for rule_based traceability."""
    async def fake_compat(chemicals):
        return {
            "pairs": [{
                "chem1": "acetone", "chem2": "water",
                "level": "compatible", "reason": "no reaction",
                "source": "GHS rule engine", "traceability": "rule_based",
            }],
            "unresolved": [],
            "documents": _SAMPLE_DOCUMENTS,
        }
    monkeypatch.setattr(server, "_direct_compat", fake_compat)
    res = asyncio.run(server.check_chemical_compatibility(["acetone", "water"]))
    assert isinstance(res, CallToolResult)
    text = res.content[0].text
    # Must label the basis as rule-based
    assert "Basis (rule)" in text
    # Must include SDS documents section
    assert "📄" in text
    assert "https://mcp.lagentbot.com/msds/token/tok123" in text
    # structuredContent carries documents
    sc = res.structuredContent
    assert sc["documents"] == _SAMPLE_DOCUMENTS
    # pairs carry traceability
    assert sc["pairs"][0]["traceability"] == "rule_based"


def test_compat_ci89_no_documents(monkeypatch):
    """When backend omits 'documents', section not rendered, structuredContent documents=[]."""
    async def fake_compat(chemicals):
        return {
            "pairs": [{"chem1": "a", "chem2": "b", "level": "compatible",
                       "reason": "ok", "source": "rule"}],
            "unresolved": [],
            # no 'documents' key
        }
    monkeypatch.setattr(server, "_direct_compat", fake_compat)
    res = asyncio.run(server.check_chemical_compatibility(["a", "b"]))
    assert "📄" not in res.content[0].text
    assert res.structuredContent["documents"] == []


def test_compat_ci89_sds_source_label_when_sds_backed(monkeypatch):
    """When a pair has traceability='sds_backed', label shows 'Source (SDS)'."""
    async def fake_compat(chemicals):
        return {
            "pairs": [{
                "chem1": "a", "chem2": "b",
                "level": "caution", "reason": "check",
                "source": "SDS Section 7", "traceability": "sds_backed",
            }],
            "unresolved": [], "documents": [],
        }
    monkeypatch.setattr(server, "_direct_compat", fake_compat)
    res = asyncio.run(server.check_chemical_compatibility(["a", "b"]))
    text = res.content[0].text
    assert "Source (SDS)" in text


# --- get_chemical_risk_warnings + CI-89 ---

def test_risk_ci89_sds_backed_label(monkeypatch):
    """Warning with traceability='sds_backed' gets [Source: SDS document] label."""
    async def fake_risk(chemicals):
        return {
            "warnings": [{
                "chemical": "Acetone", "level": "low",
                "description": "Flammable", "mitigation": "Avoid flame",
                "reference": "SDS Sec 2", "traceability": "sds_backed",
            }],
            "unresolved": [],
            "documents": _SAMPLE_DOCUMENTS,
        }
    monkeypatch.setattr(server, "_direct_risk", fake_risk)
    res = asyncio.run(server.get_chemical_risk_warnings(["acetone"]))
    assert isinstance(res, CallToolResult)
    text = res.content[0].text
    assert "[Source: SDS document]" in text
    assert "📄" in text
    assert "https://mcp.lagentbot.com/msds/token/tok123" in text
    sc = res.structuredContent
    assert sc["documents"] == _SAMPLE_DOCUMENTS
    assert sc["warnings"][0]["traceability"] == "sds_backed"


def test_risk_ci89_rule_based_label(monkeypatch):
    """Warning with traceability='rule_based' gets [Basis: rule/standard] label."""
    async def fake_risk(chemicals):
        return {
            "warnings": [{
                "chemical": "Methanol", "level": "high",
                "description": "Toxic", "mitigation": "Gloves", "traceability": "rule_based",
            }],
            "unresolved": [], "documents": [],
        }
    monkeypatch.setattr(server, "_direct_risk", fake_risk)
    res = asyncio.run(server.get_chemical_risk_warnings(["methanol"]))
    text = res.content[0].text
    assert "[Basis: rule/standard]" in text


def test_risk_ci89_infers_label_from_documents(monkeypatch):
    """When traceability field absent but chemical is in documents, infer sds_backed."""
    async def fake_risk(chemicals):
        return {
            "warnings": [{
                "chemical": "Acetone", "level": "low",
                "description": "Flammable", "mitigation": "Avoid flame",
                # no traceability field
            }],
            "unresolved": [],
            "documents": _SAMPLE_DOCUMENTS,  # Acetone is in documents
        }
    monkeypatch.setattr(server, "_direct_risk", fake_risk)
    res = asyncio.run(server.get_chemical_risk_warnings(["acetone"]))
    text = res.content[0].text
    assert "[Source: SDS document]" in text


def test_risk_ci89_no_documents(monkeypatch):
    """When backend omits 'documents', section not rendered, structuredContent documents=[]."""
    async def fake_risk(chemicals):
        return {
            "warnings": [{"chemical": "x", "level": "low", "description": "d", "mitigation": "m"}],
            "unresolved": [],
        }
    monkeypatch.setattr(server, "_direct_risk", fake_risk)
    res = asyncio.run(server.get_chemical_risk_warnings(["x"]))
    assert "📄" not in res.content[0].text
    assert res.structuredContent["documents"] == []


# --- get_ppe_recommendation + CI-89 ---

def test_ppe_ci89_sds_backed_label(monkeypatch):
    """PPE result with traceability='sds_backed' gets [Source: SDS document] in header."""
    async def fake_ppe(chemicals):
        return {
            "results": [{
                "chemical_name": "Acetone", "cas": "67-64-1",
                "signal_word": "Warning", "minimum_ppe_level": "B",
                "ppe": {"gloves": ["nitrile"]},
                "traceability": "sds_backed",
            }],
            "unresolved": [],
            "documents": _SAMPLE_DOCUMENTS,
        }
    monkeypatch.setattr(server, "_direct_ppe", fake_ppe)
    res = asyncio.run(server.get_ppe_recommendation(["acetone"]))
    assert isinstance(res, CallToolResult)
    text = res.content[0].text
    assert "[Source: SDS document]" in text
    assert "📄" in text
    assert "https://mcp.lagentbot.com/msds/token/tok123" in text
    sc = res.structuredContent
    assert sc["documents"] == _SAMPLE_DOCUMENTS


def test_ppe_ci89_rule_based_label(monkeypatch):
    """PPE result with traceability='rule_based' gets [Basis: rule/standard] in header."""
    async def fake_ppe(chemicals):
        return {
            "results": [{
                "chemical_name": "Methanol", "cas": "67-56-1",
                "signal_word": "Danger", "minimum_ppe_level": "C",
                "ppe": {}, "traceability": "rule_based",
            }],
            "unresolved": [], "documents": [],
        }
    monkeypatch.setattr(server, "_direct_ppe", fake_ppe)
    res = asyncio.run(server.get_ppe_recommendation(["methanol"]))
    text = res.content[0].text
    assert "[Basis: rule/standard]" in text


def test_ppe_ci89_no_documents(monkeypatch):
    """When backend omits 'documents', section not rendered, structuredContent documents=[]."""
    async def fake_ppe(chemicals):
        return {
            "results": [{"chemical_name": "X", "cas": "N/A", "signal_word": "W",
                         "minimum_ppe_level": "A", "ppe": {}}],
            "unresolved": [],
        }
    monkeypatch.setattr(server, "_direct_ppe", fake_ppe)
    res = asyncio.run(server.get_ppe_recommendation(["x"]))
    assert "📄" not in res.content[0].text
    assert res.structuredContent["documents"] == []


def test_ppe_ci89_internal_usage_key_not_leaked(monkeypatch):
    """_usage internal key must not appear in structuredContent (existing CI-39 contract)."""
    async def fake_ppe(chemicals):
        return {
            "results": [{"chemical_name": "A", "cas": "1", "signal_word": "W",
                         "minimum_ppe_level": "B", "ppe": {}}],
            "unresolved": [], "documents": [],
            "_usage": {"cost": 1, "balance": 99, "reason": "charged"},
        }
    monkeypatch.setattr(server, "_direct_ppe", fake_ppe)
    res = asyncio.run(server.get_ppe_recommendation(["a"]))
    assert "_usage" not in res.structuredContent


# --- ask_chemical_safety / _quick_result + CI-89 ---

def test_quick_result_ci89_documents_in_text_and_structured():
    """_quick_result appends documents section and includes documents in structuredContent."""
    data = {
        "answer": "Acetone is flammable.",
        "tool_results": [],
        "documents": _SAMPLE_DOCUMENTS,
    }
    res = server._quick_result(data)
    assert isinstance(res, CallToolResult)
    text = res.content[0].text
    assert "Acetone is flammable." in text
    assert "📄" in text
    assert "https://mcp.lagentbot.com/msds/token/tok123" in text
    sc = res.structuredContent
    assert sc["documents"] == _SAMPLE_DOCUMENTS


def test_quick_result_ci89_no_documents():
    """_quick_result with no documents key: section absent, structuredContent documents=[]."""
    data = {"answer": "Safe.", "tool_results": []}
    res = server._quick_result(data)
    assert "📄" not in res.content[0].text
    assert res.structuredContent["documents"] == []


def test_ask_chemical_safety_ci89_documents_propagated(monkeypatch):
    """ask_chemical_safety: backend returns documents → propagated to caller."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    async def fake_quick_chat(message):
        return {
            "answer": "Use nitrile gloves.",
            "tool_results": [],
            "documents": _SAMPLE_DOCUMENTS,
        }
    monkeypatch.setattr(server, "_quick_chat", fake_quick_chat)
    monkeypatch.setattr(server, "_log_call", _capture_log_call()[0])

    res = asyncio.run(server.ask_chemical_safety("PPE for acetone?"))
    assert isinstance(res, CallToolResult)
    text = res.content[0].text
    assert "📄" in text
    assert "https://mcp.lagentbot.com/msds/token/tok123" in text
    assert res.structuredContent["documents"] == _SAMPLE_DOCUMENTS


# --- batch_safety_check + CI-89 ---

def test_batch_ci89_compat_basis_label(monkeypatch):
    """Batch compat pairs get [Basis (rule)] label in text."""
    async def fake_batch(chemicals):
        return {
            "compatibility": {
                "summary": {"total": 1, "compatible": 1, "caution": 0, "incompatible": 0},
                "pairs": [{
                    "chem1": "acetone", "chem2": "water",
                    "level": "compatible", "reason": "no reaction",
                    "traceability": "rule_based",
                }],
            },
            "risk_warnings": [],
            "unresolved": [],
            "documents": _SAMPLE_DOCUMENTS,
        }
    monkeypatch.setattr(server, "_direct_batch", fake_batch)
    res = asyncio.run(server.batch_safety_check(["acetone", "water"]))
    assert isinstance(res, CallToolResult)
    text = res.content[0].text
    assert "Basis (rule)" in text
    assert "📄" in text
    assert "https://mcp.lagentbot.com/msds/token/tok123" in text
    sc = res.structuredContent
    assert sc["documents"] == _SAMPLE_DOCUMENTS
    assert sc["compatibility"]["pairs"][0]["traceability"] == "rule_based"


def test_batch_ci89_risk_sds_backed_label(monkeypatch):
    """Batch risk warnings with traceability='sds_backed' get [Source: SDS document]."""
    async def fake_batch(chemicals):
        return {
            "compatibility": {"summary": {}, "pairs": []},
            "risk_warnings": [{
                "chemical": "Acetone", "level": "low",
                "description": "Flammable", "mitigation": "No ignition sources",
                "traceability": "sds_backed",
            }],
            "unresolved": [],
            "documents": _SAMPLE_DOCUMENTS,
        }
    monkeypatch.setattr(server, "_direct_batch", fake_batch)
    res = asyncio.run(server.batch_safety_check(["acetone", "water"]))
    text = res.content[0].text
    assert "[Source: SDS document]" in text
    sc = res.structuredContent
    assert sc["risk_warnings"][0]["traceability"] == "sds_backed"


def test_batch_ci89_no_documents(monkeypatch):
    """When backend omits 'documents', section not rendered, structuredContent documents=[]."""
    async def fake_batch(chemicals):
        return {
            "compatibility": {"summary": {}, "pairs": []},
            "risk_warnings": [],
            "unresolved": [],
        }
    monkeypatch.setattr(server, "_direct_batch", fake_batch)
    res = asyncio.run(server.batch_safety_check(["acetone", "water"]))
    assert "📄" not in res.content[0].text
    assert res.structuredContent["documents"] == []


# ---------------------------------------------------------------------------
# get_audit_report — defensive URL construction
#
# Backend may return an absolute URL (when public_base_url is set on prod)
# or a relative path (when unset / older deploy).  Core must:
#   - pass absolute URLs through unchanged (no double-prefix)
#   - prepend API_URL only when the backend returns a relative path
# ---------------------------------------------------------------------------

class _FakeGetReportClient:
    """Minimal httpx.AsyncClient stub for get_audit_report."""

    def __init__(self, url_value: str):
        self._url_value = url_value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        return _FakeResp(status=200, body={"url": self._url_value})


def _patch_report_client(monkeypatch, url_value: str):
    monkeypatch.setattr(
        server.httpx, "AsyncClient",
        lambda **kw: _FakeGetReportClient(url_value),
    )


def test_get_audit_report_absolute_url_not_double_prefixed(monkeypatch):
    """When backend returns an absolute URL, core must NOT prepend API_URL again."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    absolute_url = (
        "https://api.msdschain.lagentbot.com"
        "/sessions/DEMO-ABC123/report/pdf?t=tok&lang=en"
    )
    _patch_report_client(monkeypatch, absolute_url)
    log_fn, _ = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.get_audit_report("DEMO-ABC123"))

    assert isinstance(res, CallToolResult)
    sc = res.structuredContent
    assert sc["report_url"] == absolute_url, (
        f"Absolute URL must be passed through unchanged, got: {sc['report_url']!r}"
    )
    assert sc["report_url"].count("https://") == 1, "URL must not be double-prefixed"
    assert "DEMO-ABC123" in res.content[0].text


def test_get_audit_report_relative_url_prefixed_with_api_url(monkeypatch):
    """When backend returns a relative URL, core must prepend API_URL."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    relative_url = "/sessions/DEMO-ABC456/report/pdf?t=tok"
    _patch_report_client(monkeypatch, relative_url)
    log_fn, _ = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    res = asyncio.run(server.get_audit_report("DEMO-ABC456"))

    assert isinstance(res, CallToolResult)
    sc = res.structuredContent
    assert sc["report_url"].startswith("http"), (
        f"Relative URL must be prefixed with API_URL, got: {sc['report_url']!r}"
    )
    assert "/sessions/DEMO-ABC456/report/pdf?t=tok" in sc["report_url"]


def test_tool_count_unchanged():
    """CI-89 must not add or remove tools — still 22 tools registered."""
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert len(names) == 23, f"Expected 23 tools, got {len(names)}: {sorted(names)}"


def test_search_msds_online_found(monkeypatch):
    """found → labelled PubChem result (source=pubchem, not-a-signed-SDS note)."""
    async def fake_direct(chemical_name="", cas_number=""):
        return {
            "status": "found",
            "chemical_name": "Acetonitrile",
            "cas_number": "75-05-8",
            "ghs": {"signal_word": "Danger", "h_codes": ["H225", "H302"],
                    "hazard_statements": ["..."], "pictograms": ["GHS02"]},
            "source": "pubchem",
        }

    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")
    monkeypatch.setattr(server, "_direct_online_search", fake_direct)
    res = asyncio.run(server.search_msds_online(chemical_name="acetonitrile"))

    assert isinstance(res, CallToolResult)
    assert res.structuredContent["source"] == "pubchem"
    assert res.structuredContent["cas_number"] == "75-05-8"
    text = res.content[0].text
    assert "75-05-8" in text and "H225" in text
    assert "not a" in text.lower() and "sds" in text.lower()  # labelled unverified


def test_search_msds_online_not_found(monkeypatch):
    """not_found → returns the message (no fabricated hazards)."""
    async def fake_direct(chemical_name="", cas_number=""):
        return {"status": "not_found", "message": "'zzz' not found on PubChem. Upload an SDS or skip."}

    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")
    monkeypatch.setattr(server, "_direct_online_search", fake_direct)
    res = asyncio.run(server.search_msds_online(chemical_name="zzz"))
    assert isinstance(res, str)
    assert "not found on PubChem" in res


# ---------------------------------------------------------------------------
# CI-169: upload_msds_pdf failure paths must log success=False AND be actionable
#
# The hosted core resolves pdf_source on ITS OWN filesystem, so a remote client's
# local path can never exist. Prod: the deepest user called it twice, got the
# silent `File not found` string back, and it was recorded as success=t /
# duration_ms=0 with zero rows written. A non-raising early return is still a
# failed upload — it must not inflate the success rate, and it must tell the
# caller what to do instead.
# ---------------------------------------------------------------------------

def test_upload_missing_local_file_logs_failure(monkeypatch, tmp_path):
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    missing = str(tmp_path / "definitely-not-here.pdf")
    res = asyncio.run(server.upload_msds_pdf(missing))

    assert isinstance(res, str)
    assert len(captured) == 1
    assert captured[0]["tool_name"] == "upload_msds_pdf"
    assert captured[0]["success"] is False, "unreadable path must not count as success"
    assert captured[0]["error_message"]


def test_upload_missing_local_file_message_is_actionable(tmp_path):
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    res = asyncio.run(server.upload_msds_pdf(str(tmp_path / "nope.pdf")))
    low = res.lower()
    # names the constraint (server-side filesystem, not the caller's)
    assert "server" in low and ("your machine" in low or "your computer" in low)
    # offers both routes a remote caller can actually take
    assert "https" in low and "msdschain.lagentbot.com" in low
    assert "url" in low
    # says what they get out of it
    assert "credit" in low
    # and is not the old bare error
    assert not res.startswith("File not found:")


def test_upload_empty_file_logs_failure(monkeypatch, tmp_path):
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    res = asyncio.run(server.upload_msds_pdf(str(empty)))

    assert isinstance(res, str)
    assert captured[0]["success"] is False
    assert captured[0]["error_message"] == "empty pdf content"


def test_upload_without_credential_logs_failure(monkeypatch):
    from request_identity import set_caller_credential
    set_caller_credential(None)

    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)

    try:
        res = asyncio.run(server.upload_msds_pdf("/tmp/whatever.pdf"))
    finally:
        set_caller_credential("sk-msds-test")  # don't leak the cleared credential

    assert isinstance(res, str) and "API key" in res
    assert captured[0]["success"] is False
    assert captured[0]["error_message"] == "no caller credential"


def test_upload_docstring_warns_remote_clients_about_local_paths():
    """The tool description is what an LLM client reads before choosing pdf_source.
    It must steer remote callers to a URL / the web uploader, not a local path."""
    doc = server.upload_msds_pdf.__doc__.lower()
    assert "remote" in doc and "url" in doc
    assert "self-hosted" in doc or "stdio" in doc


def test_upload_docstring_tells_model_to_use_inline_base64():
    """CI-169 remainder: a remote client holding the file's bytes (the common
    case — user just uploaded a PDF into ChatGPT/claude.ai) must be told to
    base64-encode and pass them inline, not go hunting for a local path."""
    doc = server.upload_msds_pdf.__doc__.lower()
    assert "base64" in doc
    assert "data:application/pdf;base64" in doc


# ---------------------------------------------------------------------------
# CI-169 (remainder): inline base64 PDF content — data URI and bare base64
#
# A public HTTPS URL is *also* something a remote client rarely has: a PDF the
# user just uploaded into ChatGPT/claude.ai lives in that client's sandbox with
# no public URL. So pdf_source must also accept the raw file bytes, inline,
# base64-encoded — either as a data URI or a long bare base64 string that
# decodes to a real PDF (%PDF magic bytes). Guardrails: must decode to %PDF,
# capped at 10 MB decoded, and a base64 decode failure must return
# success=False with a readable message, never a raw exception.
# ---------------------------------------------------------------------------

import base64 as _b64  # local alias — avoid clashing with server's own `base64` import


def _minimal_pdf_bytes(padding: int = 100) -> bytes:
    """A PDF-shaped byte string long enough that its base64 form clears the
    100-char bare-base64 sniff threshold in _decode_bare_base64_pdf."""
    return b"%PDF-1.4\n" + (b"A" * padding) + b"\n%%EOF"


# _FakeResp (defined near the top of this file) has no .content attribute —
# GET responses in upload_msds_pdf read `.content`, not `.json()`. Small
# standalone helper rather than touching the shared class used by every other
# test in this file.
def _fake_resp_with_content(content: bytes):
    resp = _FakeResp(status=200)
    resp.content = content
    return resp


class _FakeUploadFlowClient:
    """Fake httpx.AsyncClient covering the full upload_msds_pdf network flow:
    GET <pdf_source> (http(s) URL branch only), POST {API_URL}/sessions
    (auto session create), POST {API_URL}/sessions/{id}/upload (multipart).
    One instance is reused across both `async with httpx.AsyncClient(...)`
    call sites in the tool so upload_calls captures the real multipart body."""

    def __init__(self, get_body: bytes | None = None):
        self._get_body = get_body if get_body is not None else b""
        self.upload_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        return _fake_resp_with_content(self._get_body)

    async def post(self, url, **kw):
        if url.endswith("/sessions"):
            return _FakeResp(status=200, body={"session_id": "SESS-1"})
        if "/upload" in url:
            self.upload_calls.append(kw)
            return _FakeResp(status=200, body={
                "results": [{
                    "status": "success",
                    "chemical_name": "Acetone",
                    "cas_number": "67-64-1",
                    "risk_level": "medium",
                    "fields": {},
                    "missing": [],
                }],
                "summary": {"success": 1, "warning": 0, "failed": 0},
            })
        raise AssertionError(f"unexpected POST {url}")


def _patch_upload_flow_client(monkeypatch, get_body: bytes | None = None):
    fake = _FakeUploadFlowClient(get_body=get_body)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: fake)
    return fake


def _set_upload_credential():
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")


def test_upload_https_url_still_works(monkeypatch):
    """CI-169 regression guard: adding inline-base64 support must not break
    the existing http(s) URL branch."""
    _set_upload_credential()
    pdf_bytes = _minimal_pdf_bytes()
    fake = _patch_upload_flow_client(monkeypatch, get_body=pdf_bytes)

    res = asyncio.run(server.upload_msds_pdf("https://supplier.example.com/acetone_sds.pdf"))

    assert isinstance(res, CallToolResult)
    assert res.structuredContent["session_id"] == "SESS-1"
    assert len(fake.upload_calls) == 1
    sent_filename, sent_bytes, sent_ctype = fake.upload_calls[0]["files"]["file"]
    assert sent_filename == "acetone_sds.pdf"
    assert sent_bytes == pdf_bytes


def test_upload_local_path_still_works(monkeypatch, tmp_path):
    """CI-169 regression guard: local-path handling (self-hosted stdio case)
    must still work after adding inline-base64 support."""
    _set_upload_credential()
    pdf_bytes = _minimal_pdf_bytes()
    real_file = tmp_path / "acetone.pdf"
    real_file.write_bytes(pdf_bytes)
    fake = _patch_upload_flow_client(monkeypatch)

    res = asyncio.run(server.upload_msds_pdf(str(real_file)))

    assert isinstance(res, CallToolResult)
    assert len(fake.upload_calls) == 1
    sent_filename, sent_bytes, sent_ctype = fake.upload_calls[0]["files"]["file"]
    assert sent_filename == "acetone.pdf"
    assert sent_bytes == pdf_bytes


def test_upload_data_uri_pdf_success(monkeypatch):
    """A data:application/pdf;base64,<...> URI is decoded and uploaded."""
    _set_upload_credential()
    pdf_bytes = _minimal_pdf_bytes()
    fake = _patch_upload_flow_client(monkeypatch)
    data_uri = "data:application/pdf;base64," + _b64.b64encode(pdf_bytes).decode()

    res = asyncio.run(server.upload_msds_pdf(data_uri))

    assert isinstance(res, CallToolResult), f"expected success, got: {res!r}"
    assert len(fake.upload_calls) == 1
    sent_filename, sent_bytes, sent_ctype = fake.upload_calls[0]["files"]["file"]
    assert sent_bytes == pdf_bytes, "decoded bytes must exactly match the original PDF"
    assert sent_filename == "upload.pdf", "no filename given -> default upload.pdf"


def test_upload_data_uri_respects_explicit_filename(monkeypatch):
    """The optional `filename` arg names the file when pdf_source has none."""
    _set_upload_credential()
    pdf_bytes = _minimal_pdf_bytes()
    fake = _patch_upload_flow_client(monkeypatch)
    data_uri = "data:application/pdf;base64," + _b64.b64encode(pdf_bytes).decode()

    res = asyncio.run(server.upload_msds_pdf(data_uri, filename="my_acetone_sds.pdf"))

    assert isinstance(res, CallToolResult)
    sent_filename = fake.upload_calls[0]["files"]["file"][0]
    assert sent_filename == "my_acetone_sds.pdf"


def test_upload_bare_base64_pdf_success(monkeypatch):
    """A long bare base64 string (no data: prefix) that decodes to %PDF is
    treated as inline content, not a local path."""
    _set_upload_credential()
    pdf_bytes = _minimal_pdf_bytes()
    fake = _patch_upload_flow_client(monkeypatch)
    bare_b64 = _b64.b64encode(pdf_bytes).decode()
    assert len(bare_b64) >= 100, "test fixture must clear the bare-base64 sniff threshold"

    res = asyncio.run(server.upload_msds_pdf(bare_b64))

    assert isinstance(res, CallToolResult), f"expected success, got: {res!r}"
    sent_bytes = fake.upload_calls[0]["files"]["file"][1]
    assert sent_bytes == pdf_bytes


def test_upload_bare_base64_non_pdf_falls_through_to_local_path(monkeypatch):
    """A long base64-looking string that decodes fine but ISN'T a PDF must not
    be swallowed as inline content — it falls through to local-path handling
    (and fails there, since it isn't a real path either)."""
    _set_upload_credential()
    not_a_pdf = b"this is definitely not a pdf file, just padding " * 3
    bare_b64 = _b64.b64encode(not_a_pdf).decode()
    assert len(bare_b64) >= 100

    res = asyncio.run(server.upload_msds_pdf(bare_b64))

    assert isinstance(res, str)
    assert "server" in res.lower() and "your machine" in res.lower(), (
        "non-PDF base64 blob must fall through to the local-path message, not "
        "be accepted as inline content"
    )


def test_upload_data_uri_non_pdf_content_rejected(monkeypatch):
    """A data URI that decodes fine but isn't a PDF (wrong magic bytes) is an
    explicit inline-content declaration, so it must be rejected outright, not
    silently fall through to local-path handling."""
    _set_upload_credential()
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)
    not_a_pdf = _b64.b64encode(b"hello world, not a pdf").decode()
    data_uri = f"data:application/pdf;base64,{not_a_pdf}"

    res = asyncio.run(server.upload_msds_pdf(data_uri))

    assert isinstance(res, str)
    assert "%PDF" in res or "pdf" in res.lower()
    assert captured[0]["success"] is False
    assert captured[0]["error_message"]


def test_upload_data_uri_over_size_limit_rejected(monkeypatch):
    """Decoded content over the 10 MB inline cap is rejected with a clear
    message pointing at the URL alternative, even though it IS a valid PDF."""
    _set_upload_credential()
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)
    oversized = b"%PDF-1.4\n" + b"0" * (server._MAX_INLINE_PDF_BYTES + 1)
    data_uri = "data:application/pdf;base64," + _b64.b64encode(oversized).decode()

    res = asyncio.run(server.upload_msds_pdf(data_uri))

    assert isinstance(res, str)
    low = res.lower()
    assert "mb" in low and ("10" in low or "limit" in low)
    assert captured[0]["success"] is False
    assert captured[0]["error_message"]


def test_upload_data_uri_corrupt_base64_returns_error_not_exception(monkeypatch):
    """A malformed base64 payload in an explicit data: URI must return
    success=False with a readable message — never propagate a raw
    binascii.Error/exception out of the tool call."""
    _set_upload_credential()
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)
    data_uri = "data:application/pdf;base64,not-valid-base64!!!***"

    res = asyncio.run(server.upload_msds_pdf(data_uri))

    assert isinstance(res, str)
    assert "base64" in res.lower() or "decode" in res.lower()
    assert captured[0]["success"] is False
    assert captured[0]["error_message"]


def test_upload_data_uri_missing_base64_marker_rejected(monkeypatch):
    """A data: URI without `;base64` (e.g. a URL-encoded text data URI) is not
    something this tool supports — reject clearly instead of misparsing.

    The payload here is deliberately a VALID base64 encoding of a real PDF —
    if the ";base64" marker check were ever dropped, this would silently
    decode and succeed instead of being rejected, so this is a stronger check
    than a payload that merely fails to decode either way.
    """
    _set_upload_credential()
    log_fn, captured = _capture_log_call()
    monkeypatch.setattr(server, "_log_call", log_fn)
    valid_b64_of_real_pdf = _b64.b64encode(_minimal_pdf_bytes()).decode()

    res = asyncio.run(server.upload_msds_pdf(f"data:application/pdf,{valid_b64_of_real_pdf}"))

    assert isinstance(res, str)
    assert "base64" in res.lower()
    assert captured[0]["success"] is False


# ---------------------------------------------------------------------------
# CI-248 / CI-250: dropped logs must leave a trace, and the trace must never
# be an empty error_message.
# ---------------------------------------------------------------------------

def test_error_text_never_empty_for_blank_str_exception():
    """httpx.ReadTimeout (and its sibling TimeoutException subclasses) stringify
    to "" — this was the measured root cause of 66% of failed mcp_call_logs
    rows having no error_message (CI-250). _error_text must never return "" or
    a value that round-trips to '' after class-name stripping."""
    exc = httpx.ReadTimeout("")  # explicit blank message, matching prod behavior
    assert str(exc) == ""  # sanity: confirms the underlying stringify-empty bug
    text = server._error_text(exc)
    assert text != ""
    assert "ReadTimeout" in text
    assert "no message" in text


def test_error_text_preserves_real_message():
    exc = httpx.HTTPStatusError("Client error '402' for url X", request=None, response=None)
    text = server._error_text(exc)
    assert "HTTPStatusError" in text
    assert "402" in text


def test_error_text_truncates_to_500_chars():
    exc = ValueError("x" * 1000)
    text = server._error_text(exc)
    assert len(text) <= 500


def test_log_call_post_failure_is_logged_not_swallowed(monkeypatch, caplog):
    """CI-248: a bare `except Exception: pass` in _log_call meant a dropped
    call-log POST left no trace anywhere. It must now surface via logger.warning
    without raising into the caller (fire-and-forget contract preserved)."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    class _FailingClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("")  # blank message, mirrors prod exceptions

    monkeypatch.setattr(server.httpx, "AsyncClient", _FailingClient)

    import logging
    caplog.set_level(logging.WARNING, logger="msds_mcp")
    before = server._call_log_post_failures

    # Must not raise — this is awaited from a tool's `finally` block.
    asyncio.run(server._log_call("search_chemical_database", ["acetone"], 12, True))

    assert server._call_log_post_failures == before + 1
    assert any("mcp_call_log_post_failed" in r.message for r in caplog.records)
    assert any("search_chemical_database" in r.message for r in caplog.records)


def test_log_call_treats_non_2xx_response_as_failure(monkeypatch, caplog):
    """A non-2xx response from /mcp/call-log itself (e.g. 422 validation, 500)
    was previously accepted silently since no exception was raised without
    raise_for_status(). It must now be counted and logged too."""
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")

    class _FakeResponse:
        status_code = 500

        def raise_for_status(self):
            raise httpx.HTTPStatusError("500", request=None, response=self)

    class _ErrorClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResponse()

    monkeypatch.setattr(server.httpx, "AsyncClient", _ErrorClient)

    import logging
    caplog.set_level(logging.WARNING, logger="msds_mcp")
    before = server._call_log_post_failures

    asyncio.run(server._log_call("get_sds_document", ["acetone"], 5, True))

    assert server._call_log_post_failures == before + 1
    assert any("mcp_call_log_post_failed" in r.message for r in caplog.records)


# ── CI-169 review 补测：filename 消毒 / 预解码尺寸闸门 / 日志不含内容 ──────────

def test_sanitize_upload_filename_strips_traversal():
    """filename 直通到后端会被拼进磁盘路径，必须先削成裸文件名。"""
    from server import _sanitize_upload_filename as san
    assert san("../../etc/passwd") == "etc_passwd.pdf" or "/" not in san("../../etc/passwd")
    for bad in ["../../x.pdf", "/etc/cron.d/x", "..\\..\\win.pdf", "a/b/c.pdf"]:
        out = san(bad)
        assert "/" not in out and "\\" not in out, out
        assert not out.startswith("."), out
        assert out.lower().endswith(".pdf"), out


def test_sanitize_upload_filename_appends_pdf_suffix():
    """后端按扩展名分发，没有 .pdf 会被判成不支持的类型、静默不解析。"""
    from server import _sanitize_upload_filename as san
    assert san("acetone_sds") == "acetone_sds.pdf"
    assert san(None) == "upload.pdf"
    assert san("   ") == "upload.pdf"


def test_reject_oversize_encoded_before_decoding():
    """超大 payload 必须在 b64decode 之前就被拒，否则先把内存吃满再报错。"""
    import pytest
    from server import _reject_oversize_encoded, _InlinePdfError, _MAX_INLINE_PDF_BYTES
    ok = "A" * 1000
    _reject_oversize_encoded(ok)  # 不该抛
    huge = "A" * (_MAX_INLINE_PDF_BYTES * 4 // 3 + 1000)
    with pytest.raises(_InlinePdfError):
        _reject_oversize_encoded(huge)


def test_upload_log_never_contains_inline_payload(monkeypatch):
    """内联 base64 就是文档字节本身，调用日志里不能出现它的任何片段。"""
    import asyncio, base64 as _b64, server as _s
    logged = {}

    async def _fake_log(tool, sid, dur, success, err, params):
        logged["params"] = params

    monkeypatch.setattr(_s, "_log_call", _fake_log)
    monkeypatch.setattr(_s, "get_caller_credential", lambda: None)  # 早退即可，日志仍写
    payload = _b64.b64encode(b"%PDF-1.4 secret customer sds " + b"x" * 400).decode()
    data_uri = "data:application/pdf;base64," + payload
    asyncio.run(_s.upload_msds_pdf(data_uri))
    assert payload[:40] not in logged["params"], logged["params"]
    assert "secret" not in logged["params"]
    assert "chars" in logged["params"]
