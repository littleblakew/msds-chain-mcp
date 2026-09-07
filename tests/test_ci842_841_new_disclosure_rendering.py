"""CI-842 + CI-841：后端新增的两个披露字段必须到达**文本**面。

两条都是同一个形状的第四、第五次复发（CI-553/CI-562 `precursor_disclosure` ·
CI-470/CI-666 `no_hazard_basis` · CI-360 `insufficient_reason`）：后端加了顶层字段、
`_expose()` 免费把它带进 structuredContent，**而模型读的是 `TextContent`，那是各工具
逐字段显式拼出来的** ⇒ 没有渲染器提它的键，在文本面永远不出现
（memory [[fix-never-reaches-the-real-consumer]]）。

- **CI-842**（上游 resolution）：查询里带形态词、命中记录名里没有 ⇒ 发生过一次**替换**。
  批量端点是复数键 `query_form_disclosures`，单物质端点是单数键 `query_form_disclosure`。
- **CI-841**（上游 trust）：一句话问两件事，只跑了一件 ⇒ `unchecked_intents`。
  后端已经确定性拼了一段进 `answer`，**但那防的是后端自己的 summary LLM**；MCP 这条通道上
  还有客户端模型会重写 `answer`（CI-592 的原始事故）⇒ 需要配对指令，与 `unchecked` 同。

🔴 措辞一律后端单源（5 语言 i18n catalog），本仓**不复述、不改写**。所以这些断言打的是
「那句话有没有原样出现」，不是「有没有说某个意思」。

## 记录的变异（🔴 每条都实测过，红的理由要对）

| 变异 | 应该 | 实测 |
|---|---|---|
| `_form_disclosure_lines` 里删掉 `query_form_disclosure` 那两行 | 红 | 红（单物质 6 例全红） |
| 三个批量渲染器里删掉 `_query_form_disclosure_block` 调用 | 红 | 红（对应工具红） |
| `_query_form_disclosure_block` 的 `note` 缺失分支改成 `continue`（静默跳过） | 红 | 红（fallback 用例） |
| `_quick_result` 里删掉 `_unchecked_intents_directive(...)` | 红 | 红 |
| `_unchecked_intents_directive` 把 `isinstance(list)` 三态闸删掉 | 红 | 红（None 用例炸 TypeError） |
| 🔴 **反向**：`query_form_disclosures` 为空时也印抬头 | 红 | 红（`test_silent_when_nothing_to_say`） |
| 🔴 **反向**：指令里加一句 "we have no data, please upload" | 红 | 红（`test_directive_never_asserts_absence`） |
"""
import asyncio

import pytest

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


# ---------------------------------------------------------------------------
# CI-842 —— 逐字抄后端 i18n catalog 的英文那条（`messages.form_disclosure.answered_from_parent`）
# ---------------------------------------------------------------------------
NOTE_EN = (
    "glacial ethanol: you specified a form ('glacial'), and the record we answered from "
    "is for Ethanol — the name WITHOUT that qualifier. Physical form and concentration "
    "can change the hazards (anhydrous HF is far more dangerous than aqueous HF), so "
    "treat this as the parent-compound record, not a confirmed match for your form. "
    "Upload the SDS for that exact form to get an exact answer."
)
ENTRY = {"chemical": "glacial ethanol", "form": "glacial", "cas": "64-17-5",
         "matched": "Ethanol", "note": NOTE_EN}


# --- 批量三工具（复数键）---------------------------------------------------

# 🔴 键名逐条对着渲染器抄（`chem1`/`chem2`、`description`…），不是我按语义猜的：
# 手写替身与生产不同形时，"正文没被挤掉" 这个锚点会变成一句空话
# （[[narrow-hand-rolled-fixtures-and-engine-specific-branches]]）。第一版就是这么错的。
COMPAT = {"pairs": [{"chem1": "glacial ethanol", "chem2": "acetone",
                     "level": "compatible", "reason": "no known reaction",
                     "source": "rule_engine"}],
          "unresolved": [], "documents": [], "query_form_disclosures": [ENTRY]}
