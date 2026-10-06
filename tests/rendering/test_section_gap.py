"""CI-408: 「这一节我们没有正文」必须出现在**文本**里，不能只躺在 structuredContent 里。

后端给 `/api/v2/sds-section` 加了 `no_section_text` / `no_section_text_reason` /
`no_section_text_note`，把「为什么没有正文」显式建模成三种原因。但 `get_sds_section`
是 `structured_output=False` 的工具 —— **LLM 读的是 TextContent**，而这一面此前无论
哪种原因都回同一句 "No data available for this section in the canonical SDS."。

⇒ 后端那个修复对模型来说等于不存在。review 抓到时的原话：「唯一的外部消费者拿到的
文本回复一个字都没变」。这与 [[CI-360]] 是同一形态（判不了只写进 structuredContent），
所以判据也照那份写：**落在用户/模型真正会读到的那串文本上**。

🔴 为什么这件事值得一条测试：本项目栽过——「空」在下游被读成「无危害」，
40% HF 的储存建议因此掉进普通柜（memory `feedback-safety-fix-made-it-worse`）。
一句无区分度的 "No data available" 与「这一节没有该类危害」在模型眼里是一回事。
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


# 整份文档一段都没解析出来 —— Prod 上零分段是大头路径，不是边角情形
NO_SECTIONS_PARSED = {
    "chemical": "Sodium hypochlorite solution", "cas": "7681-52-9",
    "content": None,
    "no_section_text": True,
    "no_section_text_reason": "no_sections_parsed",
    "no_section_text_note": (
        "我们持有这份 SDS，但没有从中抽取到任何分段正文 —— 这是数据缺口，"
        "不是「这一节没有危害」。"
    ),
    "data_source": "canonical_sections",
    "supplier": "Acme Chemicals", "revision_date": "2023-04-01", "region": "US",
}


def test_the_backend_s_reason_reaches_the_text_the_model_reads():
    txt = _run(server.get_sds_section, "_direct_sds_section",
               NO_SECTIONS_PARSED, "Sodium hypochlorite solution", 5)

    # 🔴 断言打在**内容**上：模型必须读到后端给的那句解释，而不是通用兜底。
    assert NO_SECTIONS_PARSED["no_section_text_note"] in txt, (
        "后端解释了为什么没有正文，但 LLM 读到的文本里没有它 —— "
        f"实际文本：{txt!r}"
    )
    assert "No data available for this section" not in txt, (
        "无区分度的通用兜底仍然出现了 —— 它与「这一节没有该类危害」在模型眼里无异"
    )


def test_the_generic_fallback_survives_when_the_backend_says_nothing():
    """反向守卫：后端**没给** note 时（旧后端 / 别的分支），不能什么都不说。

    没有这一条，把渲染改成「只在有 note 时才输出」也能让上面那条通过，
    而那会让旧后端的响应在这一段变成**一片空白** —— 比通用文案更糟。
    """
    payload = {k: v for k, v in NO_SECTIONS_PARSED.items()
               if not k.startswith("no_section_text")}
    txt = _run(server.get_sds_section, "_direct_sds_section",
               payload, "Sodium hypochlorite solution", 5)

    assert "No data available for this section" in txt


def test_real_content_is_still_rendered_unchanged():
    """反向守卫：有正文时这段逻辑不能被碰到。"""
    payload = dict(NO_SECTIONS_PARSED)
    payload["content"] = "SECTION 5: Fire-fighting measures\nUse water spray."
    payload["no_section_text"] = False
    txt = _run(server.get_sds_section, "_direct_sds_section",
               payload, "Sodium hypochlorite solution", 5)

    assert "Use water spray." in txt
    assert payload["no_section_text_note"] not in txt, (
        "有正文却还把「没有正文」的说明也渲染出来了"
    )
