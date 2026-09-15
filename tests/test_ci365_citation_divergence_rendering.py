"""CI-365：附的这份文件不是结论所引用的那一份时，必须在渲染层说出来。

后端现在让附件跟着「结论的依据」走；取不到被引用那行的原件时（Prod 实测 ≈0.05%）
它退回文档侧选择，并发下 `document_follows_citation=False` + `citation_divergence_note`。

**那一句不渲染，降级就是静默的**——用户看到标题「📄 Original SDS (click to verify)」
下的一个链接，会拿它去核对结论里那句「依据 X 家 SDS 第 7 节」，而它出自另一份文件。
🔴 **比不给链接更坏**：不给链接只是缺一步，给错链接是替一份没参与结论的文件背书。

同一个函数里，这是「修了但没到达真正的消费者」的第二次（第一次是 CI-488）。
"""
from server import _format_sds_documents


def _doc(**kw):
    base = {"chemical": "acetone", "chemical_name": "Acetone", "cas": "67-64-1",
            "supplier": "Sigma", "revision_date": "2024-01-01", "record_id": 1,
            "sds_document_url": "https://x/msds/token/t"}
    base.update(kw)
    return base


def test_divergent_document_is_flagged_in_the_rendered_output():
    """决胜条件＝`document_follows_citation is False` 那一支。

    反向变异：删掉这一支 ⇒ 本条红（链接照给，而「这不是依据」那句消失）。
    """
    out = _format_sds_documents([_doc(
        document_follows_citation=False,
        citation_divergence_note="这份可下载的 SDS 不是本次结论所引用的那一份。",
    )])
    assert "https://x/msds/token/t" in out, "仍要给用户一个可用的文件"
    assert "不是本次结论所引用的那一份" in out, \
        f"降级必须说出来，否则用户会拿它去核对一句它证明不了的话：{out!r}"


def test_a_document_that_does_follow_the_citation_gets_no_warning():
    """「不该红」的一侧：附件就是被引用那一行时，不许挂警告。"""
    out = _format_sds_documents([_doc(document_follows_citation=True)])
    assert "⚠️" not in out, out


def test_absent_citation_is_not_treated_as_divergence():
    """🔴 决胜条件＝**三态**，这是本文件最容易写错的一条。

    `document_follows_citation` 缺失或 `None` ＝「这条回答没有引用」——绝大多数结果都是
    这样（规则层定的级，没读过任何 SDS）。写成 `if not doc.get(...)` 会把它们一起命中，
    于是**每一条普通结果后面都挂一句「这不是依据」**：把一句重要的警告变成噪声，
    人和模型都会学会忽略它，而那正是它唯一的作用。

    反向变异：把判断改成 `if not doc.get("document_follows_citation")` ⇒ 本条红。
    """
    assert "⚠️" not in _format_sds_documents([_doc()])                          # 缺失
    assert "⚠️" not in _format_sds_documents([_doc(document_follows_citation=None)])


def test_falls_back_to_a_generic_warning_when_the_backend_sends_no_note():
    """后端只发了标志没发文案时，仍要给出一句警告——不能因为没文案就沉默。"""
    out = _format_sds_documents([_doc(document_follows_citation=False)])
    assert "⚠️" in out and "cited" in out.lower(), out
