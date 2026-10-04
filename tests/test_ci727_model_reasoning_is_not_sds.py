"""CI-727: 模型推理出来的相容性判定，不许被说成有 SDS 出处。

后端把 LLM 升级过的对标成 `llm_escalated`。本层的红线是：只有 `sds_backed` 才许渲染成
「Source (SDS)」；`llm_escalated` 与任何不认识的值都落到明确的非 SDS 说法上。

变异（造的时候回读被改行并断言落在目标上）：
  ① `_pair_basis_label` 的兜底 return 改回 "Source (SDS)"        → unknown 用例红
  ② `llm_escalated` 分支删掉（落到兜底）                          → llm 用例的精确文案红
  ③ `_traceability_label` 里「未知非空值」分支删掉（落回推断）    → label 的 unknown 用例红
  ④ 单对 / batch 两处调用点之一改回内联三元式                     → 对应工具用例红
"""
import asyncio

import pytest

import server

_DOCS = [{"chemical": "acetone", "sds_document_url": "https://example.com/a"}]


def _pair(traceability):
    p = {"chem1": "acetone", "chem2": "water", "level": "caution",
         "reason": "model says so", "source": "ai_agent"}
    if traceability is not None:
        p["traceability"] = traceability
    return p


@pytest.mark.parametrize("value,expected", [
    ("rule_based", "Basis (rule)"),
    ("sds_backed", "Source (SDS)"),
    ("llm_escalated", "Basis (model reasoning, not an SDS statement)"),
    ("brand_new_value", "Basis (unverified origin, not an SDS statement)"),
])
def test_pair_basis_label(value, expected):
    assert server._pair_basis_label(value) == expected


@pytest.mark.parametrize("value", ["llm_escalated", "brand_new_value"])
def test_single_pair_tool_never_claims_sds(monkeypatch, value):
    async def fake(chemicals, **_):
        return {"pairs": [_pair(value)], "unresolved": [], "documents": _DOCS}
    monkeypatch.setattr(server, "_direct_compat", fake)
    res = asyncio.run(server.check_chemical_compatibility(["acetone", "water"]))
    text = res.content[0].text
    assert "Source (SDS)" not in text, text
    assert "not an SDS statement" in text, text
    assert res.structured_content["pairs"][0]["traceability"] == value


@pytest.mark.parametrize("value", ["llm_escalated", "brand_new_value"])
def test_batch_tool_never_claims_sds(monkeypatch, value):
    async def fake(chemicals, **_):
        return {"compatibility": {"summary": {"total": 1, "compatible": 0, "caution": 1,
                                              "incompatible": 0},
                                  "pairs": [_pair(value)]},
                "risk_warnings": [], "unresolved": [], "documents": _DOCS}
    monkeypatch.setattr(server, "_direct_batch", fake)
    res = asyncio.run(server.batch_safety_check(["acetone", "water"]))
    text = res.content[0].text
    assert "Source (SDS)" not in text, text
    assert "not an SDS statement" in text, text


def test_traceability_label_unknown_value_ignores_document_inference():
    # 即便这个化学品有文档，一个声明了别种出处类别的值也不能被推断成 SDS 出处。
    out = server._traceability_label("brand_new_value", "acetone", {"acetone"})
    assert "Source: SDS" not in out and "not an SDS statement" in out
    assert "model reasoning" in server._traceability_label("llm_escalated", "acetone", {"acetone"})
    # 字段缺失（老后端）仍走推断，行为不变
    assert server._traceability_label(None, "acetone", {"acetone"}) == "[Source: SDS document]"
