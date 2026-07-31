"""Version single-source guard.

The server version is displayed across many surfaces (the MCP `initialize`
handshake, the official MCP registry that ChatGPT/claude.ai read, npm, plugin
manifests). None auto-sync, so they used to drift. The repo-root VERSION file is
now the ONLY number a human edits; scripts/release.sh stamps it everywhere.

These tests fail CI if the wiring ever breaks, so the drift cannot silently
come back:
  - server.py's __version__ matches the VERSION file, and
  - that version is what actually surfaces as serverInfo.version in the MCP
    initialize handshake (what clients display), and
  - the JSON manifests are all stamped to the same number.
"""
import json
import os
import re

import release_metadata as rm
import server

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# JSON manifests whose "version" field(s) must equal the release version.
JSON_MANIFESTS = [
    "npm-package/package.json",
    "npm-package/server.json",
    "plugin.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".codex-plugin/plugin.json",
    ".agents/plugins/marketplace.json",
]


def _version_file() -> str:
    with open(os.path.join(ROOT, "VERSION")) as f:
        return f.read().strip()


def test_server_version_matches_version_file():
    assert server.__version__ == _version_file(), (
        "server.py __version__ drifted from VERSION — run scripts/release.sh"
    )


def test_serverinfo_reports_our_version():
    """The value clients (ChatGPT, claude.ai, raw MCP) actually display."""
    opts = server.mcp._mcp_server.create_initialization_options()
    assert opts.server_version == _version_file()


