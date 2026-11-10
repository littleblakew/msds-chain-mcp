"""CI-1559：Microsoft 365 Copilot 只读工具描述的前 1,024 个字符。

判据打在 live registry 下发的 `Tool.description` 上（客户端真正收到的那份），不是 docstring。
超出预算的工具必须有一行单独的 `Reference:`，它之前的引导段（用途、触发条件、红线）
必须落在预算内；后面的细节被截掉也不会让客户端选错工具或漏掉红线。

🔬 变异（实跑过）：
- 删掉 `app._Server.tool` 里的 `client_description(...)`，改回原样 docstring
  ⇒ `test_no_args_section_or_indent_reaches_clients` 与 `test_lead_fits_budget` 都红。
- 把 `check_regulatory_compliance` 的 `Reference:` 行删掉 ⇒ `test_lead_fits_budget` 红，
  并点名这个工具。
"""
import asyncio

import server
from msds_mcp.app import DESCRIPTION_BUDGET

_MARKER = "\nReference:\n"


def _tools():
    return asyncio.run(server.mcp.list_tools())


def test_lead_fits_budget():
    over = []
    for t in _tools():
        d = t.description or ""
        if len(d) <= DESCRIPTION_BUDGET:
            continue
        lead = d.split(_MARKER, 1)[0] if _MARKER in d else d
        if len(lead) > DESCRIPTION_BUDGET:
            over.append(f"{t.name}: {len(lead)} chars before `Reference:` (budget {DESCRIPTION_BUDGET})")
    assert not over, (
        "These descriptions put more than the budget before their `Reference:` line, so a "
        "client that truncates at the budget loses part of the lead (purpose, when to use, "
        "red lines). Move details below a `Reference:` line or tighten the lead:\n"
        + "\n".join(over))


def test_no_args_section_or_indent_reaches_clients():
    bad = [t.name for t in _tools()
           if "\nArgs:\n" in (t.description or "") or (t.description or "").startswith((" ", "\n"))]
    assert not bad, f"raw docstring layout reaches clients: {bad}"


def test_marker_is_only_used_where_needed():
    """A `Reference:` line on a short description is harmless but hides a lead that never needed it."""
    assert [t.name for t in _tools() if _MARKER in (t.description or "")
            and len(t.description) <= DESCRIPTION_BUDGET] == []


def test_guard_is_not_a_noop():
    tools = _tools()
    assert len(tools) >= 20
    assert any(len(t.description) > DESCRIPTION_BUDGET for t in tools), (
        "no description exceeds the budget any more: the lead check above no longer "
        "exercises anything; if that is intended, delete this test")
