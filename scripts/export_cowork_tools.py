"""Export the Microsoft Cowork tool-description file from the live tool registry.

    python scripts/export_cowork_tools.py > <pkg>/tools/msds-chain-tools.json

Why this is a script and not a paragraph in a README: Cowork requires this file inside
the submitted app package and has **no runtime discovery** — whatever it says is what
Copilot believes about our tools until the next package submission. So a hand-written or
stale file does not fail loudly; it just makes Copilot call the wrong signatures forever.
(Measured 2026-08-19 against the 2026-08-14 snapshot: `get_audit_report` had since become
callable with no arguments — the exact path meant for "give me a report" — but the packaged
file still marked `session_id` required, so that path was unreachable.)

The output must be in MCP **wire** shape, because the consumer is Microsoft, not this SDK:
`inputSchema` (not `input_schema`) and `readOnlyHint` (not `read_only_hint`). mcp 2.x names
the python attributes snake_case, so both spellings are one forgotten argument apart —
`tests/test_ci517_cowork_tools_export.py` pins them.
"""
import asyncio
import json
import sys

sys.path.insert(0, ".")

from server import mcp  # noqa: E402


def dump_tool(t) -> dict:
    d = {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
    ann = getattr(t, "annotations", None)
    if ann is not None:
        # by_alias=True is load-bearing — see the module docstring.
        d["annotations"] = (ann.model_dump(by_alias=True, exclude_none=True)
                            if hasattr(ann, "model_dump") else ann)
    return d


async def build() -> dict:
    return {"tools": [dump_tool(t) for t in await mcp.list_tools()]}


def main() -> None:
    print(json.dumps(asyncio.run(build()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