def _collect_versions(obj):
    """Recursively yield every value under a 'version' key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "version" and isinstance(v, str):
                yield v
            else:
                yield from _collect_versions(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _collect_versions(item)


def test_registry_description_within_limit():
    """The MCP registry (registry.modelcontextprotocol.io) rejects a server.json
    whose description exceeds 100 chars with HTTP 422 — catch it here, not at
    publish time (learned the hard way on the 1.4.0 release)."""
    with open(os.path.join(ROOT, "npm-package/server.json")) as f:
        desc = json.load(f)["description"]
    assert len(desc) <= 100, (
        f"npm-package/server.json description is {len(desc)} chars; the MCP "
        f"registry hard-limits it to 100 (422 on publish otherwise)"
    )


def test_all_json_manifests_stamped():
    want = _version_file()
    for rel in JSON_MANIFESTS:
        with open(os.path.join(ROOT, rel)) as f:
            data = json.load(f)
        versions = list(_collect_versions(data))
        assert versions, f"{rel}: no 'version' field found"
        for got in versions:
            assert got == want, (
                f"{rel}: version '{got}' != VERSION '{want}' — run scripts/release.sh"
            )


# The tool-count surfaces + patterns now live in release_metadata.py so the
# STAMPER (scripts/stamp_derived.py) and this GUARD read the same list — a new
# surface added to one is automatically seen by the other. Before CI-237 this
# list lived only here and the count was hand-maintained; the guard missed drift
# five times (npm package.json, two marketplace.json files, the root README, and
# skills/.../SKILL.md — the last stuck at 20). release.sh now stamps the count
# from the live registry, so this test only has to confirm the wiring held.
#
# server_remote.py is deliberately NOT a surface: its /health reads the live
# registry, so there is no literal to stamp or guard. Don't add it back — it
# would trip the "no 'N tools' claim found" assert below.


def test_advertised_tool_count_matches_registered():
    """Every "N tools" claim must equal the number of tools actually registered.

    release.sh derives the count from the live registry and stamps it into every
    surface in rm.TOOL_COUNT_SURFACES; this proves the stamp reached each one and
    nothing drifted. The assert on `claims` also catches a surface that quietly
    stopped carrying its literal (nothing left for the stamper to write).
    """
    import asyncio

    actual = len(asyncio.run(server.mcp.list_tools()))
    for rel in rm.TOOL_COUNT_SURFACES:
        with open(os.path.join(ROOT, rel)) as f:
            text = f.read()
        claims = [
            int(n)
            for pattern in rm.TOOL_COUNT_PATTERNS
            for n in re.findall(pattern, text, re.IGNORECASE)
        ]
        assert claims, (
            f"{rel}: no 'N tools' claim found — the stamper had nothing to write; "
            f"fix the copy or drop it from rm.TOOL_COUNT_SURFACES"
        )
        for n in claims:
            assert n == actual, (
                f"{rel} advertises {n} tools but {actual} are registered — "
                f"run scripts/release.sh to re-stamp"
            )


# --- Endpoint + transport wiring (CI-237) ---------------------------------
# Before CI-237 there was NO source of truth and NO test for the advertised
# endpoint or transport type, so the sse->streamable migration was six blind
# hand-edits. release.sh now stamps rm.PRIMARY_ENDPOINT and the per-schema
# transport; these guards confirm the stamp reached every surface.


def test_primary_endpoint_is_streamable_mcp_path():
    """The recommended endpoint is the streamable `/mcp` path, not `/sse`."""
    assert rm.PRIMARY_ENDPOINT.startswith("https://")
    assert rm.PRIMARY_ENDPOINT.endswith("/mcp"), (
        f"PRIMARY_ENDPOINT should be the streamable /mcp path, got "
        f"{rm.PRIMARY_ENDPOINT!r}"
    )


def test_advertised_endpoint_urls_match_primary():
    """Every advertised `.../mcp` URL equals rm.PRIMARY_ENDPOINT.

    Catches a surface left on an old host after an endpoint change. The `/sse`
    fallback URLs are intentionally excluded (the pattern only matches `/mcp`),
    so this does not fight the deliberate dual-transport docs.
    """
    for rel in rm.ENDPOINT_URL_SURFACES:
        with open(os.path.join(ROOT, rel)) as f:
            text = f.read()
        urls = rm.ENDPOINT_URL_RE.findall(text)
        assert urls, (
            f"{rel}: no primary '/mcp' endpoint URL found — the stamper had "
            f"nothing to write; fix the copy or drop it from ENDPOINT_URL_SURFACES"
        )
        for url in urls:
            assert url == rm.PRIMARY_ENDPOINT, (
                f"{rel} advertises {url!r} but PRIMARY_ENDPOINT is "
                f"{rm.PRIMARY_ENDPOINT!r} — run scripts/release.sh to re-stamp"
            )


def _find_mcp_blocks(obj):
    """Every dict carrying a machine-readable MCP-server `url` (ending /mcp)."""
    found = []
    if isinstance(obj, dict):
        u = obj.get("url")
        if isinstance(u, str) and u.endswith("/mcp"):
            found.append(obj)
        for v in obj.values():
            found += _find_mcp_blocks(v)
    elif isinstance(obj, list):
        for item in obj:
            found += _find_mcp_blocks(item)
    return found


def test_manifest_transport_type_matches_schema():
    """Each structured manifest declares its schema's transport type for the
    `/mcp` endpoint — "streamable-http" for the MCP registry's server.json,
    "http" for the plugin/npm client configs. Both name the streamable transport;
    the mapping is intentional, not drift. Also proves the connect url is `/mcp`,
    never `/sse` (no manifest advertises SSE as its primary transport)."""
    for rel, expected in rm.TRANSPORT_BY_SURFACE.items():
        with open(os.path.join(ROOT, rel)) as f:
            data = json.load(f)
        blocks = _find_mcp_blocks(data)
        assert blocks, (
            f"{rel}: no MCP-server block with a '/mcp' url found — "
            f"update rm.TRANSPORT_BY_SURFACE if the manifest changed shape"
        )
        for b in blocks:
            assert b["url"] == rm.PRIMARY_ENDPOINT, (
                f"{rel}: connect url {b['url']!r} != PRIMARY_ENDPOINT "
                f"{rm.PRIMARY_ENDPOINT!r} — run scripts/release.sh"
            )
            assert b.get("type") == expected, (
                f"{rel}: transport type {b.get('type')!r} != expected "
                f"{expected!r} — run scripts/release.sh"
            )
