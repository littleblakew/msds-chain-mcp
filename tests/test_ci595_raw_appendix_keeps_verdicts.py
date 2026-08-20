"""CI-595 —— 原始数据附录不许把安全结论截掉。

## 形状（Prod 实测，不是推的）

`_format_tool_results` 原来对**每个工具条目**做 `json.dumps(result)[:600]`。
逐对的 `check_compatibility` 结果各自远小于 600，所以每一对都活着；[[CI-589]] 之后
快聊面改发**一份整份矩阵**，Prod 实测 **10,227 字符** ⇒ 附录被切在 JSON 中间，
**6 对里只剩 2 对，而被切掉的正是排在后面的那几对**：

    acetone + bleach            ✅ 活着
    acetone + hydrochloric acid ✅ 活着（切在这条的 reason 中间）
    bleach  + hydrochloric acid ❌ 氯气 —— 没了
    bleach  + ammonia           ❌ 氯胺 —— 没了
    hydrochloric acid + ammonia ❌ 没了

CI-589 存在的全部理由就是那两对。而本文件另一处注释已经写明：**多数 MCP 客户端只读
text，`structuredContent` 不进模型上下文** ⇒ 这条通道不是可有可无的补充。

## 判据为什么这么定

- **结论不许丢**：`verdict` 住在字段里，解释住在长字符串里。压缩必须先丢**派生的**
  重复内容（`warnings` 由 matrix 逐对派生，Prod 上占 6,061 字符）、再缩短解释，
  **最后才轮到结论**。
- **丢了就要说**：任何被丢弃的条目都要留 `_omitted_*` 记号。静默丢弃就是把
  「我们没说」变成「它不存在」——本线反复栽的正是这个。
- **必须仍是合法 JSON**：按字节切会切在 JSON 中间，模型读到的是残句。
"""
import json
import re

import pytest

import server


def _pair(a: str, b: str, verdict: str) -> dict:
    return {
        "chemical_a": a, "cas_a": "1-1-1", "chemical_b": b, "cas_b": "2-2-2",
        "level": verdict, "verdict": verdict,
        "reason": f"{a} + {b}: " + "why this matters " * 6,
        "source": "reactive_group_rule",
        "source_detail": "Literature/NFPA: " + "detail " * 8,
        "citation": "lit:example-NFPA430",
    }


def _prod_shaped_matrix_result() -> dict:
    """按 Prod 实测的构成造：warnings 最大且由 matrix 派生、sources 带出处。"""
    chems = ["acetone", "bleach", "hydrochloric acid", "ammonia"]
    matrix = [
        _pair(a, b, "incompatible" if i else "use_caution")
        for i, (a, b) in enumerate(
            (x, y) for i, x in enumerate(chems) for y in chems[i + 1:])
    ]
    return {
        "matrix": matrix,
        "warnings": [
            {"level": "high", "chemical": f"{p['chemical_a']}+{p['chemical_b']}",
             "description": p["reason"] + " " + "padding " * 20,
             "mitigation": "Refer to each chemical's MSDS for safe handling",
             "reference": "Rule engine assessment"}
            for p in matrix
        ],
        "count": len(matrix),
        "sources": {c: {"supplier": f"SUPPLIER {c}", "revision_date": "2023-05-24",
                        "msds_version": "9.07", "region": "EU"} for c in chems},
        "grounded_count": 4, "ungrounded_count": 0, "ungrounded_chemicals": [],
    }


def test_every_verdict_survives_the_appendix():
    """🔴 本票的靶子：6 对结论一条都不能少，尤其排在后面的那几对。"""
    result = _prod_shaped_matrix_result()
    assert len(json.dumps(result, ensure_ascii=False)) > 4000, "前提：这份载荷确实超预算"

    text = server._format_tool_results([{"tool": "check_all_compatibility", "result": result}])
    rendered = text.split("`check_all_compatibility`: ", 1)[1]
    parsed = json.loads(rendered)  # 顺带钉住「必须仍是合法 JSON」

    assert len(parsed["matrix"]) == len(result["matrix"]), (
        "结论被丢了——压缩必须先丢派生内容、再缩短解释，最后才轮到结论"
    )
    got = {(p["chemical_a"], p["chemical_b"]) for p in parsed["matrix"]}
    for pair in (("bleach", "hydrochloric acid"), ("bleach", "ammonia")):
        assert pair in got, f"{pair} 没到达模型——那正是 CI-589 存在的理由"


def test_the_citation_fields_survive_too():
    """出处不是装饰：工具说明要求「必须引用供应商 + 版本日期」。"""
    parsed = json.loads(server._compact_for_context(_prod_shaped_matrix_result()))
    assert "sources" in parsed and parsed["sources"], "出处被整段丢掉了"
    one = next(iter(parsed["sources"].values()))
    assert one.get("supplier") and one.get("revision_date")


def test_dropping_anything_leaves_a_visible_marker():
    """丢了就要说——静默丢弃＝把「我们没说」变成「它不存在」。"""
    parsed = json.loads(server._compact_for_context(_prod_shaped_matrix_result()))
    dropped = len(_prod_shaped_matrix_result()["warnings"]) - len(parsed.get("warnings", []))
    if dropped:
        assert parsed.get("_omitted_warnings") == dropped, parsed.keys()


def test_a_payload_without_lists_still_says_it_was_trimmed():
    """没有列表结构可压时退回字节截断，但**必须写出截掉了多少**。"""
    blob = {"note": "x" * 5000}
    out = server._compact_for_context(blob, budget=200)
    # 判据是「**量级**可见」，不是某一句固定措辞：字符串级缩短写 `(+N chars)`，
    # 整体兜底截断写 `(+N chars trimmed)`。只留 "..." 才是不合格的——那样模型
    # 无从判断自己错过了多少。
    assert re.search(r"\(\+\d+ chars", out), out[-80:]
    assert len(out) <= 200


def test_small_results_are_untouched():
    """够短的原样输出——别为了统一而给所有结果加噪声。"""
    small = {"query": "acetone", "match_count": 1}
    assert server._compact_for_context(small) == json.dumps(small, ensure_ascii=False)


def test_the_whole_appendix_is_bounded():
    """单条有预算不等于整份有界：五个大结果照样能把上下文撑爆。"""
    results = [{"tool": f"t{i}", "result": _prod_shaped_matrix_result()} for i in range(5)]
    text = server._format_tool_results(results)
    assert len(text) <= server._RAW_TOTAL_BUDGET + 500, len(text)
    assert "budget exhausted" in text, "被总预算挡掉的条目也要说出来，别静默消失"
