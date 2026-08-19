"""CI-567 / CI-568：应急文本面必须渲染**披露**与**物质级规程**。

## 为什么这一面单独要一份守卫

后端 2026-08-18 上 Prod 的 CI-550/551 已经把物质级 HF 规程与 `provenance_note`
算了出来，但 Prod 实调复现的仍是修之前的答案。两层原因，本文件管第二层：

1. 后端两个面的返回字典都没把披露键抄出去（CI-568，已在后端修）；
2. **即便抄了，这条渲染函数是逐键取值的** —— 只读 TextContent 的客户端照样看不到
   （CI-553 刚在管制前体披露上栽过同一形状，CI-360 的注释更早就写明了
   「多数 MCP 客户端只把 text 喂给模型」）。

## 物质级那半（CI-567）

HF 的「立刻涂 2.5% 葡萄糖酸钙、每 10–15 分钟复涂」此前混在 `immediate_actions`
里、没有任何标记，而通用 `[Hxxx]` 行数量多、篇幅大 ⇒ 实测模型拿通用行的数字把它
改写掉（「冲 5 分钟」→「15 分钟」），并把「立刻涂」降级成「告知医护人员以便他们
提供」= **延迟解毒**，正是 CI-550 要消灭的那个临床错误。
⇒ 单独成段 + 段标题里写死「通用指引不覆盖这些」。
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


# 🔴 前缀是契约的一部分：后端用 `[protocol]` 标记物质级步骤（与 `[Hxxx]`、
# `[glove-compat]` 同构）。本面按前缀分段，不依赖后端多返回一个复制原文的字段。
HF_LINE = ("[protocol] HF-SPECIFIC — SKIN: apply 2.5% calcium gluconate gel to the affected skin "
           "immediately and keep massaging it in.")

HF_EMERGENCY = {
    "chemical": "Hydrofluoric acid", "cas": "7664-39-3", "scenario": "exposure",
    "signal_word": "Danger",
    "provenance_note": ("PROVENANCE: not everything here is text from the cited SDS. "
                        "Items in `from_hcodes` are standard GHS hazard-code guidance."),
    "protocol_citation": "standard HF first-aid — US university EHS / NIH",
    "critical_antidote": True,
    "immediate_actions": [HF_LINE, "Call poison control or emergency services"],
    "sds_instructions": [],
    "hcode_actions": ["[H310] Wash skin with soap and water for 15 minutes"],
    "precaution_actions": [],
    "data_source": "critical_antidote",
    "insufficient_hazard_data": False,
}

# 对照：没有物质级规程、也没有披露的普通记录。
PLAIN_EMERGENCY = {
    "chemical": "Some reagent", "cas": "1234-56-7", "scenario": "exposure",
    "signal_word": "Warning",
    "immediate_actions": ["Remove victim from contaminated area"],
    "sds_instructions": [], "hcode_actions": [], "precaution_actions": [],
    "data_source": "hcode_mapping", "insufficient_hazard_data": False,
}


def test_provenance_note_is_rendered_in_text():
    """CI-568：只放 structuredContent 等于没修 —— 只读文本的客户端是主要消费者。"""
    out = _run(server.get_emergency_response, "_direct_emergency",
               HF_EMERGENCY, "hydrofluoric acid", "exposure")
    assert "not everything here is text from the cited SDS" in out, out


def test_priority_steps_render_as_their_own_section_before_the_generic_ones():
    """CI-567：物质级那条要单独成段，且排在通用 H 码段**之前**。

    顺序不是排版口味：读的人按顺序执行，把最关键一步排到后面就是延迟解毒。
    """
    out = _run(server.get_emergency_response, "_direct_emergency",
               HF_EMERGENCY, "hydrofluoric acid", "exposure")
    assert "Substance-specific protocol" in out, out
    assert "calcium gluconate" in out, out
    assert out.index("Substance-specific protocol") < out.index("[H310]"), out


def test_the_section_heading_forbids_substituting_generic_guidance():
    """🔴 段落存在还不够：标题必须写明通用指引**不覆盖**这几条。

    事故里模型正是拿 `[H310]` 的「15 分钟」换掉了物质级的时长。只把两段分开、
    不说清谁压谁，等于把裁决权留给模型 —— 实测它裁反了。
    """
    out = _run(server.get_emergency_response, "_direct_emergency",
               HF_EMERGENCY, "hydrofluoric acid", "exposure")
    heading = out.split("Substance-specific protocol")[1].split("\n")[0].lower()
    assert "not override" in heading or "does not override" in heading, heading
    assert "clinician" in heading, (
        "「交给医护去做」是实测复现过的那种降级，标题要点名禁止它", heading)


def test_priority_line_is_not_repeated_in_immediate_actions():
    """同一句话渲染两遍会稀释它 —— CI-553 折叠重复披露的同一教训。"""
    out = _run(server.get_emergency_response, "_direct_emergency",
               HF_EMERGENCY, "hydrofluoric acid", "exposure")
    assert out.count("calcium gluconate gel") == 1, out


def test_immediate_actions_survive_when_priority_steps_take_one(): 
    """🔴 反向守卫：去重不能把其余的通用动作一起吃掉。

    `immediate_actions` 里那些与化学品无关的动作（呼叫急救、把 SDS 交给医护）
    对站在现场的人仍然有用 —— 这正是 CI-243 契约里「别going silent」那一条。
    """
    out = _run(server.get_emergency_response, "_direct_emergency",
               HF_EMERGENCY, "hydrofluoric acid", "exposure")
    assert "Call poison control" in out, out


def test_plain_record_renders_neither_section():
    """空段比没有更糟：读起来像我们查过、结果什么都没有。"""
    out = _run(server.get_emergency_response, "_direct_emergency",
               PLAIN_EMERGENCY, "some reagent", "exposure")
    assert "Substance-specific protocol" not in out, out
    assert "Provenance:" not in out, out
    assert "Remove victim" in out, "普通记录的既有渲染不能被本票改坏"
