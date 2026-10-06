"""CI-570：`batch_safety_check` 收 20 个、后端只算前 12 个，被丢掉的那些必须说出来。

与 CI-277 同形：**没有结论被读成没有危害**。所以判据不是「有没有提一句截断」，而是
「**下面的一切、包括没有警告这件事，都不涉及这几个**」这层意思有没有到达用户，
并且**被丢掉的名字要点出来**——否则用户无从知道是哪几个。

🔴 **这条 bug 之所以活了这么久，是因为提示其实早就写好了，只是挂错了地方**：
它原本在 `_precursor_disclosure_block` 里，而那个函数开头是 `if not entries: return []`
⇒ 只有「这批里恰好含受管制前体」时才渲染。CI-553 的两条测试正好都用带前体的载荷，
于是**测试是绿的、功能是坏的**——普通批次（绝大多数）全程沉默。
⇒ 下面第一条用**零前体**载荷，就是为了走那条从没被覆盖过的路。

## 变异

| 变异 | 应当 |
|---|---|
| 把 `_batch_truncation_block` 的调用搬回 `_precursor_disclosure_block` 里（＝退回事故代码） | `…without_precursor_disclosure` 红 |
| `_batch_not_analysed` 改成返回 `[]` | `…names_the_dropped_chemicals` 红。🔴 **初版这条记录是假的**：断言写成 `"chem13" in out`，而抬头那行会把**每个提交的名字**都念一遍 ⇒ 恒真，改成返回 `[]` 它照样绿。异构 review 实测抓到，已改成只断言「被丢掉的那一节」 |
| 折回按小写比较 | `…case_differing_duplicate…` 红 |
| 把作用域写回「nothing below says anything」 | `…does_not_swallow_the_precursor_notice` 红 |
| 只丢掉 1 个时把建议写回「再调一次 batch」 | `…single_dropped_chemical…` 红 |
| 删掉 structured 里的 `not_analysed` | `…structured_face_also_says_it` 红 |
| 把 `_resolve_all` 的 key 换成规范名（后端侧，本仓测不到） | **测不出** —— 见 `_batch_not_analysed` docstring 里写死的那条前提 |
"""
import asyncio

import pytest

import server as _s
from request_identity import set_caller_credential

# 13 个输入、后端只回前 12 个 —— 与 Prod 的 MAX_BATCH_CHEMICALS=12 一致。
SUBMITTED = [f"chem{i}" for i in range(1, 14)]
ANALYSED = SUBMITTED[:12]


def _payload(**extra) -> dict:
    data = {
        "chemicals": [{"name": n, "cas": f"0000-00-{i}", "resolved": True}
                      for i, n in enumerate(ANALYSED)],
        "compatibility": {"summary": {"total": 66}, "pairs": []},
        "risk_warnings": [],
        "documents": [],
        "unresolved": [],
        "truncated": True,
    }
    data.update(extra)
    return data


def _run(payload, submitted, monkeypatch):
    async def fake(*a, **kw):
        return payload
    monkeypatch.setattr(_s, "_direct_batch", fake)
    monkeypatch.setattr(_s, "_log_call", lambda *a, **kw: asyncio.sleep(0))
    set_caller_credential("sk-msds-test")
    try:
        return asyncio.run(_s.batch_safety_check(chemicals=submitted))
    finally:
        set_caller_credential(None)


def _text(result) -> str:
    if isinstance(result, str):
        return result
    return "\n".join(b.text for b in result.content if getattr(b, "text", None))


def test_truncation_is_reported_without_precursor_disclosure(monkeypatch):
    """🔴 零前体载荷 —— 正是 CI-553 的测试从没走过、而线上绝大多数调用走的那条路。"""
    out = _text(_run(_payload(), SUBMITTED, monkeypatch))
    assert "not every submitted chemical was analysed" in out, out
    assert "absence of a warning there says nothing about these" in out, (
        "只说了「没全算」，没说清「没有警告也不代表安全」—— 那正是 CI-277 的形状：\n" + out)


def _dropped_section(out: str) -> str:
    """只取「被丢掉的那一节」。

    🔴 **别对整段输出做 `in` 断言**（review 抓到的）：抬头那行
    `**Chemicals (13):** chem1, …, chem13` 会把**每一个提交的名字**都念一遍
    ⇒ `assert "chem13" in out` 恒真，把 `_batch_not_analysed` 改成返回 `[]` 它也绿。
    判据要打在真正决胜的那一段上。
    """
    assert "were NOT analysed" in out, f"根本没渲染截断块：\n{out}"
    return out.split("were NOT analysed")[-1].split("get_chemical_risk_warnings")[0] \
              .split("Call batch_safety_check")[0]


