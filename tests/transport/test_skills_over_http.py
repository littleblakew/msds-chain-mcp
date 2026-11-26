"""Skills over MCP on the transport a host actually uses (streamable HTTP, protocol 2026-07-28).

The in-process tests in `tests/contract/test_skills_extension.py` call the handlers directly;
a host sees only what survives the transport: the extension in `server/discover`, the
`skills/list` envelope (`resultType`), and `resources/read` on a `skill://` URI. A method that
is registered but not routed, or an extension that is applied but not advertised, looks the
same to a host as one that was never written.

Request shape copied from `test_dual_transport.py` (modern headers + `params._meta`).
"""
from __future__ import annotations

import hashlib
import json

_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
_IDENT = "io.modelcontextprotocol/skills"


def _call(client, method, params=None):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2026-07-28",
               "Mcp-Method": method}
    if method == "resources/read":
        # 2026-07-28 要求 `Mcp-Name` 与 body 的 `uri` 逐字一致；缺了是 -32020，不是产品缺陷。
        headers["Mcp-Name"] = params["uri"]
    r = client.post(
        "/mcp",
        headers=headers,
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                            "params": {**(params or {}), "_meta": _META}}),
    )
    assert r.status_code == 200, f"{method} 返回 {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert "result" in body, f"{method} 没有 result: {str(body)[:300]}"
    return body["result"]


def test_discover_advertises_skills_and_resources(live_client):
    caps = _call(live_client, "server/discover")["capabilities"]
    assert _IDENT in (caps.get("extensions") or {}), f"server/discover 没通告 skills 扩展: {caps}"
    assert "resources" in caps, "spec：声明 skills 扩展必须同时声明 resources"


def test_list_get_and_read_round_trip(live_client):
    listed = _call(live_client, "skills/list")
    assert listed.get("resultType") == "complete"
    assert listed["skills"], "skills/list 一个都没列出来"
    for entry in listed["skills"]:
        got = _call(live_client, "skills/get", {"uri": entry["uri"]})
        assert got["skill"]["uri"] == entry["uri"]
        for res in entry["resources"]:
            contents = _call(live_client, "resources/read", {"uri": res["uri"]})["contents"]
            raw = contents[0]["text"].encode("utf-8")
            assert len(raw) == res["size"] and "sha256:" + hashlib.sha256(raw).hexdigest() == res["digest"], res["uri"]
