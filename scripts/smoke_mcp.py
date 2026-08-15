#!/usr/bin/env python3
"""Post-deploy tool-level smoke for the public MCP core (through the gateway).

Unlike the /health check (liveness only), this connects as a real MCP client
over the public streamable endpoint, lists the tools, and calls a couple of
fast, deterministic, NON-mutating tools asserting non-empty output — so a deploy
that leaves the 22 tools broken (auth, resolver, canonical data) fails loudly
instead of going green on a bare /health.

Config via env:
  MCP_SMOKE_URL   default https://mcp.lagentbot.com/mcp  (streamable, via gateway)
  MCP_SMOKE_KEY   Bearer per-user prod API key (CI-scoped)  [required]

Exit 0 on pass, non-zero on any failure. No writes (no create_audit_session/upload).
"""
import asyncio
import os
import sys

# 🔴 直接执行脚本时 sys.path[0] 是 `scripts/`，而 `server.py` 在仓根 ⇒ `import server`
# 会 ModuleNotFoundError。pytest 从仓根跑，所以单测**永远发现不了**这个——2026-08-15
# 就是这样把生产部署的 gate 弄红的（同族：测试替生产布置好了前提）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("MCP_SMOKE_URL", "https://mcp.lagentbot.com/mcp")
KEY = os.environ.get("MCP_SMOKE_KEY", "")


async def _expected_tool_count() -> int:
    """本次提交的 registry 里到底注册了几个工具——**精确值**，不是下限。

    🔴 CI-245：这里原本是 `MCP_SMOKE_MIN_TOOLS` 默认 **20**，而实际有 **23** ⇒
    **线上掉 3 个工具，gate 依然全绿**。宽松的下限＝没测过（同族：memory
    「CI runner 环境差异会制造假绿」里那条「宽松上界=没测过」）。

    改成从 live registry 取精确值：部署的镜像与本次提交同源，所以「线上 tools/list
    的数量 == 本地 registry 的数量」是一个真判据。取不到就**硬失败**，不回退到
    某个猜的数字——一个猜的下限正是我们刚修掉的东西。

    🔴 **必须是 async**：它在 `_run()` 里被调用，而那时事件循环已经在跑——初版写成
    `asyncio.run(...)` 直接抛 `cannot be called from a running event loop`，把生产
    部署的 gate 弄红了（2026-08-15）。而单测在循环**外**调它，`asyncio.run` 正常，
    于是测试全绿——测试又一次替生产布置了它没有的前提。
    """
    import server  # 本仓自己的 registry
    return len(await server.mcp.list_tools())


def _text(result) -> str:
    """Flatten a CallToolResult's content blocks into text."""
    parts = []
    for block in (result.content or []):
        t = getattr(block, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)


async def _run() -> None:
    headers = {"Authorization": f"Bearer {KEY}"}
    async with streamablehttp_client(URL, headers=headers, timeout=30) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            expected = await _expected_tool_count()
            print(f"tools registered: {len(names)} (registry expects {expected})")
            assert len(names) == expected, (
                f"线上暴露 {len(names)} 个工具，本次提交的 registry 有 {expected} 个——"
                f"数量不等说明有工具没上线/多上线了。线上: {names}"
            )

            # 1) get_sds_section — fast, no-LLM, deterministic; exercises resolver +
            # shared.canonical_sections. acetone §4 (first aid) is a stable canary.
            r1 = await session.call_tool("get_sds_section", {"chemical": "acetone", "section": 4})
            t1 = _text(r1)
            assert not r1.isError, f"get_sds_section errored: {t1[:300]}"
            assert t1.strip() and "No data available" not in t1, \
                f"get_sds_section(acetone,4) returned no content: {t1[:300]}"
            print("✅ get_sds_section(acetone, 4) — non-empty first-aid content")

            # 2) search_chemical_database — fast lookup; confirms DB path is live.
            r2 = await session.call_tool("search_chemical_database", {"query": "acetone"})
            t2 = _text(r2)
            assert not r2.isError and t2.strip(), f"search_chemical_database returned nothing: {t2[:300]}"
            print("✅ search_chemical_database(acetone) — non-empty")

    print("MCP tool smoke PASSED")


def main() -> int:
    if not KEY:
        print("MCP_SMOKE_KEY not set — nothing to do (caller should skip).", file=sys.stderr)
        return 0
    try:
        asyncio.run(_run())
        return 0
    except Exception as e:  # noqa: BLE001 — smoke: any failure must fail the deploy
        # 🔴 anyio 的 TaskGroup 把真实异常包进 ExceptionGroup，只打最外层等于
        # **把失败原因藏起来**——CI 日志里只有 "1 sub-exception"，查不出所以然。
        def _flatten(exc, depth=0):
            yield f"{'  ' * depth}{type(exc).__name__}: {exc}"
            for sub in getattr(exc, "exceptions", ()) or ():
                yield from _flatten(sub, depth + 1)
        for line in _flatten(e):
            print(f"::error::MCP tool smoke FAILED: {line}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
