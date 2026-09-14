r"""CI-977（本仓这一半）：把「这一轮我们给了什么」当成一条独立事实发出去。

后端那半（表、列、取值白名单）在 msds-chain 的
`backend/tests/test_ci977_response_kind.py`。本仓负责**产生**这个值。

病灶：2026-09-07 一个真实外部用户问「请提供乙酸乙酯的 16 章节 SDS」——产品的核心用例——
被 RAI 分类器整条拒了，而那条调用记的是 `success=true`（传输层确实成功）。
⇒ 误伤方向**不在任何失败率里**。

🔴 **判据从后端载荷的 `intent` 推导，不去认回复正文**：拒答文案是 5 语种一张表，
按文本认它就是「自由文本判据」——加一门语言、改一个字都会静默失配，
而失配的方向是「看起来没有误伤」。

| 守卫 | 把什么改回去会让它红（逐字） |
|---|---|
| `test_rejected_intent_becomes_rejected_kind` | `_RESPONSE_KIND_BY_INTENT` 里删掉 `"rejected"` 那一项 |
| `test_redirected_intent_becomes_redirected_kind` | 同上，删 `"redirected"` |
| `test_any_other_intent_is_answered` | 把 `.get(intent, "answered")` 的默认值去掉（真答了的回合会落 NULL ⇒ 分母塌掉） |
| `test_missing_intent_is_none_not_answered` | `return None` 改成 `return "answered"`（把「没算」写成一句肯定） |
| `test_ask_chemical_safety_reports_the_kind` | `ask_chemical_safety` 的 `_log_intent(...)` 去掉 `response_kind=` 那个实参 |
| `test_log_call_puts_it_on_the_wire` | `_log_call` 的 POST body 里删掉 `"response_kind"` 那一行 |
"""
import asyncio
import json

import pytest

import server


def test_rejected_intent_becomes_rejected_kind():
    assert server._response_kind({"intent": "rejected"}) == "rejected"


def test_redirected_intent_becomes_redirected_kind():
    assert server._response_kind({"intent": "redirected"}) == "redirected"


@pytest.mark.parametrize("intent", ["risk", "compatibility", "ppe", "grounded_only"])
def test_any_other_intent_is_answered(intent):
    """🔴 后端给了 intent ⇒ 这一轮**确实**产出了内容，必须记成 `answered`。

    落 NULL 的话，读取侧算拒答率时分母只剩拒答那一类 —— 一个 100% 的拒答率，
    而且它和「真的全被拒了」完全同形。
    """
    assert server._response_kind({"intent": intent}) == "answered"


@pytest.mark.parametrize("data", [None, {}, {"intent": ""}, {"intent": 7}, "not a dict"])
def test_missing_intent_is_none_not_answered(data):
    """没算 ⇒ `None`。兜底成 `answered` 就是把「不知道」写成一句肯定。"""
    assert server._response_kind(data) is None


@pytest.mark.asyncio
async def test_ask_chemical_safety_reports_the_kind(monkeypatch):
    """打在**工具真实的 finally 路径**上，不是直接调 `_response_kind`。

    只测那个纯函数的话，「函数对了但没接上去」会全程绿 —— 而那正是这类改动最常见的
    失败方式（同族：字段传得进去、返回 201、然后被完全丢掉）。
    """
    captured: dict = {}

    async def fake_quick_chat(question, lang=None):
        return {"intent": "rejected", "answer": "抱歉，这个请求我不能协助。",
                "tool_results": [], "documents": []}

    def fake_log_intent(tool_name, chemicals, input_params=None, **kw):
        captured.update({"tool_name": tool_name, **kw})

    monkeypatch.setattr(server, "_quick_chat", fake_quick_chat)
    monkeypatch.setattr(server, "_log_intent", fake_log_intent)

    await server.ask_chemical_safety(question="给我乙酸乙酯的 16 章节 SDS")

    assert captured["tool_name"] == "ask_chemical_safety"
    assert captured["response_kind"] == "rejected"
    # 🔴 `success` 不跟着翻 —— 一次拒答在传输层是成功的。这是本票的设计，不是遗漏。
    assert captured["success"] is True


@pytest.mark.asyncio
async def test_log_call_puts_it_on_the_wire(monkeypatch):
    """最后一跳：它得真的进 POST body。前面每一步都对、这一行漏了，同样什么都不会红。"""
    sent: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: _Client())
    await server._log_call("ask_chemical_safety", None, 12, True,
                           response_kind="rejected")
    assert sent["response_kind"] == "rejected"
    assert sent["success"] is True
