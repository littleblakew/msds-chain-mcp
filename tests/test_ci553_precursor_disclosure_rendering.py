"""CI-553/CI-562: 管制前体披露必须出现在**文本**里，不能只躺在 structuredContent。

CI-541 在 /compatibility/check · /risk-warnings · /batch-safety 的返回里加了顶层键
`precursor_disclosure`。`_expose()` 默认全透 ⇒ 它自动进 structuredContent，MCP 侧零改动；
但模型读的 `TextContent` 是各工具**逐字段显式拼**出来的，没人提的键就永远不出现 ⇒
三个工具的文本面全都没有它。

🔴 判据落在**用户/模型真正读到的那串文本**上。这一条是这类 bug 的通用形状（同族 CI-360
`insufficient_reason`、CI-342）：后端加字段 + 有测试 = 「后端这半完成了」，而渲染层不认
新键，于是修好的东西一个用户也到不了（[[fix-never-reaches-the-real-consumer]]）。

⚠️ **别复述 CI-553 票里那句「batch 返回裸字符串所以连 structuredContent 都没有」**——实测
是错的：它签名写 `-> str`，实际返回带 `_expose(data)` 的 CallToolResult
（`test_ci342_structured_passthrough.py` 正是钉这个）。三个工具对结构化客户端都到得了，
缺的一直是**文本面**。

🔴 fixture 一律用**渲染器真正读的字段名**（pairs 用 `chem1`/`chem2`、warnings 用
`chemical`/`level`/`description`）。用对外命名（`chemical_a`/`chemical_name`）会让每一行渲染成
`**?** + **?**` / `### Unknown — UNKNOWN RISK` —— 那样「分析结果还在」这条守卫就是拿一堆
空行在自证，[[green-test-that-executed-nothing]] 的形状。

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
    "pairs": [{"chem1": "hydrochloric acid", "chem2": "acetone", "level": "caution",
               "reason": "exothermic reaction with organic solvents",
               "traceability": "rule_based", "source": "reactive_groups"}],
    "unresolved": [], "documents": [], "precursor_disclosure": DISCLOSURE,
}
RISK = {
    "warnings": [{"chemical": "hydrochloric acid", "cas": "7647-01-0", "level": "high",
                  "description": "Causes severe skin burns and eye damage.",
                  "mitigation": "Handle in a fume hood with acid-resistant gloves.",
                  "traceability": "sds_backed"}],
    "unresolved": [], "documents": [], "precursor_disclosure": DISCLOSURE,
}
BATCH = {
    "compatibility": {"pairs": [{"chem1": "hydrochloric acid", "chem2": "acetone",
                                 "level": "caution", "reason": "exothermic",
                                 "traceability": "rule_based"}],
                      "summary": {"compatible": 0, "caution": 1, "incompatible": 0}},
    "risk_warnings": [{"chemical": "hydrochloric acid", "level": "high",
                       "description": "Causes severe skin burns.",
                       "mitigation": "Fume hood + acid-resistant gloves."}],
    "ppe": {}, "unresolved": [], "documents": [], "precursor_disclosure": DISCLOSURE,
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
    """batch 这条单列一个用例：它的文本是 `sections` 另一套拼装，和上面两个共用不了守卫，
    最容易在加字段时被漏掉（本次三处里也确实是最后想起来的那处）。"""
    out = _run(server.batch_safety_check, "_direct_batch", BATCH,
               ["hydrochloric acid", "acetone"])
    assert "Regulated-precursor notice" in out, out
    assert CWC_STATEMENT in out, out


def test_disclosure_says_the_answer_still_stands():
    """🔴 CI-541 的裁定是「答 + 附披露」，不是拒答。措辞若滑向「因此不予评估」，就是把一个
    失效方向（完全不提示）换成了另一个（无差别拒答）——这条守卫钉的是那个滑坡。"""
    out = _run(server.check_chemical_compatibility, "_direct_compat", COMPAT,
               ["hydrochloric acid", "acetone"])
    assert "not a refusal" in out and "results follow below" in out, out
    # 分析结果本身必须还在，而且是**真渲染出来的那一行**（不是一行 `**?** + **?**`）
    assert "**hydrochloric acid** + **acetone**" in out, out
    assert "exothermic reaction with organic solvents" in out, out


def test_truncated_batch_does_not_claim_everything_was_analysed():
    """🔴 后端**在 12 个的截断闸门之前**算披露（有意为之：被丢掉的那个用户确实提交过），
    而本工具收 20 个 ⇒ 被披露的化学品可能正是没进分析的那个。

    抬头若笼统说「下面都分析过了」，模型就会把一个根本没参与计算的物质当成已评估——比不
    披露更糟。有 `truncated` 时必须明说。
    """
    payload = dict(BATCH)
    payload["truncated"] = True
    out = _run(server.batch_safety_check, "_direct_batch", payload,
               ["hydrochloric acid"] + [f"chem{i}" for i in range(1, 13)])
    assert "not every submitted chemical was analysed" in out, out


def test_untruncated_batch_has_no_truncation_line():
    out = _run(server.batch_safety_check, "_direct_batch", BATCH,
               ["hydrochloric acid", "acetone"])
    assert "not every submitted chemical was analysed" not in out, out


def test_non_dict_entry_does_not_kill_the_answer():
    """披露块跑在所有结果渲染之前 ⇒ 这里抛异常＝用户拿不到他要的相容性答案，
    比「少一段披露」严重得多。"""
    payload = dict(COMPAT)
    payload["precursor_disclosure"] = ["hydrochloric acid is listed"]
    out = _run(server.check_chemical_compatibility, "_direct_compat", payload,
               ["hydrochloric acid", "acetone"])
    assert "hydrochloric acid is listed" in out, out
    assert "**hydrochloric acid** + **acetone**" in out, out


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


def test_empty_disclosure_renders_nothing():
    """没有命中时不许凭空长出一段——否则每次查普通化学品都会看到一个空的管制段。

    🔴 生产形状是 `precursor_disclosure: []`（后端**总是**带这个键），不是没有这个键；
    两种都测，别只测那个生产里不会出现的。
    """
    for payload in ({**COMPAT, "precursor_disclosure": []},
                    {k: v for k, v in COMPAT.items() if k != "precursor_disclosure"}):
        out = _run(server.check_chemical_compatibility, "_direct_compat", payload,
                   ["water", "acetone"])
        assert "Regulated-precursor" not in out, out


# ── 折叠同一 regime 的重复行（2026-08-19 live 实测促成）────────────────────────
# 这份 payload 是 Prod 上 `check_chemical_compatibility(["hydrochloric acid","acetone"])`
# **真实返回**的 7 条（照抄，别改成手编的简化版）：日常大宗化学品同时挂在同一 regime 的
# 多张清单上，逐条打印会在回答开头堆出七段近乎相同的话。
def _drug(cas, name, tier, authority):
    return {"cas": cas, "query_name": name, "matched_name": name,
            "regime": "drug_precursor", "tier": tier, "authority": authority, "note": "",
            "statement": (f"This substance is listed on {tier} and is a monitored "
                          "drug-precursor chemical. Purchase, storage and transfer may "
                          "require registration or reporting in some jurisdictions. It is "
                          "a bulk industrial chemical — appearing on a control list does "
                          "not mean there is anything unusual about this query.")}


ACETONE_EXPLOSIVE = {
    "cas": "67-64-1", "query_name": "acetone", "matched_name": "Acetone",
    "regime": "explosive_precursor", "tier": "EU 2019/1148 Annex II",
    "authority": "Regulation (EU) 2019/1148", "note": "",
    "statement": ("This substance is listed on EU 2019/1148 Annex II. Suspicious "
                  "transactions involving it are reportable; no concentration threshold "
                  "applies. It is a bulk industrial chemical — appearing on a control "
                  "list does not mean there is anything unusual about this query."),
}

LIVE_SEVEN = [
    _drug("7647-01-0", "hydrochloric acid", "UN 1988 Table II",
          "UN 1988 Convention Tables (INCB Red List, 23rd ed., 2025)"),
    _drug("7647-01-0", "hydrochloric acid", "DEA List II", "21 CFR 1310.02"),
    _drug("7647-01-0", "hydrochloric acid", "EU 273/2004 Category 3",
          "Regulation (EC) No 273/2004, Annex I"),
    _drug("67-64-1", "acetone", "UN 1988 Table II",
          "UN 1988 Convention Tables (INCB Red List, 23rd ed., 2025)"),
    _drug("67-64-1", "acetone", "DEA List II", "21 CFR 1310.02"),
    _drug("67-64-1", "acetone", "EU 273/2004 Category 3",
          "Regulation (EC) No 273/2004, Annex I"),
    ACETONE_EXPLOSIVE,
]


def _block(entries):
    return "\n".join(server._precursor_disclosure_block({"precursor_disclosure": entries}))


def test_same_regime_duplicates_collapse_to_one_sentence():
    """同一物质同一 regime 的多张清单：整句只说一次，其余只列清单名。"""
    out = _block(LIVE_SEVEN)
    assert out.count("is a monitored drug-precursor chemical") == 2, out  # HCl 一次、丙酮一次
    assert "Also listed on: DEA List II (21 CFR 1310.02) · EU 273/2004 Category 3" in out, out


def test_a_different_regime_is_never_collapsed():
    """🔴 丙酮的 EU 2019/1148 Annex II 讲的是「可疑交易须报告、无浓度阈值」，与毒品前体监控
    是两回事——它必须原样打印，不能被折进「Also listed on」。"""
    out = _block(LIVE_SEVEN)
    assert "Suspicious transactions involving it are reportable" in out, out


def test_same_regime_but_materially_different_text_is_kept_in_full():
    """折叠的判据是「把清单名换掉之后逐字相同」。不满足就必须全文打印——比如带浓度阈值的那种。"""
    a = _drug("1310-73-2", "sodium hydroxide", "Annex I", "EU")
    b = dict(a, tier="Annex II",
             statement="This substance is listed on Annex II with a limit value of 12 % w/w.")
    out = _block([a, b])
    assert "limit value of 12 % w/w" in out, out
    assert "Also listed on" not in out, out


def test_collapsing_actually_shortens_the_live_payload():
    """钉住效果本身：真实那 7 条 ⇒ **3 段整句**（HCl 毒品前体 / 丙酮毒品前体 / 丙酮爆炸物前体）
    ＋ 2 行清单名折叠。没有这条，折叠逻辑被改回「全展开」时上面几条断言仍可能通过。"""
    body = _block(LIVE_SEVEN)
    full = [l for l in body.splitlines() if l.startswith("- ")]
    collapsed = [l for l in body.splitlines() if l.lstrip().startswith("Also listed on:")]
    assert len(full) == 3, full
    assert len(collapsed) == 2, collapsed
