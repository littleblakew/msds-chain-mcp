"""Skills over MCP (`io.modelcontextprotocol/skills`, SEP-2640): serve `skills/` from the server.

Why it exists: the skills in `skills/` used to reach only people who installed the plugin.
A user who only connected the server (a claude.ai connector, a Claude Code `mcp add`) never
saw them. Serving them from the server gives every connected client the same workflows.

What a host gets:
- `server/discover` advertises the extension (settings `{}`: no `resources/directory/read`).
- `skills/list` and `skills/get` return each skill as `{uri, frontmatter, resources}`, where
  `resources` names every file of the skill once (SKILL.md included) with its byte size and
  `sha256:` digest; the host checks each read against both.
- Each file is readable with `resources/read` at `skill://<name>/<path>`.

The files are read once at import, so what is listed, what is digested and what is read are
the same bytes for the life of the process. A skill directory whose name differs from its
frontmatter `name`, or whose SKILL.md lacks `name` / `description`, fails the import rather
than being served half-valid (the spec makes both MUSTs).
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from mcp.server.extension import Extension, MethodBinding, ResourceBinding
from mcp.server.mcpserver.resources import TextResource
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, RequestParams

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
IDENTIFIER = "io.modelcontextprotocol/skills"
# Same reasoning as `_LIST_CACHE` in app.py: constant per image, a new revision may change it.
# `public` is safe here (unlike tools/list): skill files are the same public text for every caller.
_TTL_MS = 300_000
_CACHE_SCOPE = "public"


class SkillsListParams(RequestParams):
    cursor: str | None = None


class SkillsGetParams(RequestParams):
    uri: str


@dataclass(frozen=True)
class _File:
    uri: str
    text: str
    mime_type: str
    size: int
    digest: str


@dataclass(frozen=True)
class _Skill:
    name: str
    frontmatter: dict[str, Any]
    files: tuple[_File, ...]

    @property
    def uri(self) -> str:
        return f"skill://{self.name}/SKILL.md"

    def entry(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "frontmatter": self.frontmatter,
            "resources": [{"uri": f.uri, "digest": f.digest, "size": f.size} for f in self.files],
        }


_FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def _frontmatter(text: str, where: Path) -> dict[str, Any]:
    m = _FRONTMATTER.match(text)
    if m is None:
        raise ValueError(f"{where}: SKILL.md must start with a closed YAML frontmatter block")
    data = yaml.safe_load(m.group(1))
    if not isinstance(data, dict) or not data.get("name") or not data.get("description"):
        raise ValueError(f"{where}: frontmatter needs `name` and `description`")
    return data


def _mime(path: Path) -> str:
    if path.suffix == ".md":
        return "text/markdown"
    return mimetypes.guess_type(path.name)[0] or "text/plain"


def _ignored(path: Path, skill_dir: Path) -> bool:
    """Editor and interpreter litter (`.DS_Store`, `__pycache__/`) is not part of a skill."""
    return any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(skill_dir).parts)


def load_skills(root: Path = SKILLS_DIR) -> tuple[_Skill, ...]:
    skills = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        skill_dir = skill_md.parent
        front = _frontmatter(skill_md.read_text(encoding="utf-8"), skill_md)
        if front["name"] != skill_dir.name:
            raise ValueError(f"{skill_md}: frontmatter name {front['name']!r} != directory {skill_dir.name!r}")
        files = []
        for path in sorted(p for p in skill_dir.rglob("*") if p.is_file() and not _ignored(p, skill_dir)):
            raw = path.read_bytes()
            rel = path.relative_to(skill_dir).as_posix()
            files.append(_File(
                uri=f"skill://{skill_dir.name}/{rel}",
                text=raw.decode("utf-8"),
                mime_type=_mime(path),
                size=len(raw),
                digest="sha256:" + hashlib.sha256(raw).hexdigest(),
            ))
        skills.append(_Skill(name=skill_dir.name, frontmatter=front, files=tuple(files)))
    return tuple(skills)


class Skills(Extension):
    identifier = IDENTIFIER

    def __init__(self, skills: tuple[_Skill, ...] | None = None) -> None:
        self._skills = load_skills() if skills is None else skills
        self._by_uri = {s.uri: s for s in self._skills}

    def settings(self) -> dict[str, Any]:
        return {}

    def resources(self) -> list[ResourceBinding]:
        return [
            ResourceBinding(TextResource(
                uri=f.uri, name=f.uri.removeprefix("skill://"), mime_type=f.mime_type, text=f.text,
                description=f"File of the `{s.name}` skill (Skills over MCP).",
            ))
            for s in self._skills for f in s.files
        ]

    def methods(self) -> list[MethodBinding]:
        return [
            MethodBinding("skills/list", SkillsListParams, self._list),
            MethodBinding("skills/get", SkillsGetParams, self._get),
        ]

    async def _list(self, ctx: Any, params: SkillsListParams) -> dict[str, Any]:
        # One page: we serve a handful of skills, far under any page size, so no cursor is issued
        # and any cursor sent back is answered with the same (only) page.
        return {"skills": [s.entry() for s in self._skills], "ttlMs": _TTL_MS, "cacheScope": _CACHE_SCOPE}

    async def _get(self, ctx: Any, params: SkillsGetParams) -> dict[str, Any]:
        skill = self._by_uri.get(params.uri)
        if skill is None:
            raise MCPError(INVALID_PARAMS, f"Unknown skill: {params.uri}")
        return {"skill": skill.entry(), "ttlMs": _TTL_MS, "cacheScope": _CACHE_SCOPE}
