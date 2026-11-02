#!/usr/bin/env python3
"""导出 MCP 注册面的完整快照：工具（名字 / 描述 / 入参与出参 schema / annotations / meta）、
prompts（含用样例入参渲染出的正文）、resources（含正文）、server instructions、版本。

跑法（在仓根）：`python3 scripts/dump_registry_snapshot.py [输出路径]`，默认写
`tests/contract/registry_snapshot.json`。`tests/contract/test_registry_snapshot.py` 把当前注册面与
这份文件逐字比对：重构（拆文件、搬函数）后快照必须不变；有意改了工具面才重新生成。
"""
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "tests" / "contract" / "registry_snapshot.json"

# 每个 prompt 用空入参和一组样例入参各渲染一次，正文也进快照。
_PROMPT_SAMPLES = {
    "lab_protocol_audit": {"protocol_text": "Mix A with B", "chemicals": "acetone, bleach"},
    "ehs_compliance_review": {"chemicals": "acetone", "regions": "EU,US,CN"},
    "incident_response": {"chemical": "acetone", "scenario": "fire"},
}


def _dump(obj):
    return obj.model_dump(mode="json", by_alias=True) if hasattr(obj, "model_dump") else obj


async def snapshot() -> dict:
    import server

    mcp = server.mcp
    tools = [_dump(t) for t in await mcp.list_tools()]  # 保持注册顺序：它就是 tools/list 的顺序
    prompts = sorted((_dump(p) for p in await mcp.list_prompts()), key=lambda p: p["name"])
    rendered = {}
    for p in prompts:
        for label, args in (("default", {}), ("sample", _PROMPT_SAMPLES.get(p["name"], {}))):
            res = await mcp.get_prompt(p["name"], args)
            rendered[f"{p['name']}:{label}"] = _dump(res)
    resources = sorted((_dump(r) for r in await mcp.list_resources()), key=lambda r: r["uri"])
    bodies = {}
    for r in resources:
        contents = await mcp.read_resource(r["uri"])
        bodies[r["uri"]] = [_dump(c) if hasattr(c, "model_dump") else str(c) for c in contents]
    return {
        "name": mcp.name,
        "instructions": mcp._lowlevel_server.instructions,
        "cache_hints": {k: _dump(v) for k, v in sorted((mcp._lowlevel_server.cache_hints or {}).items())}
        if hasattr(mcp._lowlevel_server, "cache_hints") else None,
        "tools": tools,
        "prompts": prompts,
        "prompt_renderings": rendered,
        "resources": resources,
        "resource_bodies": bodies,
    }


def main() -> None:
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    snap = asyncio.run(snapshot())
    out.write_text(json.dumps(snap, indent=1, ensure_ascii=False, sort_keys=True, default=str) + "\n",
                   encoding="utf-8")
    print(f"{out}: {len(snap['tools'])} tools, {len(snap['prompts'])} prompts, "
          f"{len(snap['resources'])} resources")


if __name__ == "__main__":
    main()
