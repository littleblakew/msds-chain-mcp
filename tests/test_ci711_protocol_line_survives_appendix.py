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


# ── 真实形状：急救按途径分组（dict of lists），规程行与通用 H 码话术混在同一列表里 ──
# 量级照 Prod 那次失败配（6 个条目 · HF 急救 16 条规程行 ≈ 2,100 字符）：旧实现把规程行
# 全切到 24 字符，并把 `skin` / `eye` 两条途径整条丢掉——而用户问的正是皮肤接触。

def _route(name: str, n_protocol: int, n_generic: int) -> list[str]:
    return ([f"[protocol] {name} step {i}: " + "do this specific thing now " * 4 + TAIL
             for i in range(n_protocol)]
            + [f"[H314] {name} generic advice {i} " + "rinse and seek help " * 3
               for i in range(n_generic)])


def _first_aid() -> dict:
    return {
        "chemical": "Hydrofluoric Acid",
        "source_info": {"supplier": "Example Supplier", "revision_date": "2022-09-01"},
        "routes": {"inhalation": _route("inhalation", 4, 8), "skin": _route("skin", 5, 8),
                   "eye": _route("eye", 4, 7), "ingestion": _route("ingestion", 3, 8)},
        "from_sds": ["section 4 line " * 8 for _ in range(25)],
    }


def _ppe(chem: str) -> dict:
    return {"chemical": chem,
            "source_info": {"supplier": "Example Supplier", "revision_date": "2020-01-15",
                            **{f"k{i}": "metadata value " * 6 for i in range(12)}},
            "ppe": {"gloves": ["[H314] heavy-duty gloves " * 6 for _ in range(10)]}}


def _six_entries() -> list[dict]:
    return [
        {"tool": "search_chemical", "result": {"query": "hf", "chemicals": [{"id": 1}]}},
        {"tool": "search_chemical", "result": {"query": "hno3", "chemicals": [{"id": 2}]}},
        {"tool": "ppe_recommendation", "result": _ppe("Hydrofluoric Acid")},
        {"tool": "ppe_recommendation", "result": _ppe("Nitric Acid")},
        {"tool": "first_aid_guidance", "result": _first_aid()},
        {"tool": "first_aid_guidance", "result": _generic(40)},
    ]


def _entry(out: str, n: int) -> dict:
    lines = [ln for ln in out.splitlines() if ln.startswith("`")]
    return json.loads(lines[n].split(": ", 1)[1])


def test_every_protocol_line_survives_six_way_split():
    out = server._format_tool_results(_six_entries())
    fa = _entry(out, 4)
    protocol = [x for r in fa["routes"].values() for x in r if x.startswith("[protocol]")]
    assert len(protocol) == 16
    assert all(x.endswith(TAIL) for x in protocol), "规程行被截短"


def test_route_with_protocol_lines_is_never_dropped_whole():
    fa = _entry(server._format_tool_results(_six_entries()), 4)
    assert set(fa["routes"]) == {"inhalation", "skin", "eye", "ingestion"}
    assert not fa.get("_omitted_routes")


def test_generic_lines_beside_protocol_yield_first_and_say_so():
    fa = _entry(server._format_tool_results(_six_entries()), 4)
    if fa.get("_omitted_generic_beside_protocol"):
        assert all(x.startswith("[protocol]") for r in fa["routes"].values() for x in r)


def test_entries_without_protocol_get_exactly_the_old_equal_share():
    """额外额度只给带规程行的条目：排在它前面的条目分到的份额与改前的均分公式逐字相同。"""
    items = _six_entries()
    out = server._format_tool_results(items)
    remaining, left = server._RAW_TOTAL_BUDGET, len(items)
    for n, item in enumerate(items[:4]):
        share = max(remaining // left, 200)
        expect = server._compact_for_context(item["result"], min(server._RAW_ENTRY_BUDGET, share))
        assert json.dumps(_entry(out, n), ensure_ascii=False) == expect
        remaining -= len(expect)
        left -= 1


def test_extra_allowance_is_bounded():
    many = [{"tool": "first_aid_guidance", "result": _first_aid()} for _ in range(8)]
    out = server._format_tool_results(many)
    assert len(out) <= server._RAW_TOTAL_BUDGET * 3 // 2 + 400


def test_entries_are_dropped_before_protocol_lines_are_cut():
    """预算够放下全部规程行时，先丢 `from_sds` 条目与通用话术，规程行一个字都不动。"""
    fa = _first_aid()
    budget = server._protocol_size(fa) + 1200
    out = json.loads(server._compact_for_context(fa, budget))
    protocol = [x for r in out["routes"].values() for x in r if x.startswith("[protocol]")]
    assert len(protocol) == 16 and all(x.endswith(TAIL) for x in protocol)


def test_tight_budget_shortens_protocol_but_keeps_every_route():
    """预算连规程行都放不下时规程行照样挨刀（排序不是豁免），但哪条途径都不整条丢。"""
    out = json.loads(server._compact_for_context(_first_aid(), 2400))
    assert set(out["routes"]) == {"inhalation", "skin", "eye", "ingestion"}
    assert all(r for r in out["routes"].values())


# ── review 打穿过的三处 ──

def _p(tag: str) -> str:
    return f"[protocol] {tag}: seek medical attention " + TAIL


def test_route_nested_beside_a_bare_protocol_line_is_kept():
    """同一列表里既有裸规程行、又有 `{"route": …, "steps": [规程…]}` 时，后者也是规程，不是通用话术。"""
    r = {"chemical": "HF",
         "first_aid": [_p("general"), {"route": "SKINROUTE", "steps": [_p("skin gel")]},
                       "generic H314 " * 30],
         "from_sds": ["filler " * 30 for _ in range(40)]}
    pruned, n = server._drop_generic_beside_protocol(r)
    assert n == 1 and "SKINROUTE" in json.dumps(pruned)
    assert "SKINROUTE" in server._compact_for_context(r, 1500)


def test_many_pinned_entries_still_yield_valid_json_spread_over_the_list():
    """100 个化学品各带规程行：钉住的条目最后也得轮流丢，不能落进字节截断（非法 JSON + 丢尾）。"""
    batch = {"results": [{"chemical": f"chem{i:03d}",
                          "routes": {"skin": [_p(f"c{i} skin " + "y" * 60)] * 3}}
                         for i in range(100)]}
    out = server._format_tool_results([{"tool": "batch", "result": batch}])
    body = json.loads(out.split("`batch`: ", 1)[1])
    names = [x["chemical"] for x in body["results"]]
    assert names[0] == "chem000" and names[-1] == "chem099"


def test_pinning_stays_linear_on_large_lists():
    import time
    rows = [{"id": i, "t": _p(f"p{i}") if i % 2 == 0 else "generic text " * 3}
            for i in range(3000)]
    t = time.perf_counter()
    out = server._compact_for_context({"chemical": "x", "rows": rows}, 4000)
    assert time.perf_counter() - t < 0.5
    json.loads(out)
