"""CI-615：稀释制剂披露必须出现在**用户读到的文本**里，不只是 structuredContent。

后端（msds-chain 侧）算出 `preparation_disclosure` 之后，**这一层漏抄就等于没修**：
这些工具都是 `structured_output=False`，多数 MCP 客户端只把 TextContent 喂给模型；
而 `get_sds_document` 的 `structured_content` 是一份**手写白名单**，后端新增的键不会
自己出现在里面。

🔴 本票的原始复现就是走 MCP 打 `get_sds_document("7732-18-5")`（水 → Cambridge Isotope
的 TMSP，成分段 `pct_by_weight: 0.03`）——第一版只改了后端，复审实测：那条复现
**一个字都看不到**。同族：CI-553 / CI-408 / CI-360 / CI-572，
memory `fix-never-reaches-the-real-consumer`。
"""
import asyncio

import pytest

import server

_PREP = ("⚠️ 这条答案依据的那份 SDS 描述的是**含该物质 0.03%（按重量）的制剂**，不是纯物质。"
         "纯品的危害、接触限值与操作要求可能严重得多。请以你手上那批物料的 SDS 为准。")
_FORM = "以下数据来自氢氟酸（水溶液）的 SDS；无水氟化氢的处置方式不同，我们没有它的数据。"


def _run(tool, patch_name, payload, *args):
    async def _fake(*_a, **_k):
        return payload
    orig = getattr(server, patch_name)
    setattr(server, patch_name, _fake)
    try:
        res = asyncio.run(tool(*args))
        return res
    finally:
        setattr(server, patch_name, orig)


def _text(res):
    return res.content[0].text if hasattr(res, "content") else res


def _run_doc(payload):
    """🔴 `get_sds_document` 在打后端之前先过 `_require_api_key`：不给凭证的话工具
    **在渲染之前**就返回一段「需要认证」的文本 ⇒ 断言会在没执行到被测代码的情况下
    红/绿，两种都是假的（实测：第一版就是这么红的）。写法同 test_ci572。

    凭证是 contextvar、跨用例可见 ⇒ **必须还原**，否则会把
    「没有凭证时是什么行为」那些用例染绿。
    """
    from request_identity import get_caller_credential, set_caller_credential
    prev = get_caller_credential()
    set_caller_credential("sk-msds-test")
    try:
        return _run(server.get_sds_document, "_direct_sds_document", payload, "7732-18-5")
    finally:
        set_caller_credential(prev)


def test_the_original_repro_says_it_in_the_text():
    """🔴 `get_sds_document` —— 本票原始复现打的就是这个工具。"""
    payload = {
        "available": True, "record_kind": "substance", "chemical_name": "TMSP",
        "cas": "7732-18-5", "supplier": "Cambridge Isotope Laboratories, Inc.",
        "revision_date": "2023-05-19", "region": "US", "record_id": 25395,
        "pdf_hash": "a" * 64, "pdf_url": "https://example/x", "expires_in_seconds": 300,
        "physical_form": None, "physical_form_disclosure": None,
        "preparation_percent": 0.03, "preparation_disclosure": _PREP,
    }
    res = _run_doc(payload)

    assert "0.03%" in _text(res), (
        f"用户读到的文本里没有这句披露——后端产出 ≠ 用户看见：{_text(res)[:300]}"
    )
    # structuredContent 那份手写白名单也要带上（机器消费者按它分支）
    sc = getattr(res, "structured_content", None) or {}
    assert sc.get("preparation_percent") == 0.03, f"白名单漏抄：{sorted(sc)}"
    assert sc.get("preparation_disclosure")


def test_it_is_rendered_before_the_form_disclosure():
    """两条披露同时在场时，「这根本不是纯物质」排在「是哪一种形态」**之前**。

    🔴 顺序不是排版偏好：多数客户端按序截断（本仓 600 字符那处），排在后面的会被切掉，
    而「这份 SDS 描述的不是纯物质」是更靠前的一层否定。
    """
    payload = {
        "available": True, "record_kind": "substance", "chemical_name": "TMSP",
        "cas": "7732-18-5", "supplier": "CIL", "revision_date": "2023-05-19",
        "region": "US", "record_id": 1, "pdf_hash": "b" * 64,
        "pdf_url": "https://example/x", "expires_in_seconds": 300,
        "physical_form": "aqueous_solution", "physical_form_disclosure": _FORM,
        "preparation_percent": 0.03, "preparation_disclosure": _PREP,
    }
    text = _text(_run_doc(payload))

    assert "0.03%" in text and _FORM in text, "两条披露必须都在"
    assert text.index("0.03%") < text.index(_FORM), (
        "稀释制剂那句被排到形态披露之后了——按序截断时它会先被切掉"
    )


def test_silence_when_the_sheet_says_nothing():
    """🔴 反方向：没有这个键时一个字都不许多说（`None` ＝ 这份 SDS 没声明，
    **不是**「就是纯的」，更不能因此编一句）。"""
    payload = {
        "available": True, "record_kind": "substance", "chemical_name": "Water",
        "cas": "7732-18-5", "supplier": "Sigma", "revision_date": "2024-01-01",
        "region": "US", "record_id": 2, "pdf_hash": "c" * 64,
        "pdf_url": "https://example/x", "expires_in_seconds": 300,
        "physical_form": None, "physical_form_disclosure": None,
        "preparation_percent": None, "preparation_disclosure": None,
    }
    text = _text(_run_doc(payload))

    assert "制剂" not in text and "preparation" not in text.lower()


@pytest.mark.parametrize("tool_name,patch,payload_extra", [
    ("get_ppe_recommendation", "_direct_ppe", {"ppe": {"gloves": ["Nitrile"]},
                                               "minimum_ppe_level": 2,
                                               "signal_word": "Danger"}),
    ("get_storage_guidance", "_direct_storage", {"storage_class_label": "General",
                                                 "cabinet_color": "Grey"}),
])
def test_the_listed_tools_render_it_too(tool_name, patch, payload_extra):
    """稀释制剂披露与形态披露共用 `_form_disclosure_lines` ⇒ 那六个清单型工具一并覆盖。

    🔴 只测 `get_sds_document` 会让「共用出口」这个前提无人守——出口一旦被谁改成
    只渲染形态，这些工具会静默恢复沉默。
    """
    item = {"chemical_name": "TMSP", "cas": "7732-18-5",
            "physical_form": None, "physical_form_disclosure": None,
            "preparation_percent": 0.03, "preparation_disclosure": _PREP}
    item.update(payload_extra)
    text = _text(_run(getattr(server, tool_name), patch,
                      {"results": [item], "unresolved": []}, ["water"]))

    assert "0.03%" in text, f"{tool_name} 的文本里没有披露：{text[:200]}"
