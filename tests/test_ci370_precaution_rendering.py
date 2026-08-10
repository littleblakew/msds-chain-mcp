"""CI-370: GHS 官方处置语（P 句）必须出现在**文本**里，不能只躺在 structuredContent。

后端 CI-370 给 `emergency_response` / `storage_guidance` 加了第四种依据 —— GHS 官方
为该危害类别指派的处置语（P 句），每条自带 P 码。可答率因此从 15.5% 升到 79.2%
（exposure 场景，Prod 全量 72,426 条）。

**但这一面此前只渲染旧键** ⇒ `precaution_actions` / `precaution_conditions` 只活在
`structuredContent` 里，而本文件（见 CI-360 那份注释）已经写明「多数 MCP 客户端只把
text 喂给模型」。后果：后端报 `data_source: ghs_precautionary`、
`insufficient_hazard_data: false`（声称有依据），而真实 MCP 客户端看到的文本里
**零条可见指引** —— 正是 CI-360/CI-243 要消灭的形状，只是挪到了渲染层。
也意味着这一整票的收益，唯一深度活跃的真实 MCP 用户根本吃不到。

🔴 第二个判据（比「有没有渲染」更重要）：**出处必须在文本里说清**。
P 句是 GHS 对**这一类危害**的标准处置语，不是这份 SDS 的正文。两者混在一起渲染，
模型会把通用处置语当成「这份 SDS 这么说」——那会侵蚀本产品唯一的对外主张（可追溯）。
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


PRECAUTION_ONLY_EMERGENCY = {
    "chemical": "Some reagent", "cas": "1234-56-7", "scenario": "exposure",
    "signal_word": "Warning",
    "immediate_actions": ["Remove victim from contaminated area"],
    "sds_instructions": [],
    "hcode_actions": [],
    "precaution_actions": [
        "[P302+P352] IF ON SKIN: Wash with plenty of water and soap",
        "[P332+P313] If skin irritation occurs: Get medical advice/attention",
    ],
    "data_source": "ghs_precautionary",
    "insufficient_hazard_data": False,
}

NO_PRECAUTION_EMERGENCY = dict(PRECAUTION_ONLY_EMERGENCY, precaution_actions=[])

PRECAUTION_STORAGE = {"results": [{
    "chemical_name": "Some reagent", "cas": "1234-56-7",
    "storage_class_label": "General Chemicals", "cabinet_color": "White",
    "recommended_cabinet": "General storage", "temperature_requirement": "Ambient",
    "storage_requirements": ["Keep container closed"],
    "incompatible_materials": [],
    "precaution_conditions": [
        "[P403+P233] Store in a well-ventilated place. Keep container tightly closed",
        "[P405] Store locked up",
    ],
    "nfpa_ratings": {},
    "data_source": "classification",
}], "unresolved": []}

NO_PRECAUTION_STORAGE = {"results": [
    dict(PRECAUTION_STORAGE["results"][0], precaution_conditions=[])], "unresolved": []}


# ── emergency ────────────────────────────────────────────────────────────
def test_emergency_renders_precaution_statements_in_text():
    out = _run(server.get_emergency_response, "_direct_emergency",
               PRECAUTION_ONLY_EMERGENCY, "some reagent", "exposure")
    assert "[P302+P352]" in out, out
    assert "wash with plenty of water" in out.lower(), out
    assert "[P332+P313]" in out, out


def test_emergency_labels_the_precaution_provenance():
    """🔴 出处要在文本里说清：这是 GHS 对这类危害的标准处置语，不是这份 SDS 的正文。

    ⚠️ 本条第一版是**假绿**：只断言 `"ghs" in text`，而文本里本来就有一行
    `*Data source: ghs_precautionary*` —— 与出处标注毫无关系却让它通过。
    现在断言的是**独立的段标题**，且必须与 SDS 段的标题不同。
    """
    out = _run(server.get_emergency_response, "_direct_emergency",
               PRECAUTION_ONLY_EMERGENCY, "some reagent", "exposure")
    heading = "**GHS Standard Precautions"
    assert heading in out, out
    # 标题必须出现在 data_source 那一行**之前**（是段落标题，不是脚注的一部分）
    assert out.index(heading) < out.index("Data source:"), out
    # 且必须与「SDS 专属指引」是两个不同的段
    assert "SDS-Specific Instructions" not in out.split(heading)[1], out


def test_emergency_omits_the_section_when_there_are_no_precautions():
    """空段不渲染 —— 空标题比没有更糟（读起来像我们查过但什么都没有）。"""
    out = _run(server.get_emergency_response, "_direct_emergency",
               NO_PRECAUTION_EMERGENCY, "some reagent", "exposure")
    assert "[P" not in out, out
    assert "GHS Standard" not in out, out


# ── storage ──────────────────────────────────────────────────────────────
def test_storage_renders_precaution_conditions_in_text():
    out = _run(server.get_storage_guidance, "_direct_storage",
               PRECAUTION_STORAGE, ["some reagent"])
    assert "[P403+P233]" in out, out
    assert "well-ventilated" in out.lower(), out
    assert "[P405]" in out, out


def test_storage_omits_the_section_when_there_are_no_precautions():
    out = _run(server.get_storage_guidance, "_direct_storage",
               NO_PRECAUTION_STORAGE, ["some reagent"])
    assert "[P" not in out, out


# ── 反向守卫：旧键的渲染不许被挤掉 ───────────────────────────────────────
def test_existing_sections_still_render():
    out = _run(server.get_emergency_response, "_direct_emergency",
               dict(PRECAUTION_ONLY_EMERGENCY,
                    sds_instructions=["Flush with water for 15 minutes"],
                    hcode_actions=["[H314] Do NOT neutralize"]),
               "some reagent", "exposure")
    assert "Flush with water for 15 minutes" in out
    assert "[H314] Do NOT neutralize" in out
    assert "[P302+P352]" in out
    assert "Remove victim from contaminated area" in out
