"""CI-848：`compare_sds_versions` 的两个已弃用参数必须被**显式披露**，不能静默丢弃。

背景：`version_old`/`version_new` 2026-06-08 随「改走 /api/v2 直连」被删，
但外部客户端拿的是**工具面快照**——2026-09-03 实测 ChatGPT 应用目录条目至今仍列着它们
（那份快照停在 2026-05-22~06-08）。旧客户端照旧传，pydantic 对多余入参**静默丢弃**：
用户要「比较 v3 和 v5」，拿到「最近两版」的对比，**一份看起来完全正常的答案**。
这发生在外部调用量第二高的工具上（169 次）。

⇒ 参数收回来当已弃用接住，只为把**静默损失**换成**显式披露**。

🔴 **两个面都要验**：`content[0].text`（模型读的）和 `structuredContent`
（claude.ai 连接器**只拿得到这一面**）。只验其中一面的话，另一面的用户什么也看不到，
而测试照样绿——本仓 [[fix-never-reaches-the-real-consumer]] 那个形状。

🔬 **变异（2026-09-04 实跑，四个，结果照抄实测）**：
- 删 `lines.append(f"\\n{note}")`（has_newer 分支的文本披露）⇒ **只红 1 条**，正是那一支的
  文本用例。**两个分支的文本是两处独立写入点**，所以一处坏掉不会连累另一处——粒度对了。
- 删 `text = f"{text}\\n\\n{note}"`（no-newer 分支的文本披露）⇒ 只红另外那一条。
- 删两处 `payload = {**payload, ...}` ⇒ 红 3 条**全是 structured 面**，文本面仍绿
  ⇒ 两个面确实是分开验的，不是一条断言顺带覆盖了两处。
- 阴性对照见 `test_no_note_when_not_supplied`（下方）。
- 🔴 **阴性对照**：不传这两个参数时**一个字都不许多**——`test_no_note_when_not_supplied`。
  没有它，一条「无条件加披露」的实现会让上面全部变绿，而每个正常调用都被塞进一句
  「你要的没做到」，那是比原问题更坏的输出。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential

_PAIR = {"chemical": "hydrogen peroxide", "cas": "7722-84-1", "has_newer": True,
         "from_version": "2019-03-01", "to_version": "2024-11-02",
         "hazard_changes": [{"added": ["H271"], "removed": []}], "verdict_relevant": True}
_NO_NEWER = {"chemical": "acetone", "cas": "67-64-1", "has_newer": False}


@pytest.fixture(autouse=True)
def _credential():
    set_caller_credential("sk-msds-test")
    yield
    set_caller_credential(None)


@pytest.fixture
def call(monkeypatch):
    def _run(payload, **kwargs):
        async def _fake(*a, **kw):
            return payload
        monkeypatch.setattr(server, "_direct_compare_sds", _fake)
        res = asyncio.run(server.compare_sds_versions("x", **kwargs))
        return res.content[0].text, (res.structured_content or {})
    return _run


@pytest.mark.parametrize("payload,expect_actual", [
    (_PAIR, "2019-03-01 → 2024-11-02"),
    (_NO_NEWER, "no comparison was made"),
])
def test_text_face_says_it_was_not_honoured_and_what_was_done(call, payload, expect_actual):
    """披露必须两句都在：①没按你要求做 ②实际做了什么。只说「已忽略」会被读成「结果仍是你要的」。"""
    text, _ = call(payload, version_old="2019-03-01", version_new="2021-01-01")
    assert "no longer supported" in text and "ignored" in text, text
    assert expect_actual in text, text


@pytest.mark.parametrize("payload", [_PAIR, _NO_NEWER])
def test_structured_face_carries_the_same_disclosure(call, payload):
    """claude.ai 连接器只拿 structuredContent ⇒ 只放在文本面等于对那批用户没修。"""
    _, sc = call(payload, version_old="a", version_new="b")
    assert sc["deprecated_parameters_ignored"] == ["version_old", "version_new"]
    assert "no longer supported" in sc["deprecated_parameters_note"]


def test_only_the_supplied_one_is_named(call):
    """只传一个就只点名一个——名单是从入参推出来的，不是写死的两条。"""
    text, sc = call(_PAIR, version_old="2019-03-01")
    assert sc["deprecated_parameters_ignored"] == ["version_old"]
    assert "version_new" not in text


@pytest.mark.parametrize("payload", [_PAIR, _NO_NEWER])
def test_no_note_when_not_supplied(call, payload):
    """🔴 阴性对照：正常调用一个字都不许多。无条件加披露＝每次都告诉用户「你要的没做到」。"""
    text, sc = call(payload)
    assert "no longer supported" not in text, text
    assert "deprecated_parameters_ignored" not in sc
    assert "deprecated_parameters_note" not in sc
