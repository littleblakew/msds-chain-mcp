"""CI-711 —— 原始数据附录的裁剪认 `[protocol]` 行：规程行最后才挨刀，且有上限。

## 形状

`_format_tool_results` 把 `_RAW_TOTAL_BUDGET` 分摊给多个工具；份额小时 `_shorten_strings`
的 allowance 收紧到 160（keep=140），一条 `[protocol]` 物质级急救规程行被切在句子中间，
丢的是后半句的警示（HF：不痛也要就医）。后端 `tool_payload` 的 CI-574 规则是
「规程行最后才挨刀」——这里同一规则：先缩通用文本，仍放不下才缩规程行，
且规程行也走收紧梯度（排序不是豁免，豁免会让一条病态长规程撑爆预算）。
"""
import json

import server

TAIL = "Seek medical care even if there is no pain - symptoms can be delayed."
PROTOCOL = (
    "[protocol] HF-SPECIFIC FIRST AID: flush skin with water for 5 minutes, then apply "
    "2.5% calcium gluconate gel and keep massaging it in until pain relief continues. "
    "Remove contaminated clothing while flushing; do not delay for gel. "
    + TAIL
)


def _generic(n: int) -> dict:
    return {"verdict": "ok", "notes": ["generic ghs text " * 12 for _ in range(n)]}


def _tool_results() -> list[dict]:
    return [
        {"tool": "check_compatibility", "result": _generic(40)},
        {"tool": "get_risk_warnings", "result": _generic(40)},
        {"tool": "get_emergency_response",
         "result": {"first_aid": [PROTOCOL] + ["generic rinse text " * 10] * 30}},
    ]


def test_protocol_tail_survives_shared_budget():
    out = server._format_tool_results(_tool_results())
    assert TAIL in out


def test_protocol_line_is_not_a_bypass_of_the_budget():
    huge = "[protocol] " + "step " * 3000
    out = server._compact_for_context({"first_aid": [huge], "n": _generic(5)}, 1500)
    assert len(out) <= 1500
    # 规程行被收紧梯度缩短（仍是合法 JSON），而不是靠兜底字节截断才勉强放进预算
    assert json.loads(out)["first_aid"][0].startswith("[protocol]")


def test_protocol_is_cut_only_after_generic_text_is_exhausted():
    # 预算放不下完整规程时，规程仍被收紧（排序不是豁免），但通用文本先挨刀
    result = {"first_aid": [PROTOCOL], "notes": ["generic ghs text " * 12] * 6}
    budget = len(PROTOCOL) + 300
    out = server._compact_for_context(result, budget)
    assert len(out) <= budget
    assert TAIL in out
    assert out.count("generic ghs text") < 6 * 12 or "chars)" in out


def test_non_protocol_long_strings_are_still_shortened():
    out = server._compact_for_context({"reason": "x" * 5000}, 1000)
    assert "chars)" in out and len(out) <= 1000


def test_output_is_still_json_when_only_strings_are_cut():
    out = server._compact_for_context(
        {"first_aid": [PROTOCOL], "notes": ["generic ghs text " * 12] * 20}, 3000)
    assert TAIL in json.loads(out)["first_aid"][0]
