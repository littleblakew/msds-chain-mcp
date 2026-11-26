"""Skills over MCP (`io.modelcontextprotocol/skills`): what a host sees when it asks the server.

Checks the wire shape a host relies on, against the files on disk:
- the extension is advertised, and `resources` is too (the spec requires both together);
- every file of every skill directory is listed exactly once, SKILL.md included;
- each listed `size` / `sha256:` digest matches the bytes `resources/read` returns, since a
  host rejects a file whose read does not match its listing;
- `skills/get` answers each listed skill and rejects an unknown URI with Invalid params.

Mutation: drop a file from `_Skill.files` (e.g. filter out `rules/`) → the completeness test fails.
Mutation: compute the digest over `text.encode()` of a modified string → the digest test fails.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

import server  # noqa: F401  (registers the full server)
from msds_mcp import skills_ext
from msds_mcp.app import mcp

ROOT = Path(__file__).resolve().parents[2]
_found = [e for e in mcp._extensions if e.identifier == skills_ext.IDENTIFIER]
assert _found, "Skills extension is not registered on the server (`extensions=[Skills()]` in app.py)"
EXT = _found[0]


def _run(coro):
    return asyncio.run(coro)


def test_extension_and_resources_are_advertised():
    assert mcp._lowlevel_server.extensions[skills_ext.IDENTIFIER] == {}
    assert _run(mcp.list_resources()), "resources capability needs at least the registered resources"


def test_every_skill_file_is_listed_once():
    listed = _run(EXT._list(None, skills_ext.SkillsListParams()))
    assert listed["ttlMs"] > 0 and listed["cacheScope"] == "public"
    on_disk = {
        f"skill://{p.parent.name}/SKILL.md" for p in (ROOT / "skills").glob("*/SKILL.md")
    }
    assert {s["uri"] for s in listed["skills"]} == on_disk and on_disk
    for entry in listed["skills"]:
        name = entry["frontmatter"]["name"]
        assert entry["uri"] == f"skill://{name}/SKILL.md"
        assert entry["frontmatter"]["description"]
        files = {
            f"skill://{name}/{p.relative_to(ROOT / 'skills' / name).as_posix()}"
            for p in (ROOT / "skills" / name).rglob("*") if p.is_file()
        }
        uris = [r["uri"] for r in entry["resources"]]
        assert len(uris) == len(set(uris)) and set(uris) == files


def test_listed_digest_and_size_match_what_resources_read_returns():
    listed = _run(EXT._list(None, skills_ext.SkillsListParams()))
    for entry in listed["skills"]:
        for res in entry["resources"]:
            contents = list(_run(mcp.read_resource(res["uri"])))
            assert len(contents) == 1
            raw = contents[0].content
            raw = raw.encode("utf-8") if isinstance(raw, str) else raw
            assert len(raw) == res["size"], res["uri"]
            assert "sha256:" + hashlib.sha256(raw).hexdigest() == res["digest"], res["uri"]
            if res["uri"].endswith(".md"):
                assert contents[0].mime_type == "text/markdown"


def test_get_answers_listed_skills_and_rejects_unknown():
    listed = _run(EXT._list(None, skills_ext.SkillsListParams()))
    for entry in listed["skills"]:
        got = _run(EXT._get(None, skills_ext.SkillsGetParams(uri=entry["uri"])))
        assert got["skill"] == entry
    from mcp.shared.exceptions import MCPError
    with pytest.raises(MCPError) as err:
        _run(EXT._get(None, skills_ext.SkillsGetParams(uri="skill://nope/SKILL.md")))
    assert err.value.error.code == skills_ext.INVALID_PARAMS


def test_skill_dir_must_match_frontmatter_name(tmp_path):
    d = tmp_path / "wrong-dir"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: other\ndescription: x\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter name"):
        skills_ext.load_skills(tmp_path)


def test_frontmatter_accepts_crlf_and_bom_and_skips_litter(tmp_path):
    d = tmp_path / "ok-skill"
    (d / "__pycache__").mkdir(parents=True)
    (d / "SKILL.md").write_bytes("\ufeff---\r\nname: ok-skill\r\ndescription: x\r\n---\r\nbody\r\n".encode("utf-8"))
    (d / ".DS_Store").write_bytes(b"\x00\x87binary")
    (d / "__pycache__" / "x.pyc").write_bytes(b"\x00\xff")
    (skill,) = skills_ext.load_skills(tmp_path)
    assert skill.frontmatter == {"name": "ok-skill", "description": "x"}
    assert [f.uri for f in skill.files] == ["skill://ok-skill/SKILL.md"]

