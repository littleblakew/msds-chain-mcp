#!/usr/bin/env bash
#
# release.sh — single-source version fan-out for msds-chain-mcp.
#
# THE PROBLEM THIS SOLVES: the server version is displayed in many places
# (MCP `initialize` handshake, the official MCP registry that ChatGPT/claude.ai
# read, npm, Claude Code / Codex plugin manifests) and NONE of them auto-sync
# with git. Each was published by hand, so the numbers drifted (registry 1.3.0,
# npm 1.2.0, plugins 1.0.0, live serverInfo = the SDK version). ChatGPT showed a
# stale 1.0.0 because it ingested our oldest registry entry.
#
# THE FIX: the repo-root VERSION file is the ONLY number a human edits. This
# script stamps it into every manifest + server.py, verifies, and (with
# --publish) pushes the irreversible external releases.
#
# USAGE:
#   scripts/release.sh              # sync all files from VERSION + verify (safe, local)
#   scripts/release.sh --publish    # local fallback: git tag + npm publish + mcp registry publish
#
# TYPICAL RELEASE (CI-driven — preferred):
#   1. edit VERSION            (e.g. 1.4.0 -> 1.5.0)
#   2. scripts/release.sh      # stamps everything, runs the guard test
#   3. review `git diff`, commit, push main   # push auto-deploys the core (serverInfo)
#   4. git tag vX.Y.Z && git push origin vX.Y.Z
#      -> .github/workflows/release.yml publishes npm + MCP registry automatically
#         (npm via NPM_TOKEN secret; registry via GitHub OIDC — no manual login)
#
# The `--publish` path below is a LOCAL FALLBACK for when CI is unavailable. It
# uses the committed macOS mcp-publisher binary and requires you to have run
# `npm login` and `./mcp-publisher login github` first.
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

VERSION="$(tr -d '[:space:]' < VERSION)"
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "❌ VERSION file must contain a bare semver (got: '$VERSION')" >&2
  exit 1
fi
echo "▶ Target version: $VERSION"

# --- 1. Stamp every surface (targeted, byte-minimal replacements) -----------
# server.py: the __version__ literal that becomes serverInfo.version at runtime.
perl -0pi -e 's/^__version__ = "[^"]*"/__version__ = "'"$VERSION"'"/m' server.py

# JSON manifests: every "version" key in each file is the release version.
# (verified: no manifest carries an unrelated "version" field to protect.)
JSON_MANIFESTS=(
  npm-package/package.json
  npm-package/server.json
  plugin.json
  .claude-plugin/plugin.json
  .claude-plugin/marketplace.json
  .codex-plugin/plugin.json
  .agents/plugins/marketplace.json
)
for f in "${JSON_MANIFESTS[@]}"; do
  perl -pi -e 's/"version": "[0-9]+\.[0-9]+\.[0-9]+"/"version": "'"$VERSION"'"/g' "$f"
  echo "  stamped $f"
done
echo "✅ All surfaces stamped to $VERSION"

# --- 1b. Stamp DERIVED metadata (tool count + primary endpoint + transport) --
# VERSION is not the only value that drifts. The tool count ("N tools") and the
# primary endpoint URL + transport type also surface across many files and used
# to be hand-maintained (the count was policed by a "surface list + regex" guard
# that missed drift five times; the endpoint/transport had no source of truth or
# test at all). Derive them the same way — the count from the LIVE registry,
# endpoint/transport from release_metadata.py — and stamp them here, so the guard
# only has to check wiring, never re-audit copy. See scripts/stamp_derived.py.
echo "▶ Stamping derived metadata (tool count + endpoint + transport)..."
python scripts/stamp_derived.py
echo "✅ Derived metadata stamped"

# --- 2. Verify: guard test proves server.py == VERSION and serverInfo carries it
echo "▶ Running version guard test..."
python -m pytest tests/test_version.py -q

if [[ "${1:-}" != "--publish" ]]; then
  echo ""
  echo "✅ Sync complete. Review 'git diff', commit, and push main to deploy the core."
  echo "   Then run: scripts/release.sh --publish   (npm + MCP registry — what ChatGPT reads)"
  exit 0
fi

# --- 3. Publish (irreversible external actions; only with --publish) ---------
echo ""
echo "⚠️  --publish will run IRREVERSIBLE external releases for v$VERSION:"
echo "     • git tag v$VERSION"
echo "     • npm publish (registry.npmjs.org/msds-chain-mcp)"
echo "     • mcp-publisher (registry.modelcontextprotocol.io — feeds ChatGPT/claude.ai)"
read -r -p "Proceed? [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "Aborted."; exit 1; }

# 3a. git tag (annotated) — human-readable release marker on the repo.
git tag -a "v$VERSION" -m "Release v$VERSION" 2>/dev/null \
  && echo "  tagged v$VERSION (push with: git push origin v$VERSION)" \
  || echo "  tag v$VERSION already exists — skipping"

# 3b. npm publish — the npm package (msds-chain-mcp) referenced by server.json.
( cd npm-package && npm publish --access public )
echo "  ✅ npm published $VERSION"

# 3c. MCP official registry — the entry ChatGPT's apps directory + claude.ai read.
#     Requires prior `mcp-publisher login github` (interactive, one-off).
if [[ -x npm-package/mcp-publisher ]]; then
  ( cd npm-package && ./mcp-publisher publish )
  echo "  ✅ MCP registry published $VERSION"
else
  echo "  ⚠️  npm-package/mcp-publisher not found — run it manually to push the registry entry."
fi

echo ""
echo "🎉 Published v$VERSION. Note: ChatGPT's directory re-ingests on OpenAI's own"
echo "   cadence (not instant) — it may still show the old version for a while, and"
echo "   may require re-submitting the connector in the OpenAI developer console."
