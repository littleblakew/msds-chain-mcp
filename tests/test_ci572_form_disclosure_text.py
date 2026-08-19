"""CI-572：形态披露必须出现在**六个安全工具**的文本里，不只是 get_sds_section。

后端（本票的 msds-chain 侧）已经把 `physical_form_disclosure` 接进
ppe / storage / exposure / transport / waste / emergency 六个端点。但这六条工具
全是 `structured_output=False`，**多数 MCP 客户端只把 TextContent 喂给模型**
——后端产出了键而渲染器不渲染，用户面等于没修（CI-553/CI-408/CI-360 各栽过一次，
memory `fix-never-reaches-the-real-consumer`）。

判据落在用户/模型真正读到的那串文本上，与 test_ci347 同源。
"""
import asyncio

import pytest

import server

_DISCLOSURE = (
    "以下数据来自氢氟酸（水溶液）的 SDS；无水氟化氢的处置方式不同，我们没有它的数据。"
)


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


def _listed(extra: dict) -> dict:
    """六个端点里的五个都是 `{"results": [...]}`，逐条带自己的形态披露。"""
    item = {"chemical_name": "Hydrofluoric Acid", "cas": "7664-39-3",
            "physical_form": "aqueous_solution",
            "physical_form_disclosure": _DISCLOSURE}
    item.update(extra)
    return {"results": [item], "unresolved": []}


# 🔴 六个工具全列，不抽样：本票的缺陷形状就是「清单漏了某一个」。
# 每条的 `extra` 放该工具真正会渲染的字段，避免渲染出一段只有披露的空壳
# ——那样测到的是「披露在不在」而不是「披露有没有挤掉正文」。
_CASES = [
    ("get_ppe_recommendation", "_direct_ppe",
     _listed({"ppe": {"gloves": ["Nitrile"]}, "minimum_ppe_level": 2,
              "signal_word": "Danger", "traceability": "sds_backed"}),
     (["hydrofluoric acid"],), ("Nitrile",)),   # 🔴 list，与生产同形
    ("get_storage_guidance", "_direct_storage",
     _listed({"storage_class_label": "Corrosive Acids", "cabinet_color": "White"}),
     (["hydrofluoric acid"],), ("Corrosive Acids",)),
    ("get_exposure_limits", "_direct_exposure",
     _listed({"limits": [{"source": "OSHA", "type": "TWA", "value": 3, "unit": "ppm"}],
              "data_source": "msds_parsed"}),
     (["hydrofluoric acid"],), ("OSHA",)),
    ("get_transport_classification", "_direct_transport",
     _listed({"un_number": "UN1790", "hazard_class": "8", "data_source": "msds_parsed"}),
     (["hydrofluoric acid"],), ("UN1790",)),
    ("get_waste_disposal", "_direct_waste",
     _listed({"waste_classification": "acidic_waste", "data_source": "sds_section_13"}),
     (["hydrofluoric acid"],), ("acidic_waste",)),
    ("get_emergency_response", "_direct_emergency",
     {"chemical": "Hydrofluoric Acid", "cas": "7664-39-3", "scenario": "exposure",
      "immediate_actions": ["Rinse with water for 15 minutes"],
      "sds_instructions": [], "hcode_actions": [], "precaution_actions": [],
      "data_source": "sds_parsed", "insufficient_hazard_data": False,
      "physical_form": "aqueous_solution",
      "physical_form_disclosure": _DISCLOSURE},
     ("hydrofluoric acid", "exposure"), ("Rinse with water",)),
]


@pytest.mark.parametrize("tool_name,patch_name,payload,args,must_keep", _CASES,
                         ids=[c[0] for c in _CASES])
def test_form_disclosure_reaches_the_text_the_model_reads(
        tool_name, patch_name, payload, args, must_keep):
    txt = _run(getattr(server, tool_name), patch_name, payload, *args)
    assert _DISCLOSURE in txt, (
        f"{tool_name}：后端说清了这份数据是哪种形态，而 LLM 读到的文本里没有它 —— {txt!r}")
    # 反向守卫的一侧：披露不能挤掉这条工具本来的正文。
    for keep in must_keep:
        assert keep in txt, f"{tool_name}：加了披露之后正文 {keep!r} 不见了 —— {txt!r}"