RISK = {"warnings": [{"chemical": "glacial ethanol", "cas": "64-17-5",
                      "level": "high", "description": "Highly flammable liquid",
                      "mitigation": "Keep away from ignition sources"}],
        "unresolved": [], "unresolved_detail": [], "documents": [],
        "query_form_disclosures": [ENTRY]}
BATCH = {"compatibility": {"pairs": [], "summary": {}}, "risk_warnings": [],
         "ppe": {}, "unresolved": [], "documents": [],
         "query_form_disclosures": [ENTRY]}

# 🔴 三个全列不抽样：这一族缺陷的形状就是「清单漏了某一个」——
# `precursor_disclosure` 当年正是三个端点里漏了一个才有 CI-562。
_BATCH_CASES = [
    ("check_chemical_compatibility", "_direct_compat", COMPAT,
     (["glacial ethanol", "acetone"],)),
    ("get_chemical_risk_warnings", "_direct_risk", RISK, (["glacial ethanol"],)),
    ("batch_safety_check", "_direct_batch", BATCH,
     (["glacial ethanol", "acetone"],)),
]


@pytest.mark.parametrize("tool_name,patch,payload,args", _BATCH_CASES,
                         ids=[c[0] for c in _BATCH_CASES])
def test_batch_tools_render_query_form_disclosure(tool_name, patch, payload, args):
    """靶心：三个批量工具的文本里必须原样出现后端那句话。"""
    out = _run(getattr(server, tool_name), patch, payload, *args)
    assert NOTE_EN in out, f"{tool_name} 的文本面没有这句披露：\n{out}"
    # 🔴 钉的是**命中记录的名字**（`matched`），不是 CAS：后端那句 i18n 措辞里没有 CAS
    # （与 `no_hazard_basis` 的措辞不同，那条刻意带了）。这里**不许**在 MCP 侧补一个
    # CAS 后缀——措辞 5 语言单源在后端，往一句已本地化的话上贴一段英文片段就是那份
    # 会各自漂移的第二副本。要不要加 CAS 是后端的判断，已在回执里提给 resolution。
    # `matched` 本身就是这条披露的判别事实（"你说 glacial，我们答的是 Ethanol"）。
    assert "Ethanol" in out, out


@pytest.mark.parametrize("tool_name,patch,payload,args", _BATCH_CASES,
                         ids=[c[0] for c in _BATCH_CASES])
def test_silent_when_nothing_to_say(tool_name, patch, payload, args):
    """🔴 反向变异：没有披露时**不许**印抬头。

    假阳性不是噪声——一句「你指定的形态不在记录名里」印在根本没发生替换的答案上，
    是在教用户忽略这条披露。而它是本票唯一的产出。
    """
    clean = {k: v for k, v in payload.items() if k != "query_form_disclosures"}
    out = _run(getattr(server, tool_name), patch, clean, *args)
    assert "is NOT in the name of the record" not in out, out


def test_non_dict_entry_does_not_take_down_the_answer():
    """🔴 一条坏数据不许把用户要的安全答案换成一个工具错误。

    同 `_precursor_disclosure_block` / `_unresolved_block` 的守卫与理由：这个 block
    跑在结果渲染之前，这里抛 AttributeError ＝ 用整份答案换掉一条缺失的披露。
    """
    payload = {**RISK, "query_form_disclosures": ["not a dict", ENTRY]}
    out = _run(server.get_chemical_risk_warnings, "_direct_risk", payload,
               ["glacial ethanol"])
    assert NOTE_EN in out, out
    assert "not a dict" in out, "坏条目要原样报出来，别静默吞掉"
    assert "Highly flammable liquid" in out, "正文被披露挤掉了"


