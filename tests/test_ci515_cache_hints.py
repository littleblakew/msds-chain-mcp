"""CI-515：list 类结果必须带可用的 `ttlMs`/`cacheScope`（2026-07-28 spec / SEP-2549）。

判据打在**线上真会发出去的那份 JSON** 上，不是打在 `_LIST_CACHE` 这个常量上——常量对了但
没接进 `MCPServer(cache_hints=...)`，或者接了但方法名写错（SDK 只认 `CACHEABLE_METHODS` 里的
字面量），结果照样是 SDK 默认的 `ttlMs: 0`＝「立刻过期」，而**没有任何一步会报错**。
同族：memory「修了，但没到达真正的消费者」。

⚠️ 这些字段只出现在 **modern（2026-07-28）** 那条腿上，请求要带 `MCP-Protocol-Version`
头 + `params._meta` 信封；走 `initialize` 的旧腿协商到 2025-11-25，压根没有这些字段。
所以下面每个请求都是完整的 modern 形状——照抄别删。
"""
import json

import pytest
import server

_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _call(live_client, method: str) -> dict:
    r = live_client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": method,
        },
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                            "params": {"_meta": _META}}),
    )
    assert r.status_code == 200, f"{method} 返回 {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert "result" in body, f"{method} 没有 result: {body}"
    return body["result"]


@pytest.mark.parametrize("method", ["tools/list", "server/discover"])
def test_cacheable_result_carries_a_usable_ttl(live_client, method):
    """`ttlMs: 0` 是 SDK 默认值，意思是「立刻过期」= 等于没给提示。"""
    result = _call(live_client, method)
    assert result.get("ttlMs", 0) > 0, (
        f"{method} 的 ttlMs={result.get('ttlMs')} ⇒ 客户端每次都要重列。"
        f"检查 MCPServer(cache_hints=...) 里这个方法名是否拼对（SDK 只认 CACHEABLE_METHODS 的字面量）"
    )


@pytest.mark.parametrize("method", ["tools/list", "server/discover"])
def test_cacheable_result_is_scoped_private(live_client, method):
    """🔴 改成 `public` 之前先读 server.py 里 `_LIST_CACHE` 上面那段。

    工具表**今天**确实与调用方无关（无条件注册 + 网关不过滤），但我们随时可能按 plan
    收起某些工具；那一刻 `public` 会让共享缓存把一份工具表跨授权上下文发出去。
    要放开，先加一条「工具表不随调用方变化」的守卫，再改这里。
    """
    assert _call(live_client, method).get("cacheScope") == "private"


def test_hints_are_only_configured_for_methods_we_actually_serve():
    """给没有 handler 的方法配 hint ＝ 写一份永远不执行的配置，日后误导人。

    🔴 配置**不在** `mcp.settings` 上（那里没有 `cache_hints` 这个字段），在
    `_lowlevel_server.cache_hints`。初版写成 `settings.__dict__.get(...) or {硬编码集合}`，
    于是永远走兜底分支、断言自己跟自己比——恒真。取不到就让它 AttributeError，别兜底。
    """
    from mcp_types.methods import CACHEABLE_METHODS
    configured = set(server.mcp._lowlevel_server.cache_hints or {})
    assert configured, "一个 cache hint 都没配上 —— 上面那两条断言是靠什么绿的？"
    unknown = configured - set(CACHEABLE_METHODS)
    assert not unknown, f"这些方法不在 SDK 的 CACHEABLE_METHODS 里，hint 永远不会生效：{unknown}"


def test_modern_leg_needs_no_session(live_client):
    """无状态是这次协议对齐的实际收益：没有 initialize、没有 Mcp-Session-Id 也能调工具。

    它同时是「Hermes 换 revision 就要重启」那个常驻坑消失的**前提**——但只对走 modern 腿的
    客户端成立，Hermes 现在走的是 SSE，坑还在（别据此删 CLAUDE.md 里那条）。
    """
    r = live_client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
        },
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                            "params": {"_meta": _META}}),
    )
    assert r.status_code == 200
    assert "mcp-session-id" not in {k.lower() for k in r.headers}, (
        "modern 腿不该再发 Mcp-Session-Id —— 协议层 session 已被 2026-07-28 spec 移除"
    )
