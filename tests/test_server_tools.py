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
