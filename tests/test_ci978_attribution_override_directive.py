"""CI-978（本仓这一半）：后端刚拼的那条确定性更正，不许在客户端重写中消失。

后端那半的病灶与修法见 msds-chain 的
`backend/tests/test_ci978_unchecked_intent_attribution.py`。本仓只负责一件事：
`answer` 在这条通道上要过**第三个模型**（claude.ai / Copilot 重写之后才到用户眼前），
所以后端的确定性拼串在这里**不是**承重层，得配一条显式指令。

🔴 **每条守卫的变异都实跑过**（2026-09-14，4/4 按预期变红、红的理由也对，跑完还原基线仍绿）：

| 守卫 | 把什么改回去会让它红（逐字） |
|---|---|
| `test_directive_emitted_when_backend_says_it_overrode` ＋ `test_directive_sits_above_everything_else` | `_quick_result` 里去掉 `_attribution_override_directive(...)` 那一项 |
| `test_absent_or_false_emits_nothing` | `overridden is True` → `overridden is not False`（缺席被当成「更正过」⇒ 叫客户端模型去找一段 `answer` 里并不存在的文字） |
| `test_directive_does_not_restate_the_correction_wording` | 往 `_ATTRIBUTION_OVERRIDE_DIRECTIVE` 里抄一份用户可见措辞（那就是第二处拼写，且它只会有英文一种） |
| `test_flag_reaches_structured_content` | 在 `_quick_result` 里把这个键从 `data` 里剔掉（发生率就只剩散文一条通道，而那条通道要过客户端模型） |
"""
import server


def _quick(**extra):
    payload = {"answer": "ANSWER-BODY", "tool_results": [], "documents": []}
    payload.update(extra)
    return server._quick_result(payload).content[0].text


def test_directive_emitted_when_backend_says_it_overrode():
    text = _quick(unchecked_intent_attribution_overridden=True)
    assert "[attribution-correction]" in text


def test_directive_sits_above_everything_else():
    """🔴 位置不是美学：CI-89-followup 在 prod 上实测过靠后的内容会被客户端模型丢掉，
    而这条指令要保护的那段更正就在 `answer` 的开头。
    """
    text = _quick(
        unchecked_intent_attribution_overridden=True,
        unchecked=["acetone"],
        unchecked_intents=["first_aid"],
    )
    assert text.index("[attribution-correction]") < text.index("[unchecked-question]")
    assert text.index("[unchecked-question]") < text.index("[unchecked]")
    assert text.index("[unchecked]") < text.index("ANSWER-BODY")


def test_absent_or_false_emits_nothing():
    """🔴 缺席（老后端）与 `False` 都不发。

    默认发的话，我们会告诉客户端模型「上面有一条更正」，而 `answer` 里并没有
    —— 那是我们自己制造的幻觉源，比少发一条指令坏得多。
    """
    assert "[attribution-correction]" not in _quick()
    assert "[attribution-correction]" not in _quick(
        unchecked_intent_attribution_overridden=False)
    # 非 bool 的脏值（老后端某天发了字符串）同样不发。
    assert "[attribution-correction]" not in _quick(
        unchecked_intent_attribution_overridden="true")


def test_directive_does_not_restate_the_correction_wording():
    """🔴 用户可见的措辞单源在后端（它按 `lang` 本地化）。本仓复述就是第二处拼写，
    而且只会是英文的那一份 —— 中文用户会读到两段口径不同的更正。
    """
    out = server._attribution_override_directive(True)
    for leaked in ("unsourced:", "Do not act on it", "更正", "没有出处"):
        assert leaked not in out, f"指令里复述了用户可见措辞 {leaked!r}：\n{out}"


def test_flag_reaches_structured_content():
    """不经模型改写的消费者要能自己看到这一轮更正过没有（发生率只能从这里量）。"""
    res = server._quick_result({
        "answer": "a", "tool_results": [], "documents": [],
        "unchecked_intent_attribution_overridden": True,
    })
    assert res.structured_content["unchecked_intent_attribution_overridden"] is True