@pytest.mark.parametrize("tool_name,patch_name,payload,args,must_keep", _CASES,
                         ids=[c[0] for c in _CASES])
def test_no_disclosure_means_silence_not_a_guess(
        tool_name, patch_name, payload, args, must_keep):
    """🔴 反向守卫：`None` ＝ 无话可说（未判定、或我们一份 SDS 都没有），不是
    「只有一种形态」。没有这一条，把渲染写成「总是输出一句形态说明」也能让上面
    那组全绿，而那会**编造**一个我们并不知道的事实。"""
    def _blank(obj):
        obj = dict(obj)
        if "results" in obj:
            obj["results"] = [{**r, "physical_form": None,
                               "physical_form_disclosure": None} for r in obj["results"]]
        else:
            obj["physical_form"] = None
            obj["physical_form_disclosure"] = None
        return obj

    txt = _run(getattr(server, tool_name), patch_name, _blank(payload), *args)
    assert "形态" not in txt
    assert "⚠️" not in txt
    for keep in must_keep:
        assert keep in txt


# ── review 抓到的第七条面：get_sds_document ────────────────────────────────

_DOC_PAYLOAD = {
    "available": True, "record_kind": "substance",
    "chemical_name": "Hydrofluoric Acid", "cas": "7664-39-3",
    "supplier": "Air Liquide USA LLC", "revision_date": "2022-09-01", "region": "US",
    "pdf_url": "https://example.invalid/hf.pdf", "record_id": 1, "pdf_hash": "a" * 64,
    "physical_form": "aqueous_solution", "physical_form_disclosure": _DISCLOSURE,
}


def _run_doc(payload):
    """`get_sds_document` 在打后端之前先过 `_require_api_key`，所以这条路要先给凭证
    （同 test_server_tools.py 的既有写法）——不给的话工具在渲染之前就返回一段
    「需要认证」的文本，断言会在**没有执行到被测代码**的情况下红/绿，两种都是假的。"""
    from request_identity import get_caller_credential, set_caller_credential
    prev = get_caller_credential()
    set_caller_credential("sk-msds-test")
    try:
        return _run(server.get_sds_document, "_direct_sds_document", payload,
                    "hydrofluoric acid")
    finally:
        # 🔴 必须还原：凭证是 contextvar、跨用例可见，不还原会把
        # test_request_identity 的「没有凭证时是什么行为」染绿（实测撞到过）。
        set_caller_credential(prev)


def test_sds_document_renders_the_disclosure_in_text_not_only_structured():
    """后端在 `/sds-document-url` 上**早就**产出这两个键，而这条工具此前只把它们放进
    structuredContent。多数客户端只读 text ⇒ 用户拿到 PDF 链接和出处，却读不到
    「这份是水溶液、无水的我们没有」。（本票 review 抓到的，不是新缺陷是旧缺口。）"""
    res = _run_doc(_DOC_PAYLOAD)
    assert _DISCLOSURE in res
    assert "Signed URL" in res, "披露不能挤掉这条工具本来的正文"


def test_sds_document_stays_silent_without_a_disclosure():
    payload = dict(_DOC_PAYLOAD, physical_form=None, physical_form_disclosure=None)
    res = _run_doc(payload)
    assert "⚠️" not in res or "formulated product" in res
    assert "Signed URL" in res


# ── 🔴 别给整句再套一层加粗：四种语言的这句话自带 `**`，套了会把否定词的强调反过来 ──

def test_the_disclosure_line_does_not_nest_bold_markers():
    """zh/ja/de/id 的披露文案里，加粗的正是否定词（「我们**没有**」/「**keine**」/
    「**tidak**」）。外面再包一层 `**…**` 会拼出 `**A**B**C**`，客户端于是把 A、C
    加粗、把中间那个否定词渲染成普通字 —— 强调恰好反了。英文那条不带 `**`，
    所以这条守卫必须用**带标记的那种**文案，否则它对真实用例免疫。"""
    zh_note = "这份数据描述的是水溶液形态。我们**没有**这个 CAS 无水形态的数据。"
    lines = server._form_disclosure_lines({"physical_form_disclosure": zh_note})
    assert lines == [f"- ⚠️ {zh_note}"]
    assert "**⚠️" not in lines[0]
