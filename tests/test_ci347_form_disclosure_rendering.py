"""CI-347: 形态披露必须出现在**文本**里，不能只躺在 structuredContent 里。

同一个 CAS 可以是两种形态——无水氟化氢（剧毒气体）vs 氢氟酸水溶液——
**储存容器、泄漏处置、急救都不同**。后端把「这份数据是哪种形态 + 另一种我们没有」
建模成 `physical_form_disclosure`，但 `get_sds_section` 是 `structured_output=False`，
**LLM 读的是 TextContent**。不接进来，后端那个修复对模型就等于不存在。

🔴 这是今天第三次撞上同一形态（前两次：CI-408 的 no_section_text_note、
CI-429 的 REST 面）——「修了，但没到达真正的消费者」。所以这条测试判据落在
用户/模型真正读到的那串文本上，与 test_ci360/test_ci408 同源。
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


_DISCLOSURE = (
    "以下数据来自氢氟酸（水溶液）的 SDS；无水氟化氢的处置方式不同，我们没有它的数据。"
)

AQUEOUS_HF = {
    "chemical": "Hydrofluoric Acid", "cas": "7664-39-3",
    "content": "SECTION 5: Fire-fighting measures\nUse water spray for cooling.",
    "supplier": "Air Liquide USA LLC", "revision_date": "2022-09-01", "region": "US",
    "data_source": "canonical_sections",
    "physical_form": "aqueous_solution",
    "physical_form_disclosure": _DISCLOSURE,
}


def test_the_form_disclosure_reaches_the_text_the_model_reads():
    txt = _run(server.get_sds_section, "_direct_sds_section", AQUEOUS_HF,
               "hydrofluoric acid", 5)

    assert _DISCLOSURE in txt, (
        "后端说清了这份数据是哪种形态、另一种没有，而 LLM 读到的文本里没有它 —— "
        f"实际文本：{txt!r}"
    )


def test_undetermined_form_says_nothing_rather_than_guessing():
    """🔴 反向守卫：`None` ＝ 未判定，不是「只有一种形态」。

    没有这一条，把渲染写成「总是输出一句形态说明」也能让上面那条通过，
    而那会**编造**一个我们并不知道的事实——正是这条线（CI-347/CI-375）在防的。
    """
    payload = dict(AQUEOUS_HF)
    payload["physical_form"] = None
    payload["physical_form_disclosure"] = None
    txt = _run(server.get_sds_section, "_direct_sds_section", payload,
               "hydrofluoric acid", 5)

    assert "形态" not in txt and "form" not in txt.lower().replace("information", "")
    # 正文与出处仍然照常给出——「不说形态」不等于「什么都不说」
    assert "Use water spray" in txt
    assert "Air Liquide" in txt


def test_content_and_source_still_render_with_the_disclosure():
    """反向守卫的另一侧：加了披露不能挤掉正文或出处。"""
    txt = _run(server.get_sds_section, "_direct_sds_section", AQUEOUS_HF,
               "hydrofluoric acid", 5)
    assert "Use water spray" in txt
    assert "Air Liquide" in txt
    assert "2022-09-01" in txt
