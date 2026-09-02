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
| `_batch_not_analysed` 改成返回 `[]` | `…names_the_dropped_chemicals` 红 |
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
    assert "including the absence of a warning" in out, (
        "只说了「没全算」，没说清「没有警告也不代表安全」—— 那正是 CI-277 的形状：\n" + out)


def test_it_names_the_dropped_chemicals(monkeypatch):
    """点名，否则用户无从知道是哪几个。"""
    out = _text(_run(_payload(), SUBMITTED, monkeypatch))
    assert "chem13" in out, out
    # 阴性对照：进了分析的那些不该被列成「没分析」
    dropped_section = out.split("were NOT analysed")[-1].split("Call batch_safety_check")[0]
    assert "chem1\n" not in dropped_section and "chem12" not in dropped_section, dropped_section


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
    assert "methanol" in out
    assert "Acetone" not in out.split("were NOT analysed")[-1].split("Call batch")[0]
