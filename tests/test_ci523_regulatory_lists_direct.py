"""CI-523：监管清单查询不再经过 LLM，而且后端的两条显式披露要真的渲染出来。

Prod 实测的症状有两个，形状不同：
  ① 同一入参五次调用返回 174/722/2588/2592/2599 字符，其中 174 那次原文是
     「I'm sorry, I can't assist with that request…」——查苯上了哪些监管清单，
     被 RAI 分类器判成非化学品请求。
  ② 答案由 summary LLM 复述 ⇒ 后端早就建好的 CI-507（`lists_unavailable`）与
     CI-375（未解析说明）被复述掉。

所以这里钉两件事：**这个工具不许再打 quick-chat**（否则 ① 会回来），以及**三个分支
各自的措辞**（否则 ② 会回来）。判据打在渲染输出上——那就是调用方真正读到的东西。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential

_fmt = server._format_regulatory_lists


def test_hits_are_listed_with_their_region():
    out = _fmt({"chemical": "benzene", "cas": "71-43-2", "count": 2, "lists": [
        {"list": "California Proposition 65", "source": "regulatory_prop65", "region": "US"},
        {"list": "EU SVHC Candidate List", "source": "hardcoded", "region": "EU"},
    ]}, "benzene")
    assert "71-43-2" in out
    assert "California Proposition 65" in out and "(US)" in out
    assert "EU SVHC Candidate List" in out and "(EU)" in out


def test_unreadable_source_says_not_checked_not_empty():
    """🔴 本文件最要紧的一条：读不到清单 ≠ 不在任何清单上。

    反向变异：把渲染里的 `lists_unavailable` 分支删掉，这条必红（会掉进
    「Matching lists: 0」那句，正是 CI-507 要防的读法）。
    """
    out = _fmt({
        "chemical": "benzene", "cas": "71-43-2", "count": 0, "lists": [],
        "lists_unavailable": True,
        "error": "The regulatory list source could not be read, so no check was "
                 "performed. This is NOT the same as the chemical being on no list.",
    }, "benzene")
    assert "Not checked" in out, out
    assert "NOT the same" in out, f"后端的披露没被渲染出来：{out!r}"
    assert "Matching lists" not in out, f"读不到数据源却报出了清单数：{out!r}"


def test_zero_hits_is_worded_as_not_found_in_our_copy():
    out = _fmt({"chemical": "acetone", "cas": "67-64-1", "count": 0, "lists": []}, "acetone")
    assert "not found in our" in out.lower(), out
    assert "not regulated" in out, "缺了那句「绝不是『不受监管』」的否定"


def test_unresolved_explains_which_kind_of_missing():
    out = _fmt({
        "chemical": "固体氢氧化钾", "cas": None, "count": 0, "lists": [],
        "unresolved_kind": "form_not_recognized",
        "error": "We have 氢氧化钾 in the database but could not confirm it is the same substance.",
        "near_matches": ["氢氧化钾"],
    }, "固体氢氧化钾")
    assert "could not confirm" in out, out
    assert "氢氧化钾" in out, "近似命中没渲染出来，调用方就只能死在这里"


class _Resp:
    status_code = 200
    headers: dict[str, str] = {}

    def raise_for_status(self): ...

    def json(self):
        return {"chemical": "benzene", "cas": "71-43-2", "count": 0, "lists": []}


class _CapturingClient:
    def __init__(self, sent: list):
        self._sent = sent

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        self._sent.append(url)
        return _Resp()


@pytest.fixture
def sent(monkeypatch):
    box: list = []
    monkeypatch.setattr(server.httpx, "AsyncClient", _CapturingClient(box))
    set_caller_credential("sk-msds-test")
    yield box
    set_caller_credential(None)


def test_tool_calls_the_deterministic_endpoint_never_quick_chat(sent):
    """🔴 CI-523 的正题。判据打在**实际发出的 URL** 上，不是「代码里有没有那个函数名」。

    只要还打 `/quick-chat`，RAI 分类器就还能整个否掉这次查询——那不是措辞问题，
    是一次合法的安全查询被拒答。
    """
    asyncio.run(server.check_regulatory_lists(chemical="benzene"))
    assert sent, "一个后端请求都没发出去"
    assert all("/quick-chat" not in u for u in sent), f"还在走 quick-chat：{sent}"
    assert any("/api/v2/regulatory-lists" in u for u in sent), sent
