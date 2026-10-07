"""CI-237-a：浓度披露必须出现在**六个单物质安全工具**的文本里。

后端在 ppe / storage / exposure / transport / waste 的 `results[]` 条目上、
emergency 的顶层，给出已渲染好的 `concentration_disclosure`：用户名字里写了浓度
（`50% ethanol`），而作答那份 SDS 标的是别的浓度或没标。这六条工具全是
`structured_output=False`，多数 MCP 客户端只把 TextContent 喂给模型 ⇒ 渲染器
不读这个键，用户面就等于没修（同 CI-572 / CI-615 的形状）。

判据落在模型真正读到的那串文本上；顺序判据落在「披露排在危害正文之前」。
"""
import asyncio

import pytest

import server

_CONC = ("Ethanol: you asked about 50%, and the SDS we answered from (Ethanol absolute, "
         "CAS 64-17-5) declares 99.8%. Hazards can change with concentration, so check "
         "that this sheet matches what you actually hold.")


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
    item = {"chemical_name": "Ethanol", "cas": "64-17-5",
            "concentration_disclosure": _CONC}
    item.update(extra)
    return {"results": [item], "unresolved": []}


# 🔴 六个全列，不抽样：这类缺陷的形状就是「某一个工具漏了」。
# `body` 是该工具本来的正文里的一个串，用来判顺序（披露在前）和「没挤掉正文」。
_CASES = [
    ("get_ppe_recommendation", "_direct_ppe",
     _listed({"ppe": {"gloves": ["Nitrile"]}, "minimum_ppe_level": 2,
              "signal_word": "Danger", "traceability": "sds_backed"}),
     (["50% ethanol"],), "Signal word"),
    ("get_storage_guidance", "_direct_storage",
     _listed({"storage_class_label": "Flammable Liquids", "cabinet_color": "Yellow"}),
     (["50% ethanol"],), "Flammable Liquids"),
    ("get_exposure_limits", "_direct_exposure",
     _listed({"limits": [{"source": "OSHA", "type": "TWA", "value": 1000, "unit": "ppm"}],
              "data_source": "msds_parsed"}),
     (["50% ethanol"],), "OSHA"),
    ("get_transport_classification", "_direct_transport",
     _listed({"un_number": "UN1170", "hazard_class": "3", "data_source": "msds_parsed"}),
     (["50% ethanol"],), "UN1170"),
    ("get_waste_disposal", "_direct_waste",
     _listed({"waste_classification": "flammable_waste", "data_source": "sds_section_13"}),
     (["50% ethanol"],), "flammable_waste"),
    ("get_emergency_response", "_direct_emergency",
     {"chemical": "Ethanol", "cas": "64-17-5", "scenario": "exposure",
      "immediate_actions": ["Rinse with water for 15 minutes"],
      "sds_instructions": [], "hcode_actions": [], "precaution_actions": [],
      "data_source": "sds_parsed", "insufficient_hazard_data": False,
      "signal_word": "Danger", "concentration_disclosure": _CONC},
     ("50% ethanol", "exposure"), "Rinse with water"),
]


@pytest.mark.parametrize("tool_name,patch_name,payload,args,body", _CASES,
                         ids=[c[0] for c in _CASES])
def test_concentration_disclosure_reaches_the_text_before_the_body(
        tool_name, patch_name, payload, args, body):
    txt = _run(getattr(server, tool_name), patch_name, payload, *args)
    assert _CONC in txt, f"{tool_name}：后端给了浓度披露，模型读到的文本里没有 —— {txt!r}"
    assert body in txt, f"{tool_name}：加了披露之后正文 {body!r} 不见了 —— {txt!r}"
    assert txt.index(_CONC) < txt.index(body), (
        f"{tool_name}：浓度披露排到了正文之后——按序截断时它会先被切掉")


@pytest.mark.parametrize("tool_name,patch_name,payload,args,body", _CASES,
                         ids=[c[0] for c in _CASES])
def test_no_disclosure_means_silence(tool_name, patch_name, payload, args, body):
    """🔴 反向：键缺席或为 `None` ＝ 浓度对得上或用户没写浓度，不许编一句。"""
    payload = dict(payload)
    if "results" in payload:
        payload["results"] = [{**r, "concentration_disclosure": None}
                              for r in payload["results"]]
    else:
        payload["concentration_disclosure"] = None
    txt = _run(getattr(server, tool_name), patch_name, payload, *args)
    assert "concentration" not in txt.lower()
    assert "⚠️" not in txt
    assert body in txt


def test_order_matches_the_backend_whitelist():
    """后端 `direct_service` 把三条披露按 query_form → concentration → preparation
    排列；渲染沿用同一顺序（先「答的是不是这个东西」，再「浓度对不对」，再「是不是制剂」）。"""
    lines = server._form_disclosure_lines({
        "query_form_disclosure": "Q", "concentration_disclosure": "C",
        "preparation_disclosure": "P", "physical_form_disclosure": "F"})
    assert lines == ["- ⚠️ Q", "- ⚠️ C", "- ⚠️ P", "- ⚠️ F"]
