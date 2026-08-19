"""CI-553/CI-562: 管制前体披露必须出现在**文本**里，不能只躺在 structuredContent。

CI-541 在 /compatibility/check · /risk-warnings · /batch-safety 的返回里加了顶层键
`precursor_disclosure`。`_expose()` 默认全透 ⇒ 它自动进 structuredContent，MCP 侧零改动；
但模型读的 `TextContent` 是各工具**逐字段显式拼**出来的，没人提的键就永远不出现。
`batch_safety_check` 更彻底：它返回的是**裸字符串**，连 structuredContent 都没有 ⇒
在那条路径上披露 100% 丢失。

🔴 判据落在**用户/模型真正读到的那串文本**上。这一条是这类 bug 的通用形状（同族 CI-360
`insufficient_reason`、CI-342）：后端加字段 + 有测试 = 「后端这半完成了」，而渲染层不认
新键，于是修好的东西一个用户也到不了（[[fix-never-reaches-the-real-consumer]]）。

另一条被钉住的是**失效方向**：命中了但 `statement` 为空时不许静默跳过——那正是最该说话
的时候。此时要退回机器可读的分面并明说「措辞没拿到」。
"""
import asyncio

import server


def _run(tool, patch_name, payload, *args):
    async def _fake(*_a, **_k):
        return payload
    orig = getattr(server, patch_name)
    setattr(server, patch_name, _fake)
    try:
        res = asyncio.run(tool(*args))
        return res.content[0].text if hasattr(res, "content") else res
    finally:
        setattr(server, patch_name, orig)


CWC_STATEMENT = ("This substance is listed on CWC Schedule 3B of the Chemical Weapons "
                 "Convention and is therefore export-controlled. This tool only reports "
                 "list membership; it is not a compliance determination.")

DISCLOSURE = [{
    "cas": "7647-01-0", "query_name": "hydrochloric acid",
    "matched_name": "Hydrogen chloride", "regime": "chemical_weapons",
    "tier": "CWC Schedule 3B", "authority": "OPCW", "note": "",
    "statement": CWC_STATEMENT,
}]

COMPAT = {
    "pairs": [{"chemical_a": "hydrochloric acid", "chemical_b": "acetone",
               "level": "caution", "reason": "exothermic"}],
    "name_to_cas": {"hydrochloric acid": "7647-01-0", "acetone": "67-64-1"},
    "unresolved": [], "documents": [], "precursor_disclosure": DISCLOSURE,
}
RISK = {
    "warnings": [{"chemical_name": "hydrochloric acid", "cas": "7647-01-0",
                  "hazards": ["H314"], "risk_level": "high"}],
    "unresolved": [], "documents": [], "precursor_disclosure": DISCLOSURE,
}
BATCH = {
    "compatibility": {"pairs": [], "summary": {"compatible": 1}},
    "risk_warnings": [], "ppe": {}, "unresolved": [], "documents": [],
    "precursor_disclosure": DISCLOSURE,
}


def test_compatibility_text_carries_the_disclosure():
    out = _run(server.check_chemical_compatibility, "_direct_compat", COMPAT,
               ["hydrochloric acid", "acetone"])
    assert "Regulated-precursor notice" in out, out
    assert CWC_STATEMENT in out, out
    assert "hydrochloric acid" in out


def test_risk_warnings_text_carries_the_disclosure():
    out = _run(server.get_chemical_risk_warnings, "_direct_risk", RISK,
               ["hydrochloric acid"])
    assert "Regulated-precursor notice" in out, out
    assert CWC_STATEMENT in out, out


def test_batch_safety_text_carries_the_disclosure():
    """batch_safety_check 返回裸字符串——它是唯一连 structuredContent 兜底都没有的那条路径，
    此前披露在这里 100% 丢失。"""
    out = _run(server.batch_safety_check, "_direct_batch", BATCH,
               ["hydrochloric acid", "acetone"])
    assert "Regulated-precursor notice" in out, out
    assert CWC_STATEMENT in out, out


def test_disclosure_says_the_answer_still_stands():
    """🔴 CI-541 的裁定是「答 + 附披露」，不是拒答。措辞若滑向「因此不予评估」，就是把一个
    失效方向（完全不提示）换成了另一个（无差别拒答）——这条守卫钉的是那个滑坡。"""
    out = _run(server.check_chemical_compatibility, "_direct_compat", COMPAT,
               ["hydrochloric acid", "acetone"])
    assert "still performed" in out, out
    # 分析结果本身必须还在
    assert "CAUTION" in out.upper(), out


def test_hit_without_statement_is_still_disclosed():
    """命中了但后端没给 `statement` 时不许静默跳过——那是最该说话的时刻。"""
    payload = dict(COMPAT)
    payload["precursor_disclosure"] = [{
        "cas": "7647-01-0", "query_name": "hydrochloric acid",
        "regime": "drug_precursor", "tier": "EU Category 2", "authority": "EU",
        "statement": "",
    }]
    out = _run(server.check_chemical_compatibility, "_direct_compat", payload,
               ["hydrochloric acid", "acetone"])
    assert "hydrochloric acid" in out
    assert "drug_precursor" in out and "EU Category 2" in out, out
    assert "not returned by the backend" in out, out


def test_no_disclosure_key_renders_nothing():
    """没有命中时不许凭空长出一段——否则每次查普通化学品都会看到一个空的管制段。"""
    payload = {k: v for k, v in COMPAT.items() if k != "precursor_disclosure"}
    out = _run(server.check_chemical_compatibility, "_direct_compat", payload,
               ["water", "acetone"])
    assert "Regulated-precursor" not in out, out
