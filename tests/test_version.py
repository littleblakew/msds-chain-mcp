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