def test_missing_note_still_says_a_substitution_happened():
    """🔴 后端没给 `note` 时**不许静默跳过**——静默正是本票要修的那个 bug 本身。

    触发条件：本仓与后端各自发布，版本会错开（老后端只有 `query_form_match` 的四个
    结构化字段、没有渲染好的 `note`）。
    """
    bare = {k: v for k, v in ENTRY.items() if k != "note"}
    payload = {**RISK, "query_form_disclosures": [bare]}
    out = _run(server.get_chemical_risk_warnings, "_direct_risk", payload,
               ["glacial ethanol"])
    assert "glacial ethanol" in out and "Ethanol" in out and "64-17-5" in out, out
    assert "glacial" in out, out


# --- 单物质工具（单数键，走共用出口 `_form_disclosure_lines`）----------------

_PHYS = "本数据来自乙醇的 SDS。"


def _listed(extra: dict, *, with_phys: bool = False) -> dict:
    item = {"chemical_name": "Ethanol", "cas": "64-17-5",
            "query_form_disclosure": NOTE_EN}
    if with_phys:
        item["physical_form_disclosure"] = _PHYS
    item.update(extra)
    return {"results": [item], "unresolved": []}


_SINGLE_CASES = [
    ("get_ppe_recommendation", "_direct_ppe",
     _listed({"ppe": {"gloves": ["Nitrile"]}, "minimum_ppe_level": 2,
              "signal_word": "Danger", "traceability": "sds_backed"}),
     (["glacial ethanol"],), "Nitrile"),
    ("get_storage_guidance", "_direct_storage",
     _listed({"storage_class_label": "Flammable Liquids", "cabinet_color": "Yellow"}),
     (["glacial ethanol"],), "Flammable Liquids"),
    ("get_exposure_limits", "_direct_exposure",
     _listed({"limits": [{"source": "OSHA", "type": "TWA", "value": 1000,
                          "unit": "ppm"}], "data_source": "msds_parsed"}),
     (["glacial ethanol"],), "OSHA"),
    ("get_transport_classification", "_direct_transport",
     _listed({"un_number": "UN1170", "hazard_class": "3",
              "data_source": "msds_parsed"}),
     (["glacial ethanol"],), "UN1170"),
    ("get_waste_disposal", "_direct_waste",
     _listed({"waste_classification": "Hazardous — flammable solvent"}),
     (["glacial ethanol"],), "Hazardous — flammable solvent"),
]


@pytest.mark.parametrize("tool_name,patch,payload,args,anchor", _SINGLE_CASES,
                         ids=[c[0] for c in _SINGLE_CASES])
def test_single_substance_tools_render_query_form_disclosure(
        tool_name, patch, payload, args, anchor):
    """靶心：单数键走 `_form_disclosure_lines` 这个共用出口 ⇒ 一处改动覆盖全部调用点。

    🔴 全列不抽样，理由同批量那组。`anchor` 钉住正文没有被披露挤掉。
    """
    out = _run(getattr(server, tool_name), patch, payload, *args)
    assert NOTE_EN in out, f"{tool_name}：\n{out}"
    assert anchor in out, f"{tool_name} 的正文没了：\n{out}"


def test_both_form_disclosures_coexist_and_query_form_comes_first():
    """🔴 两条披露**不合并、不二选一**（后端 `query_form_match` 的硬契约）。

    一个由记录驱动（我们手上这份 SDS 是哪个形态），一个由查询与命中名之差驱动
    （发生过一次替换）。两者可以同时为真且不矛盾，合并会丢掉其中一半。
    顺序：身份先于内容——先读到「答的可能不是这个东西」，再读到「这个东西是什么形态」。
    """
    payload = _listed({"ppe": {"gloves": ["Nitrile"]}, "minimum_ppe_level": 2,
                       "signal_word": "Danger", "traceability": "sds_backed"},
                      with_phys=True)
    out = _run(server.get_ppe_recommendation, "_direct_ppe", payload,
               ["glacial ethanol"])
    assert NOTE_EN in out and _PHYS in out, out
    assert out.index(NOTE_EN) < out.index(_PHYS), "身份披露必须排在形态披露之前"


