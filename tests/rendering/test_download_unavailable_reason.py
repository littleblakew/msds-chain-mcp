"""CI-488：没有下载链接时，必须说清楚为什么——不能只是「少了个链接」。

后端（`build_sds_documents`）现在会在每日额度用尽时**保留条目、去掉 URL**，并给出
`document_unavailable_reason`（机器判）和 `_note`（人读）。这一层如果不接，渲染出来
的就是「📄 Original SDS」标题下一行没有链接也没有解释的条目 —— 人和模型都会读成
「这份文件不存在」，正是那两个字段要避免的歧义。

🔴 后端建好模型而渲染层没接，本仓有专门的叫法：「修了，但没到达真正的消费者」。
MCP 是这些端点上唯一带 API key 的真实调用面，所以这一层就是那个消费者。
"""
from server import _format_sds_documents


def _doc(**kw):
    base = {"chemical": "acetone", "chemical_name": "Acetone", "cas": "67-64-1",
            "supplier": "Sigma", "revision_date": "2024-01-01", "record_id": 1}
    base.update(kw)
    return base


def test_url_present_is_rendered_as_before():
    out = _format_sds_documents([_doc(sds_document_url="https://x/msds/token/t")])
    assert "https://x/msds/token/t" in out


def test_quota_block_is_explained_not_silently_linkless():
    out = _format_sds_documents([_doc(
        sds_document_url=None,
        document_unavailable_reason="daily_pdf_quota_reached",
        document_unavailable_note="今日 SDS PDF 下载额度已用尽；这份文件我们有，只是现在取不了。",
    )])
    assert "Acetone" in out, "条目本身不该消失"
    assert "额度" in out, f"没有解释为什么没有链接，读起来就是「这份文件不存在」：{out!r}"


def test_falls_back_to_a_reason_string_when_the_backend_sends_no_note():
    """老后端 / 将来新增 reason：note 缺失时按 reason 兜底，仍要给出一句解释。"""
    out = _format_sds_documents([_doc(
        sds_document_url=None, document_unavailable_reason="insufficient_credits",
    )])
    assert "credit" in out.lower(), out


def test_unknown_reason_does_not_get_a_made_up_explanation():
    """🔴 不认识的 reason 就少说一句，绝不替后端编一个原因。"""
    out = _format_sds_documents([_doc(
        sds_document_url=None, document_unavailable_reason="something_new",
    )])
    assert "Acetone" in out
    assert "quota" not in out.lower() and "credit" not in out.lower(), out
