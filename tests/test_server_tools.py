"""Tests for direct-service MCP tools in server.py.

Covers tools backed by _direct_* helpers (no LLM), monkeypatching the service
layer so no real HTTP calls are made. Uses asyncio.run() (no pytest-asyncio
dependency — this repo's CI has no async plugin configured).
"""
import asyncio

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


def test_timeout_compat_exceeds_default_fast_path_timeout():
    """check_chemical_compatibility / batch_safety_check can each make up to
    MAX_LLM_FALLBACK_PAIRS (backend, compatibility_engine.py) serial LLM-escalation
    calls for uncategorized chemical pairs, on top of DB work — a real ~1-3s+
    round-trip per call. Prod evidence: 9-21 chemical batch_safety_check calls all
    failed at ~15024ms, pinned to the plain TIMEOUT=15.0 ceiling. TIMEOUT_COMPAT
    must give these two pairwise-heavy tools more headroom than the rest of the
    no-LLM v2 fast path, but must stay under TIMEOUT_LLM (it is NOT the
    multi-turn quick-chat budget)."""
    assert server.TIMEOUT_COMPAT > server.TIMEOUT
    assert server.TIMEOUT_COMPAT < server.TIMEOUT_LLM
    assert server.TIMEOUT_COMPAT >= 30.0


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
    assert len(EXPECTED_TOOLS) == 22


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
    assert len(names) == 22, f"Expected 22 tools, got {len(names)}: {sorted(names)}"
