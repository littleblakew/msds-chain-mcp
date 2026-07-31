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


# Files whose user-facing copy states the tool count ("N tools"). These are what
# ChatGPT / claude.ai / npm / the Claude Code + Codex plugin listings show.
TOOL_COUNT_SURFACES = [
    # The npm PAGE's description is read from this file — the most externally
    # visible surface of them all, and the one this list originally missed.
    # Consequence (2026-07-25): the 22→23 fix landed everywhere except here, so npm
    # advertised "22 MCP tools" while CI stayed green; it took a manual four-way
    # audit (CI-167) to catch. Listed first as a reminder that the guard is only
    # worth as much as its surface list.
    "npm-package/package.json",
    "npm-package/server.json",
    "npm-package/README.md",
    "plugin.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    # 2026-07-29: these two were ALSO missing — both still advertised "22 chemical
    # safety tools" after the 22→23 fix. Same failure mode the comment above warns
    # about, twice over: the surface list was short AND the regex below only matched
    # a bare "N tools", so "N chemical safety tools" slipped through even where it
    # was listed. Widened both.
    ".claude-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
    # 2026-07-31: fourth miss of the same shape, found while reviewing the
    # streamable-transport change. The root README was never in this list at all,
    # so THREE stale "22"s sat there (heading, architecture diagram, /health
    # example) while this test stayed green. Two causes: the short surface list,
    # and TOOL_COUNT_PATTERNS below only matched a number BEFORE "tools" and only
    # in lowercase — so "Tools (22)" and "22 Safety Tools" were invisible even
    # once listed.
    "README.md",
]

# Deliberately NOT a surface: server_remote.py. Its /health used to hardcode the
# count (and drifted to 22); it now reads the live registry, so there is no
# literal number left to guard. Adding it here would trip the "no 'N tools'
# claim found" assert below. Dynamic beats guarded — don't add it back.

# A tool count shows up in copy in three shapes. Match all of them,
# case-insensitively; a number-before-"tools" pattern alone is what let the
# 2026-07-31 miss through.
TOOL_COUNT_PATTERNS = [
    # "23 tools", "23 MCP tools", "23 chemical safety tools", "**23** tools"
    r"\*{0,2}(\d+)\*{0,2}\s+(?:[\w-]+\s+){0,3}tools\b",
    # "## Tools (23)"
    r"\btools\s*\((\d+)\)",
    # '{"status":"ok","tools":23}'
    r'"tools"\s*:\s*(\d+)',
]


def test_advertised_tool_count_matches_registered():
    """Every "N tools" claim must equal the number of tools actually registered.

    scripts/release.sh stamps the VERSION but NOT the tool count, and nothing else
    guarded it — so adding the 23rd tool (search_msds_online, SE-19) silently left
    five user-facing surfaces advertising "22 tools", including the description
    already published to the MCP registry. This test makes that drift fail CI.
    """
    import asyncio
    import re

    actual = len(asyncio.run(server.mcp.list_tools()))
    for rel in TOOL_COUNT_SURFACES:
        with open(os.path.join(ROOT, rel)) as f:
            text = f.read()
        claims = [
            int(n)
            for pattern in TOOL_COUNT_PATTERNS
            for n in re.findall(pattern, text, re.IGNORECASE)
        ]
        assert claims, f"{rel}: no 'N tools' claim found — update TOOL_COUNT_SURFACES"
        for n in claims:
            assert n == actual, (
                f"{rel} advertises {n} tools but {actual} are registered — "
                f"update the copy when adding/removing a tool"
            )
