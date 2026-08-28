"""CI-666：「匹配到了记录、但它没有危害数据」必须出现在**文本**里。

后端在 `/risk-warnings` 与 `/batch-safety` 的返回里加了顶层 `no_hazard_basis`。
`_expose()` 默认全透 ⇒ 它自动进 structuredContent；**但模型读的是 `TextContent`**，
而那是各工具逐字段显式拼出来的 ⇒ 没人提的键永远不出现。

🔴 **不补这一层，本票等于没修**：票的原始复现就是走 MCP 的
`get_chemical_risk_warnings(["carbon disulfide"])` —— 响应格式完整、内容为零，而模型在
文本里唯一看到的是 `"No risk warnings found for the given chemicals."`，读起来正是
「查过了，没有」。同族形状：CI-553/CI-562（`precursor_disclosure`）、CI-360
（`insufficient_reason`）——后端加字段 + 有测试 ＝「后端这半完成了」，而用户一个也收不到
（[[fix-never-reaches-the-real-consumer]]）。

🔴 措辞是后端渲染的（5 语言，i18n catalog 单一来源）——这里**不复述、不改写**，否则第二份
不受版本管理的副本开始漂移。那段措辞**刻意带上匹配到的 CAS**：物质层面的答案仍可能是错的
（一个名字可以匹配到错的记录），而 CAS 是调用方唯一能据以发现的线索 ⇒ 渲染必须把它带出去。
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


REASON_EN = (
    "We matched a record for this name (CAS 12539-80-9), but that record carries no "
    "hazard data at all — so no hazard warning could be produced. This is not a "
    "finding that the substance is safe."
)
GAP = [{"query": "carbon disulfide", "cas": "12539-80-9",
        "code": "no_hazard_data", "reason": "（中文措辞）", "reason_en": REASON_EN}]

# 🔴 复现票里那条 Prod 响应的真实形状：**warnings 是空的**。
# 把它写成非空会让这份守卫测不到它要防的那条路径（那条路径的全部特征就是「什么都没有」）。
RISK_EMPTY = {"warnings": [], "unresolved": [], "unresolved_detail": [],
              "documents": [], "no_hazard_basis": GAP}
BATCH_EMPTY = {"compatibility": {"pairs": [], "summary": {}}, "risk_warnings": [],
               "ppe": {}, "unresolved": [], "documents": [], "no_hazard_basis": GAP}


def test_risk_warnings_text_says_why_it_is_empty():
    """靶心：零危害的那份响应，文本里必须说出原因、点出 CAS。"""
    out = _run(server.get_chemical_risk_warnings, "_direct_risk", RISK_EMPTY,
               ["carbon disulfide"])
    assert "carries no hazard data" in out, out
    assert "12539-80-9" in out, "匹配到的 CAS 没进文本 —— 匹配错了时调用方无从发现"
    assert "not a finding that the substance is safe" in out, out


def test_batch_safety_text_says_why_it_is_empty():
    # 🔴 `batch_safety_check` 自己要求 ≥2 个化学品，只传 1 个会被它在渲染之前挡回
    # （"Please provide at least 2 chemicals…"）⇒ 用例根本走不到渲染层。
    out = _run(server.batch_safety_check, "_direct_batch", BATCH_EMPTY,
               ["carbon disulfide", "acetone"])
    assert "carries no hazard data" in out, out
    assert "12539-80-9" in out, out


def test_the_misleading_no_risk_line_is_suppressed():
    """🔴 失效方向：说清楚之后**不许**再补一句 "No risk warnings found"。

    那句话读起来就是「查过了、没有」——正是本票要消灭的读法。补在后面等于把刚说清的
    事情重新压回去，而且模型更可能采信那句更短、更肯定的。
    """
    out = _run(server.get_chemical_risk_warnings, "_direct_risk", RISK_EMPTY,
               ["carbon disulfide"])
    assert "No risk warnings found" not in out, out

    batch = _run(server.batch_safety_check, "_direct_batch", BATCH_EMPTY,
                 ["carbon disulfide", "acetone"])
    assert "No risk data available" not in batch, batch


def test_the_line_still_appears_when_there_is_genuinely_nothing_to_say():
    """反向：**没有** `no_hazard_basis` 时，原来那句兜底必须照旧出现。

    少了这条，把兜底整句删掉也全绿——而那会让「真的什么都没有」的响应连一句话都没有，
    是另一个方向的静默。
    """
    out = _run(server.get_chemical_risk_warnings, "_direct_risk",
               {"warnings": [], "unresolved": [], "documents": []}, ["whatever"])
    assert "No risk warnings found" in out, out


def test_a_malformed_entry_never_takes_down_the_answer():
    """🔴 一条畸形条目不许把整份安全答案换成一个工具错误。

    这个块跑在结果渲染**之前**，AttributeError 会顶掉用户真正要的东西——比它修的那个
    缺失更糟。同 `_precursor_disclosure_block` / `_unresolved_block` 的既有守法。
    """
    payload = dict(RISK_EMPTY, no_hazard_basis=["不是字典", None, GAP[0]])
    out = _run(server.get_chemical_risk_warnings, "_direct_risk", payload,
               ["carbon disulfide"])
    assert "12539-80-9" in out, "合法那条被畸形条目带崩了"