def test_note_is_not_double_bolded():
    """🔴 后端 zh/ja/de/id 的措辞**自带** `**`（加粗的正是否定词）。

    再套一层会拼出 `**A**B**C**`，markdown 客户端于是把两头加粗、把中间那个否定词
    渲染成普通文字 —— 强调恰好反了。这是 `_form_disclosure_lines` 已经记过一次的 bug，
    本条钉住新加的这一路没有重犯。
    """
    zh_note = "glacial ethanol：你指明了一个形态，而我们据以作答的那条记录**不带这个词**。"
    payload = _listed({"ppe": {"gloves": ["Nitrile"]}, "minimum_ppe_level": 2,
                       "signal_word": "Danger", "traceability": "sds_backed"})
    payload["results"][0]["query_form_disclosure"] = zh_note
    out = _run(server.get_ppe_recommendation, "_direct_ppe", payload,
               ["glacial ethanol"])
    assert f"- ⚠️ {zh_note}" in out, out
    assert f"**{zh_note}**" not in out, out


# ---------------------------------------------------------------------------
# CI-841 —— `unchecked_intents` 的配对指令
# ---------------------------------------------------------------------------

def test_directive_present_when_a_part_of_the_question_was_not_run():
    out = server._unchecked_intents_directive(["first_aid_guidance"])
    assert "first_aid_guidance" in out
    assert "NO tool was run" in out
    assert "not checked" in out


@pytest.mark.parametrize("value", [[], None, "first_aid_guidance", 0],
                         ids=["empty-list", "none", "not-a-list", "falsy-non-list"])
def test_three_states_all_render_empty(value):
    """🔴 三态与 `_unchecked_directive` 逐字相同：`[]`＝算过没有、`None`＝这一轮没算、
    键缺失＝老后端。**后两者都不许凭空说一句「可能有没查的」**（没有可靠名单时那是噪声）。
    非 list 的输入不许炸——本仓与后端各自发布。
    """
    assert server._unchecked_intents_directive(value) == ""


def test_directive_never_asserts_absence():
    """🔴 反向变异：措辞红线（CI-243 / CI-322 / CI-334 三次同形事故）。

    「我们没查」不等于「我们没有」。指令里一旦出现「未收录 / 没有数据 / 建议上传」，
    就是在断言一件我们这一轮根本没有验证过的事。
    """
    out = server._unchecked_intents_directive(["first_aid_guidance"])
    lowered = out.lower()
    for banned in ("no data", "not found", "no record", "upload"):
        assert banned not in lowered, f"指令里出现了 {banned!r}：\n{out}"


def test_quick_result_puts_intents_directive_above_the_chemicals_one():
    """🔴 两个指令同时在时的顺序，与后端两个确定性块的相对顺序一致：
    「这一问没查」比「这几个化学品没查」更靠近用户要的那件事。
    """
    res = server._quick_result({
        "answer": "PPE for HF: …",
        "tool_results": [], "documents": [],
        "unchecked": ["acetone"],
        "unchecked_intents": ["first_aid_guidance"],
    })
    text = res.content[0].text
    assert "[unchecked-question]" in text and "[unchecked]" in text
    assert text.index("[unchecked-question]") < text.index("[unchecked]"), text
    assert text.index("[unchecked]") < text.index("PPE for HF"), text


def test_quick_result_carries_the_key_into_structured_content():
    """`_expose` 透传（CI-592）——结构化客户端要能自己区分 `[]` 与 `None`。"""
    res = server._quick_result({
        "answer": "a", "tool_results": [], "documents": [],
        "unchecked_intents": [],
    })
    # 🔴 属性名是 snake_case `structured_content`（pydantic 字段名）。camelCase 那个
    # 会抛 AttributeError 而不是返回 None —— CI-242 记过同一族的 41 处。
    assert res.structured_content["unchecked_intents"] == []
