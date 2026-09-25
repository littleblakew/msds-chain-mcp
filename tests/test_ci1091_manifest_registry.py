"""CI-1091: the manifest list must discover its own members, and the Gemini
extension manifest must stay in the shape the gallery accepts.

WHY A DISCOVERY GUARD. rm.JSON_MANIFESTS is a hand-written list that the stamper
(scripts/release.sh) and the version guard (tests/test_version.py) both read. A
list like that only fails in one direction: a manifest that IS listed gets
checked, a manifest nobody remembered to list is simply never stamped and never
verified — it sits at whatever version it was born with and nothing goes red.
This repo has shipped eight release-versioned manifests to five different
distribution channels, so "remember to add a line" is not a mechanism.

So: discover the candidates from git instead of trusting the list. Every tracked
*.json carrying a semver `"version"` must be either registered for stamping or
explicitly exempted here with a reason.

MUTATION RECORD (what makes each test red — the direction matters):
  * test_every_versioned_manifest_is_registered — add a new tracked JSON file
    with a `"version": "1.2.3"` field and register it nowhere. Note TRACKED: the
    discovery reads `git ls-files`, so a manifest still untracked is invisible
    to it (mutating against an unstaged file passes and looks like the guard is
    broken — it is not). That scope is deliberate: an unpublished file cannot
    drift in anyone's client. Verified by
    pointing the discovery at a temp tree in
    test_discovery_actually_finds_an_unregistered_manifest below, so the guard
    is not a green no-op when the repo happens to be tidy.
  * test_gemini_manifest_shape — change httpUrl to another host, or add a
    `trust` key to the server block.
"""
import json
import os
import re
import subprocess

import pytest

import release_metadata as rm

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

SEMVER_VERSION_RE = re.compile(r'"version"\s*:\s*"\d+\.\d+\.\d+"')

# Tracked JSON files that legitimately carry a semver but are NOT stamped from
# the repo-root VERSION. Each needs a reason: an unexplained entry here is
# indistinguishable from someone silencing the guard.
EXEMPT = {
    # Independently versioned sub-package published on its own cadence (0.1.0),
    # deliberately decoupled from the server's release version.
    "plugins/dsh/package.json": "separately versioned DeepSeek Harness plugin",
}


def _tracked_json_files():
    out = subprocess.run(
        ["git", "ls-files", "*.json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _versioned(paths, root=ROOT):
    found = []
    for rel in paths:
        with open(os.path.join(root, rel)) as f:
            if SEMVER_VERSION_RE.search(f.read()):
                found.append(rel)
    return found


def test_every_versioned_manifest_is_registered():
    unregistered = [
        rel for rel in _versioned(_tracked_json_files())
        if rel not in rm.JSON_MANIFESTS and rel not in EXEMPT
    ]
    assert not unregistered, (
        f"these tracked JSON manifests carry a semver 'version' but are neither "
        f"in release_metadata.JSON_MANIFESTS (so release.sh never stamps them and "
        f"they will silently drift) nor in this test's EXEMPT map: {unregistered}"
    )


def test_discovery_actually_finds_an_unregistered_manifest(tmp_path):
    """Positive control: prove the discovery above can see a new manifest.

    Without this, the guard reads as working whenever the repo is already tidy —
    which is exactly when a broken discovery would go unnoticed.
    """
    (tmp_path / "newthing.json").write_text('{"version": "1.2.3"}')
    (tmp_path / "notamanifest.json").write_text('{"schema": "x"}')
    found = _versioned(["newthing.json", "notamanifest.json"], root=str(tmp_path))
    assert found == ["newthing.json"]


def test_registered_manifests_all_exist():
    """A rename that misses rm.JSON_MANIFESTS leaves a dead path; release.sh
    would die mid-stamp, but the guard should say so first and by name."""
    missing = [
        rel for rel in rm.JSON_MANIFESTS
        if not os.path.exists(os.path.join(ROOT, rel))
    ]
    assert not missing, f"registered but absent: {missing}"


# --- Gemini CLI extension manifest ---------------------------------------
# https://geminicli.com/docs/extensions/releasing/ — gallery listing needs a
# public repo, the `gemini-cli-extension` GitHub topic, and this file at the
# absolute repo root. Only the last one is checkable from here; the topic is
# repo metadata and lives outside the tree (see tickets/CI-1091.md).

GEMINI_MANIFEST = "gemini-extension.json"


@pytest.fixture
def gemini():
    with open(os.path.join(ROOT, GEMINI_MANIFEST)) as f:
        return json.load(f)


def test_gemini_manifest_at_repo_root():
    assert os.path.exists(os.path.join(ROOT, GEMINI_MANIFEST)), (
        "gemini-extension.json must sit at the ABSOLUTE repo root or the gallery "
        "crawler will not index it"
    )


def test_gemini_manifest_shape(gemini):
    assert re.fullmatch(r"[a-z0-9-]+", gemini["name"]), (
        f"extension name {gemini['name']!r} must be lowercase/digits/dashes only "
        f"(no underscores or spaces) per the extension reference"
    )
    assert gemini.get("description"), "the gallery lists this description"

    servers = gemini["mcpServers"]
    assert servers, "manifest declares no MCP server"
    for name, block in servers.items():
        assert block.get("httpUrl") == rm.PRIMARY_ENDPOINT, (
            f"{name}: httpUrl {block.get('httpUrl')!r} != PRIMARY_ENDPOINT "
            f"{rm.PRIMARY_ENDPOINT!r} — run scripts/release.sh to re-stamp"
        )
        # "all MCP server configuration options are supported except for `trust`"
        # — an extension that ships `trust` is rejected, and we would find out
        # from a user who could not install it.
        assert "trust" not in block, (
            f"{name}: `trust` is not allowed in an extension manifest"
        )
