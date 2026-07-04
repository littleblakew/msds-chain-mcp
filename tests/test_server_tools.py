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


def test_direct_tools_still_registered(monkeypatch):
    """The graceful-timeout wrapper must not break FastMCP tool registration."""
    import asyncio as _a
    tools = _a.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert "batch_safety_check" in names
    assert len(names) >= 15  # full public tool surface still present
