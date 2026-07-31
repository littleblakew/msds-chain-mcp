"""Single source of truth for release-time DERIVED metadata — the tool count,
the primary endpoint URL, and the transport type — that scripts/release.sh
stamps into every client-facing surface, alongside the VERSION it already stamps.

Why this module exists (CI-237)
-------------------------------
release.sh stamps VERSION into 8 files but historically left two other values
hand-maintained:

  * the tool count ("N tools"), scattered as ~12 literals across 10 files, and
  * the primary endpoint URL + transport type in the machine-readable manifests.

The tool count was policed after the fact by a "surface list + regex" guard in
tests/test_version.py, which missed drift FIVE times (npm package.json, two
marketplace.json files, the root README, and skills/.../SKILL.md — the last
stuck at 20). The file's own comment said "the guard is only worth as much as
its surface list" and it kept losing ground. The endpoint/transport had NO
source of truth and NO test at all, so the sse->streamable migration was six
hand-edits with no safety net.

The fix: release.sh now DERIVES these (the count from the live registry, the
rest from this module) and stamps them the same way it stamps VERSION. The guard
downgrades from "audit the copy" to "check the wiring". The writer
(scripts/stamp_derived.py) and the verifier (tests/test_version.py) both import
THIS module, so their surface lists can no longer drift apart.
"""
import re

# --- Tool count ------------------------------------------------------------
# Files whose user-facing copy states the tool count ("N tools"). These are what
# ChatGPT / claude.ai / npm / the Claude Code + Codex plugin listings + the
# bundled skill show. release.sh stamps the live registry count into each; the
# guard only verifies the wiring held (that a literal exists to stamp).
#
# server_remote.py is deliberately NOT here: its /health reads the live registry
# (len(await mcp.list_tools())), so there is no literal to stamp or guard.
TOOL_COUNT_SURFACES = [
    "npm-package/package.json",
    "npm-package/server.json",
    "npm-package/README.md",
    "plugin.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
    "README.md",
    "skills/msds-safety-check/SKILL.md",
]

# A tool count appears in copy in these shapes; each captures the number in
# group(1). Matched case-insensitively. The stamper rewrites exactly what these
# match, and the guard reads exactly what these match, so the two cannot diverge.
# A "N lists" / "N regions" number never matches (no "tools" adjacency), so the
# coincidental "(23 lists, 10 regions)" in descriptions is left untouched.
TOOL_COUNT_PATTERNS = [
    # "23 tools", "23 MCP tools", "23 chemical safety tools", "**23** tools"
    r"\*{0,2}(\d+)\*{0,2}\s+(?:[\w-]+\s+){0,3}tools\b",
    # "## Tools (23)" and '{"status":"ok","tools":23}' — same shape, either delimiter
    r"\btools\s*[(:]\s*(\d+)",
]


# --- Primary endpoint + transport -----------------------------------------
# The recommended (streamable) connect endpoint. Changing it here and running
# scripts/release.sh restamps every advertised `.../mcp` URL below. The legacy
# `/sse` fallback URLs are intentionally NOT stamped — the hosted gateway keeps
# dual transport live, so those references stay put on purpose.
PRIMARY_ENDPOINT = "https://mcp.lagentbot.com/mcp"

# Structured manifests carrying a machine-readable MCP-server block ({url,type}).
# The transport TYPE is schema-specific and is NOT drift: the MCP Registry's
# server.json uses "streamable-http"; the plugin / npm client configs use "http".
# Both name the same streamable transport in their own schema's vocabulary, so
# this is a mapping, never a single string to unify.
TRANSPORT_BY_SURFACE = {
    "npm-package/server.json": "streamable-http",  # registry.modelcontextprotocol.io
    "npm-package/package.json": "http",            # npm client-config example
    ".claude-plugin/plugin.json": "http",          # Claude Code plugin
    ".codex-plugin/plugin.json": "http",           # Codex plugin
}

# Every surface advertising the primary endpoint as a full `https://<host>/mcp`
# URL (structured manifests + prose docs). Stamping rewrites the host of each;
# `/sse` URLs never match this pattern and are left alone.
ENDPOINT_URL_SURFACES = [
    "npm-package/server.json",
    "npm-package/package.json",
    "npm-package/README.md",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "README.md",
    "skills/msds-safety-check/rules/setup-guide.md",
]

# Matches a full primary-endpoint URL (the `/mcp` streamable path). Never matches
# `/sse`, the product homepage (`https://msdschain.lagentbot.com`), or the git
# repo URL — none of those end in `/mcp`. The leading lookahead also excludes
# deliberate self-hosting PLACEHOLDER hosts (`your-server.example.com`, localhost)
# so the "point at YOUR own server" docs are never rewritten to our host.
ENDPOINT_URL_RE = re.compile(
    r"""https://
        (?![^\s/"'`)]*(?:example\.|your-|localhost|127\.0\.0\.1))  # skip placeholders
        [^\s"'`)]+/mcp\b""",
    re.VERBOSE,
)

# Matches the transport `type` that sits immediately before an `/mcp` url inside
# a manifest's server block. Anchored to the `/mcp` url so an unrelated "type"
# (e.g. package.json's "type": "git", followed by a .git url) is never touched.
# group(1) = up to the value's opening quote; group(2) = closing quote onward
# through the url line. Replacement is group(1) + <transport> + group(2).
TRANSPORT_TYPE_RE = re.compile(
    r'("type"\s*:\s*")(?:streamable-http|http|sse)'
    r'("\s*,\s*\n\s*"url"\s*:\s*"https://[^"]*/mcp")'
)
