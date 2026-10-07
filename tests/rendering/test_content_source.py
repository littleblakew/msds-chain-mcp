"""CI-938：作答那一行的出处（`content_source`）必须出现在六个单物质工具的文本里。

后端在 ppe / storage / exposure / transport / waste 的 `results[]` 条目上、emergency 的
顶层给出 `{source, supplier, revision_date, msds_version}` 或 `null`。canonical 不覆盖的
CAS 现在会从 PubChem 的 GHS 分类汇总作答 ⇒ 文本面不说出处，模型会把它引成供应商 SDS。
"""
import pytest

import server
from tests.rendering.test_concentration_disclosure import _CASES, _run

_PUBCHEM = {"source": "pubchem_ghs", "supplier": "", "revision_date": "", "msds_version": ""}
_SUPPLIER = {"source": "system_library", "supplier": "Sigma-Aldrich",
             "revision_date": "2024-03-01", "msds_version": "6.2"}


def _with_source(payload: dict, cs) -> dict:
    payload = dict(payload)
    if "results" in payload:
        payload["results"] = [{**r, "concentration_disclosure": None, "content_source": cs}
                              for r in payload["results"]]
    else:
        payload.update(concentration_disclosure=None, content_source=cs)
    return payload


_IDS = [c[0] for c in _CASES]


@pytest.mark.parametrize("tool_name,patch_name,payload,args,body", _CASES, ids=_IDS)
def test_pubchem_answer_says_it_is_not_a_supplier_sds(
        tool_name, patch_name, payload, args, body):
    txt = _run(getattr(server, tool_name), patch_name,
               _with_source(payload, _PUBCHEM), *args)
    assert "not a supplier SDS" in txt, f"{tool_name}：PubChem 作答没说出处 —— {txt!r}"
    assert body in txt
    assert txt.index("not a supplier SDS") < txt.index(body)


@pytest.mark.parametrize("tool_name,patch_name,payload,args,body", _CASES, ids=_IDS)
def test_supplier_answer_names_supplier_and_revision(
        tool_name, patch_name, payload, args, body):
    txt = _run(getattr(server, tool_name), patch_name,
               _with_source(payload, _SUPPLIER), *args)
    assert "**Answering SDS:** Sigma-Aldrich · revision 2024-03-01" in txt, txt
    assert "PubChem" not in txt
    assert body in txt


@pytest.mark.parametrize("cs", [None, {**_SUPPLIER, "supplier": ""}],
                         ids=["null", "no-supplier"])
@pytest.mark.parametrize("tool_name,patch_name,payload,args,body", _CASES, ids=_IDS)
def test_nothing_to_name_means_silence(tool_name, patch_name, payload, args, body, cs):
    """🔴 反向：没有作答行 / 没有供应商名 ⇒ 不编一句（绝不写 "unknown supplier"）。"""
    txt = _run(getattr(server, tool_name), patch_name,
               _with_source(payload, cs), *args)
    assert "Answering" not in txt
    assert "unknown supplier" not in txt.lower()
    assert body in txt



@pytest.mark.parametrize("source", ["some_new_feed", None])
def test_unrecognised_source_is_not_presented_as_a_supplier_sds(source):
    """反向：认不出的 source 没有供应商名时不许静默（会被当成供应商 SDS 引用）。"""
    lines = server._content_source_lines(
        {"content_source": {"source": source, "supplier": "", "revision_date": ""}})
    assert len(lines) == 1 and "not identified as a supplier SDS" in lines[0]
    assert "Answering SDS" not in lines[0]
