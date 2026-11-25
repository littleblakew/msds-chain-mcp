"""改过名的工具：旧名不进 `tools/list`，但按旧名调用仍然走到新工具（CI-1303）。"""
import asyncio

import server
from msds_mcp.app import TOOL_ALIASES


def test_alias_targets_are_registered_and_aliases_are_not_listed():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    for old, new in TOOL_ALIASES.items():
        assert new in names, f"别名 {old} 指向的 {new} 没注册"
        assert old not in names, f"旧名 {old} 不该出现在 tools/list 里"


def test_old_name_still_callable(monkeypatch):
    async def fake(chemical_name="", cas_number=""):
        return {"status": "found", "chemical_name": "Acetonitrile", "cas_number": "75-05-8",
                "ghs": {"signal_word": "Danger", "h_codes": ["H225"]}}
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")
    monkeypatch.setattr(server, "_direct_online_search", fake)
    res = asyncio.run(server.mcp.call_tool("search_msds_online", {"chemical_name": "acetonitrile"}))
    assert not res.is_error
    assert "Acetonitrile" in res.content[0].text


def test_surface_diff_honours_alias_but_still_checks_params():
    """别名只免「工具消失」这一条；新工具少了旧参数仍然判 breaking（变异方向：新名变紧）。"""
    from tool_surface import diff_surface
    base = {"old": {"required": [], "optional": ["a", "b"]}}
    ok = diff_surface(base, {"new": {"required": [], "optional": ["a", "b"]}}, {"old": "new"})
    assert not ok["breaking"] and any("改名" in s for s in ok["additive"])
    bad = diff_surface(base, {"new": {"required": [], "optional": ["a"]}}, {"old": "new"})
    assert any("`b`" in s for s in bad["breaking"])
    assert diff_surface(base, {"new": {"required": [], "optional": ["a", "b"]}})["breaking"]
