"""CI-1598：老 SDS（修订日早于 GHS 全面实施）的披露必须进**文本**面。

后端在六个单物质端点的条目、`/sds-section`、`/sds-document-url` 上给出
`pre_ghs_disclosure`（会话语言整句，自带 ⚠️ 前缀）。这些工具多数客户端只把 text
喂给模型 ⇒ 渲染器不读这个键就等于没修。
"""
import server

_EN = ("⚠️ The SDS this answer is based on was last revised on 1998-03-01, which may "
       "predate full GHS implementation. Ask the supplier for the current revision.")
_BODY = _EN.removeprefix("⚠️ ")


def test_pre_ghs_line_rendered_once_with_single_warning_sign():
    lines = server._form_disclosure_lines({"pre_ghs": True, "pre_ghs_disclosure": _EN})
    assert lines == [f"- ⚠️ {_BODY}"]


def test_pre_ghs_follows_the_answering_sds_line():
    """它限定的是出处那一行说的那份 SDS ⇒ 紧跟在出处之后。"""
    lines = server._form_disclosure_lines({
        "content_source": {"source": "canonical", "supplier": "Acme", "revision_date": "1998-03-01"},
        "pre_ghs": True, "pre_ghs_disclosure": _EN})
    assert lines == ["- **Answering SDS:** Acme · revision 1998-03-01", f"- ⚠️ {_BODY}"]


def test_sentence_without_prefix_is_kept_whole():
    lines = server._form_disclosure_lines({"pre_ghs_disclosure": "老版本 SDS。"})
    assert lines == ["- ⚠️ 老版本 SDS。"]


def test_none_or_false_means_silence():
    for item in ({"pre_ghs": None, "pre_ghs_disclosure": None},
                 {"pre_ghs": False, "pre_ghs_disclosure": None}, {}):
        assert server._form_disclosure_lines(item) == []
