"""CI-1559：Microsoft 365 Copilot 只读工具描述的前 1,024 个字符。

判据打在 live registry 下发的 `Tool.description` 上（客户端真正收到的那份），不是 docstring。
超出预算的工具必须有一行单独的 `Reference:`，它之前的引导段（用途、触发条件、红线）
必须落在预算内；后面的细节被截掉也不会让客户端选错工具或漏掉红线。
🔴 哪句算红线这里判不了：往 `Reference:` 后面挪句子时，要问「截掉它，模型会不会答错」。

🔬 变异（实跑过）：
- 删掉 `app._Server.tool` 里的 `client_description(...)`，改回原样 docstring
  ⇒ `test_no_args_section_or_indent_reaches_clients` 与 `test_lead_fits_budget` 都红。
- 把 `check_regulatory_compliance` 的 `Reference:` 行删掉 ⇒ `test_lead_fits_budget` 红，
  并点名这个工具。
"""
import asyncio

import pytest

import server
from msds_mcp.app import DESCRIPTION_BUDGET, client_description

_MARKER = "\nReference:\n"


def _tools():
    return asyncio.run(server.mcp.list_tools())


def _units(text: str) -> int:
    """UTF-16 code units: 🔴 counts as 2, so a client counting either way stays inside."""
    return len(text.encode("utf-16-le")) // 2


def test_lead_fits_budget():
    over = []
    for t in _tools():
        d = t.description or ""
        if _units(d) <= DESCRIPTION_BUDGET:
            continue
        lead = d.split(_MARKER, 1)[0] if _MARKER in d else d
        if _units(lead) > DESCRIPTION_BUDGET:
            over.append(f"{t.name}: {_units(lead)} units before `Reference:` (budget {DESCRIPTION_BUDGET})")
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
            and _units(t.description) <= DESCRIPTION_BUDGET] == []


def test_only_the_args_section_is_dropped():
    doc = """
        Lead.

        Args:
            x: the x

        Raises:
            ValueError: kept

        Returns:
            kept too
        """
    out = client_description(doc)
    assert "the x" not in out and "Raises:" in out and "kept too" in out


def test_bare_decorator_still_raises():
    async def probe() -> str:
        return ""
    with pytest.raises(TypeError):
        server.mcp.tool(probe)


def test_guard_is_not_a_noop():
    tools = _tools()
    assert len(tools) >= 20
    assert any(_units(t.description) > DESCRIPTION_BUDGET for t in tools), (
        "no description exceeds the budget any more: the lead check above no longer "
        "exercises anything; if that is intended, delete this test")
