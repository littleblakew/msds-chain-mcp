"""The image ships only the files the Dockerfile COPY line names.

A module `server_remote.py` imports (or a data file it reads) that is missing from that
line builds a perfectly green image which fails at import, or worse, on a code path only
one deployment mode reaches (`edge.py` runs only with `MSDS_MCP_EDGE=1`).

Mutation: drop `edge.py` from the Dockerfile COPY line → this test fails.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _copied() -> set[str]:
    names = set()
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        m = re.match(r"\s*COPY\s+(.+)\s+\./\s*$", line)
        if m:
            names.update(m.group(1).split())
    return names


def _local_imports(path: Path, seen: set[str]) -> None:
    for node in ast.walk(ast.parse(path.read_text())):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods = [node.module.split(".")[0]]
        for m in mods:
            f = ROOT / f"{m}.py"
            if f.exists() and f.name not in seen:
                seen.add(f.name)
                _local_imports(f, seen)


def test_every_local_module_and_data_file_is_copied():
    needed = {"server_remote.py"}
    _local_imports(ROOT / "server_remote.py", needed)
    needed.add("hosted_instructions.txt")  # read by server_remote in edge mode
    missing = sorted(needed - _copied())
    assert not missing, f"Dockerfile COPY is missing {missing}"