def test_it_names_the_dropped_chemicals(monkeypatch):
    """点名，否则用户无从知道是哪几个。"""
    out = _text(_run(_payload(), SUBMITTED, monkeypatch))
    section = _dropped_section(out)
    assert "chem13" in section, section
    # 阴性对照：进了分析的那些不该被列成「没分析」
    assert "chem12" not in section, section


def test_structured_face_also_says_it(monkeypatch):
    """🔴 claude.ai 连接器只拿 structuredContent ⇒ 只改文本面等于没修到那批用户。"""
    result = _run(_payload(), SUBMITTED, monkeypatch)
    sc = getattr(result, "structured_content", None) or {}
    assert sc.get("truncated") is True, sc.keys()
    assert sc.get("not_analysed") == ["chem13"], sc.get("not_analysed")
    # 🔴 这一条是为什么必须有 not_analysed：`chemicals` 回的是提交的全部 13 个
    assert len(sc.get("chemicals") or []) == 13, "抬头字段仍是提交清单（本身没错，但会让人以为都算过）"


def test_untruncated_batch_says_nothing(monkeypatch):
    """阴性对照：没截断时一个字都不该多说。"""
    payload = _payload()
    payload.pop("truncated")
    out = _text(_run(payload, ANALYSED, monkeypatch))
    assert "not every submitted chemical was analysed" not in out, out
    assert "absence of a warning" not in out, out


def test_caller_string_is_what_gets_matched(monkeypatch):
    """差集按调用方原样字符串比 —— 大小写/空格不该造出假的「没分析」。"""
    payload = _payload(chemicals=[{"name": "Acetone", "cas": "67-64-1", "resolved": True}])
    out = _text(_run(payload, ["  Acetone  ", "methanol"], monkeypatch))
    section = _dropped_section(out)
    assert "methanol" in section, section
    assert "Acetone" not in section, section


def test_the_claim_does_not_swallow_the_precursor_notice(monkeypatch):
    """🔴 受管制前体披露是**在截断之前**算的（后端有意），它**能**点到被丢掉的那个。

    所以断言不能笼统说「下面的一切都不涉及它们」—— 那会让读者把两行之下那条真正
    适用的披露也当成不适用，比不提示更糟。这条是 review 抓到的回归。
    """
    payload = _payload(precursor_disclosure=[{"name": "chem13", "statement": "…"}])
    out = _text(_run(payload, SUBMITTED, monkeypatch))
    assert "nothing below says anything" not in out, (
        "作用域又写回「下面的一切」了：\n" + out)
    assert "compatibility and risk results below" in out, out
    assert "computed on everything you submitted" in out, (
        "有前体披露时必须说明它覆盖的是全部提交，否则与上面那句冲突：\n" + out)


def test_single_dropped_chemical_gets_an_instruction_that_works(monkeypatch):
    """本工具拒收 <2 个输入 ⇒ 只丢掉 1 个时叫人「拿这些再调一次」是保证被拒的建议。"""
    out = _text(_run(_payload(), SUBMITTED, monkeypatch))
    assert "get_chemical_risk_warnings" in out, out
    assert "Call batch_safety_check again with just those" not in out, out


def test_case_differing_duplicate_is_still_reported_as_dropped(monkeypatch):
    """🔴 后端 `name_to_cas` 保留原样大小写 ⇒ `Acetone` 与 `acetone` 是两个 key。

    这边若折叠成小写比较，被丢掉的那个会被当成已入账 ⇒ `truncated=true` 配
    `not_analysed=[]` —— 正是本票要防的那句「我们什么都没丢」。
    """
    payload = _payload(chemicals=[{"name": "Acetone", "cas": "67-64-1", "resolved": True}])
    result = _run(payload, ["Acetone", "acetone"], monkeypatch)
    sc = getattr(result, "structured_content", None) or {}
    assert sc.get("not_analysed") == ["acetone"], sc.get("not_analysed")


def test_empty_difference_under_truncation_is_loud(monkeypatch):
    """差集为空 + `truncated` ⇒ 是我们这边算漏了，不是「什么都没丢」。含糊过去的话，
    `_batch_not_analysed` 那条跨仓前提失效时看起来一切正常。"""
    payload = _payload(chemicals=[{"name": n, "cas": "x", "resolved": True} for n in SUBMITTED])
    out = _text(_run(payload, SUBMITTED, monkeypatch))
    assert "defect on our side" in out, out
    assert "unknown subset" in out, out
