"""CI-360: 「判不了」必须出现在**文本**里，不能只躺在 structuredContent 里。

后端（CI-243）在「记录在库、但该记录没有危害数据」时返回
`insufficient_hazard_data: true` + `insufficient_reason`。这一面此前只渲染
`*Data source: none*` —— 多数 MCP 客户端只把 text 喂给模型，于是：

  · `get_emergency_response`：几段全空 + 一行 `Data source: none`。`none` 不在任何
    既有枚举里，模型多半读成「来源未知但建议有效」，比修之前的 `hcode_mapping` 更含混。
  · `get_waste_disposal`：**自相矛盾**——同一段里既有
    「Waste classification: general_chemical_waste」（读起来像一个具体结论，实际是
    后端的兜底桶），又有「Data source: none」（说没有依据）。

🔴 判据落在**用户/模型真正会读到的那串文本**上，不是 `structuredContent`——
后者一直是对的，问题从来不在那儿。
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


INSUFFICIENT_EMERGENCY = {
    "chemical": "Hydrochloric acid", "cas": "7647-01-0", "scenario": "spill",
    "signal_word": "",
    # 与化学品无关的通用动作 —— 这部分**必须继续渲染**（见下面的反向守卫）
    "immediate_actions": ["Alert nearby personnel", "Refer to SDS Section 6"],
    "sds_instructions": [], "hcode_actions": [],
    "data_source": "none",
    "insufficient_hazard_data": True,
    "insufficient_code": "insufficient_hazard_data",
    "insufficient_reason": "这份记录没有解析出该场景的处置信息，也没有可据以推导的 H 码映射。",
}

INSUFFICIENT_WASTE = {"results": [{
    "chemical_name": "Hydrochloric acid", "cas": "7647-01-0",
    "waste_classification": "general_chemical_waste",
    "waste_categories": ["general_chemical_waste"],
    "sds_section_13": None,
    "data_source": "none",
    "insufficient_hazard_data": True,
    "insufficient_code": "insufficient_hazard_data",
    "insufficient_reason": "这份记录没有解析出废弃处置信息，也没有可据以分类的 H 码映射。",
}], "unresolved": []}


def test_emergency_says_it_cannot_determine():
    out = _run(server.get_emergency_response, "_direct_emergency",
               INSUFFICIENT_EMERGENCY, "hydrochloric acid", "spill")
    assert "CANNOT BE DETERMINED" in out
    assert "insufficient_reason" not in out, "别把键名漏给用户"
    assert "没有解析出该场景的处置信息" in out, "后端给的原因必须出现在文本里"
    assert "NOT a low-hazard finding" in out


def test_emergency_still_shows_chemical_agnostic_actions():
    """🔴 反向守卫：通用动作（呼叫急救 / 参照 SDS 第 6 节）与化学品无关，永远成立。

    「判不了」不等于「什么都别说」——扣留这半在 82% 的记录上会变成系统性失声
    （后端侧同一判据见 msds-chain 的 prompts.py 与 quick_engine 短路分支）。
    """
    out = _run(server.get_emergency_response, "_direct_emergency",
               INSUFFICIENT_EMERGENCY, "hydrochloric acid", "spill")
    assert "Alert nearby personnel" in out
    assert "Refer to SDS Section 6" in out


def test_waste_does_not_render_the_fallback_bucket_as_a_conclusion():
    out = _run(server.get_waste_disposal, "_direct_waste",
               INSUFFICIENT_WASTE, ["hydrochloric acid"])
    assert "CANNOT BE DETERMINED" in out
    assert "general_chemical_waste" not in out, (
        "无依据时把兜底桶当分类结论渲染，与同段的 Data source: none 直接打架"
    )
    assert "没有解析出废弃处置信息" in out


def test_sufficient_data_is_unchanged():
    """反向：有依据时逐字保持原样，别把契约做成「永远判不了」。"""
    ok = {"results": [{
        "chemical_name": "Acetone", "cas": "67-64-1",
        "waste_classification": "flammable_waste",
        "waste_categories": ["flammable_waste"],
        "sds_section_13": "Dispose via licensed contractor.",
        "data_source": "h_code_classification",
        "insufficient_hazard_data": False,
    }], "unresolved": []}
    out = _run(server.get_waste_disposal, "_direct_waste", ok, ["acetone"])
    assert "flammable_waste" in out
    assert "CANNOT BE DETERMINED" not in out
    assert "Dispose via licensed contractor" in out


def test_structured_content_still_carries_the_raw_contract():
    """文本变了，机读面不许跟着变——两个面服务不同消费者。"""
    async def _fake(*_a, **_k):
        return INSUFFICIENT_WASTE
    orig = server._direct_waste
    server._direct_waste = _fake
    try:
        res = asyncio.run(server.get_waste_disposal(["hydrochloric acid"]))
    finally:
        server._direct_waste = orig
    item = res.structuredContent["results"][0]
    assert item["insufficient_hazard_data"] is True
    assert item["waste_classification"] == "general_chemical_waste", (
        "structuredContent 是原始契约，不该被展示层的取舍改写"
    )
