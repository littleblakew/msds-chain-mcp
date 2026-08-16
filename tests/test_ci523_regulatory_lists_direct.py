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
    assert "not found in our copy" in out.lower(), out
    assert "not regulated" in out, "缺了那句「绝不是『不受监管』」的否定"


def test_coverage_caveat_also_appears_when_there_ARE_hits():
    """🔴 有命中时更需要这句。

    只在零命中时说「我们的副本不含台湾/IARC」，等于把「查到了 EU 两条」放任
    被读成「其他辖区没事」——而恰恰是有命中的那次，调用方最可能直接照抄结论。
    """
    out = _fmt({"chemical": "benzene", "cas": "71-43-2", "count": 1, "lists": [
        {"list": "EU SVHC Candidate List", "region": "EU"}]}, "benzene")
    assert "Taiwan" in out and "IARC" in out, f"有命中时把覆盖范围说明省掉了：{out!r}"


def test_zh_caller_gets_chinese_prose_not_english():
    """quick-chat 时代 zh 调用方拿到的是中文；换成本地渲染后别把它变回英文。

    这是本次改动**可能引入**的退化，不是既有问题——所以判据打在这一层。
    """
    out = _fmt({"chemical": "苯", "cas": "71-43-2", "count": 0, "lists": []}, "苯", "zh")
    assert "不受监管" in out, f"zh 调用方拿到的是英文：{out!r}"
    assert "not regulated" not in out, out


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


def test_unavailable_result_is_logged_as_a_failure(monkeypatch):
    """🔴 不抛异常的失败也要记成失败——`_log_intent(success=...)` 就是为这类而存在。

    读不到监管清单时工具照常返回一段文本（对调用方是对的），但那不是一次成功的
    核查。装饰器只看得见「抛没抛」，所以工具必须自己降级；否则「数据源读不了」
    在 `mcp_call_logs` 里长得和正常查询一模一样，线上完全看不出来。
    """
    async def _unavailable(*a, **kw):
        return {"chemical": "benzene", "cas": "71-43-2", "count": 0, "lists": [],
                "lists_unavailable": True, "error": "source could not be read"}

    monkeypatch.setattr(server, "_direct_regulatory_lists", _unavailable)
    captured: list = []

    async def _log_call(tool_name, chemicals, ms, success, error_message, *a, **kw):
        captured.append({"success": success, "error_message": error_message})

    monkeypatch.setattr(server, "_log_call", _log_call)
    set_caller_credential("sk-msds-test")
    try:
        res = asyncio.run(server.check_regulatory_lists(chemical="benzene"))
    finally:
        set_caller_credential(None)

    assert "Not checked" in res.content[0].text
    assert captured and captured[0]["success"] is False, captured
    assert captured[0]["error_message"] == "lists_unavailable", captured


def test_keyless_call_is_refused_not_answered_anonymously(monkeypatch):
    """换端点不该顺带改访问控制：老路径（quick-chat）要求凭证，新路径也要。

    匿名放行不只是「少了归属」——按 CI-506，匿名租户路径返回的清单集合本身就不同。
    """
    called: list = []

    async def _should_not_run(*a, **kw):
        called.append(1)
        return {}

    async def _noop_log(*a, **kw):
        return None

    monkeypatch.setattr(server, "_direct_regulatory_lists", _should_not_run)
    monkeypatch.setattr(server, "_log_call", _noop_log)
    set_caller_credential(None)
    res = asyncio.run(server.check_regulatory_lists(chemical="benzene"))
    assert "Authentication required" in res.content[0].text, res
    assert not called, "没有凭证却还是打了后端"


def test_tool_calls_the_deterministic_endpoint_never_quick_chat(sent):
    """🔴 CI-523 的正题。判据打在**实际发出的 URL** 上，不是「代码里有没有那个函数名」。

    只要还打 `/quick-chat`，RAI 分类器就还能整个否掉这次查询——那不是措辞问题，
    是一次合法的安全查询被拒答。
    """
    asyncio.run(server.check_regulatory_lists(chemical="benzene"))
    assert sent, "一个后端请求都没发出去"
    assert all("/quick-chat" not in u for u in sent), f"还在走 quick-chat：{sent}"
    assert any("/api/v2/regulatory-lists" in u for u in sent), sent
