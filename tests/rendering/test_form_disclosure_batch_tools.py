"""CI-577 —— 三个批量工具把后端顶层 `physical_form_disclosures` 渲染进文本面。

后端 `/compatibility/check`、`/risk-warnings`、`/batch-safety` 补上了记录驱动的形态披露
（「库里那份 SDS 说的是水溶液形态」）。`_expose()` 会把键带进 structuredContent，
但模型读 text ⇒ 渲染器不写就等于没修（CI-553 / CI-842 同一个形状）。

🔴 Recorded mutations（2026-10-04 实跑）:
  1. 任一工具删掉 `_physical_form_disclosures_block(data)` 那一行 → 该工具的参数化用例红。
  2. 渲染不再带化学品名 → `test_each_line_names_its_chemical` 红。
  3. 去掉 `physical_form_lookup_failed` 那一句 → `test_lookup_failure_is_said_not_swallowed` 红。
"""
import asyncio

import pytest

import server

NOTE = ("The SDS we hold for this CAS describes the aqueous solution, not the "
        "anhydrous form.")
ENTRY = {"chemical": "hydrofluoric acid", "cas": "7664-39-3",
         "physical_form": "aqueous_solution", "physical_form_disclosure": NOTE}
SILENT = {"chemical": "acetone", "cas": "67-64-1",
          "physical_form": None, "physical_form_disclosure": None}

_BASE = {"unresolved": [], "documents": [], "query_form_disclosures": []}
COMPAT = {**_BASE, "pairs": [{"chem1": "hydrofluoric acid", "chem2": "acetone",
                              "level": "compatible", "reason": "x", "source": "rule_engine"}]}
RISK = {**_BASE, "warnings": [], "unresolved_detail": []}
BATCH = {**_BASE, "compatibility": {"pairs": [], "summary": {}}, "risk_warnings": [], "ppe": {}}

_CASES = [
    ("check_chemical_compatibility", "_direct_compat", COMPAT, (["hydrofluoric acid", "acetone"],)),
    ("get_chemical_risk_warnings", "_direct_risk", RISK, (["hydrofluoric acid"],)),
    ("batch_safety_check", "_direct_batch", BATCH, (["hydrofluoric acid", "acetone"],)),
]


def _run(tool_name, patch_name, payload, args):
    async def _fake(*_a, **_k):
        return payload
    orig = getattr(server, patch_name)
    setattr(server, patch_name, _fake)
    try:
        res = asyncio.run(getattr(server, tool_name)(*args))
        return res.content[0].text if hasattr(res, "content") else res
    finally:
        setattr(server, patch_name, orig)


@pytest.mark.parametrize("tool,patch,payload,args", _CASES, ids=[c[0] for c in _CASES])
def test_batch_tools_render_the_disclosure(tool, patch, payload, args):
    out = _run(tool, patch, {**payload, "physical_form_disclosures": [ENTRY, SILENT]}, args)
    assert NOTE in out, f"{tool} 的文本面没有这句披露：\n{out}"


@pytest.mark.parametrize("tool,patch,payload,args", _CASES, ids=[c[0] for c in _CASES])
def test_each_line_names_its_chemical(tool, patch, payload, args):
    out = _run(tool, patch, {**payload, "physical_form_disclosures": [ENTRY]}, args)
    assert f"hydrofluoric acid: {NOTE}" in out


def test_none_entries_stay_silent():
    assert server._physical_form_disclosures_block({"physical_form_disclosures": [SILENT]}) == []


@pytest.mark.parametrize("tool,patch,payload,args", _CASES, ids=[c[0] for c in _CASES])
def test_lookup_failure_is_said_not_swallowed(tool, patch, payload, args):
    out = _run(tool, patch, {**payload, "physical_form_disclosures": [],
                             "physical_form_lookup_failed": True}, args)
    assert "physical-form check" in out
