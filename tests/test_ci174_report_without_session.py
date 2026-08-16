"""CI-174：报告不能只在「你手上已经有 session_id」时才拿得到。

Prod 实测（2026-08-16）：`batch_safety_check` **自它诞生那天（2026-04-20）起**就在输出尾部
写着「要签名 PDF 报告就调 `create_audit_session`」——外部 60 次调用、6 个用户、
**转化 0 人 0 次**。所以缺的不是又一句提示，是**那一步本身**：拿报告要先有一个 session_id，
而用户手上从来没有。

改法：`get_audit_report()` 可以不带参数，用后端记着的「这个人最近分析过什么」把 session
建出来。判据钉三件事——不带参数时**真的**建了并出了报告 · 没有可报告的东西时**不建空 session**
· 带 session_id 的老路径**一步都没变**（老客户端不能因为这次改动而行为改变）。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.headers: dict[str, str] = {}

    def raise_for_status(self): ...

    def json(self):
        return self._p


class _FakeClient:
    """按 URL 回不同 payload，并把请求过的 URL 记下来。"""

    def __init__(self, sent, recent):
        self._sent = sent
        self._recent = recent

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        self._sent.append(url)
        if "/api/v2/recent-chemicals" in url:
            return _Resp(self._recent)
        if "/report/signed-url" in url:
            return _Resp({"url": "/reports/signed/abc"})
        return _Resp({})

    async def post(self, url, json=None, headers=None, **kw):
        self._sent.append(url)
        if url.endswith("/sessions"):
            return _Resp({"session_id": "DEMO-CI174TST"})
        if url.endswith("/chemicals"):
            return _Resp({"added": [{"name": "acetone", "status": "added"}], "not_found": []})
        if url.endswith("/compatibility"):
            return _Resp({"matrix": [], "warnings": []})
        return _Resp({})


@pytest.fixture
def wired(monkeypatch):
    """returns a factory: 给定 recent-chemicals 的返回，装好 client 并返回已发 URL 列表。"""
    def _make(recent):
        sent: list = []
        monkeypatch.setattr(server.httpx, "AsyncClient", _FakeClient(sent, recent))
        monkeypatch.setattr(server, "_log_call", _noop_log)
        set_caller_credential("sk-msds-test")
        return sent
    yield _make
    set_caller_credential(None)


async def _noop_log(*a, **kw):
    return None


def test_no_argument_call_builds_the_report_from_recent_analyses(wired):
    sent = wired({"chemicals": ["acetone", "methanol"], "days": 7, "calls": 4})

    res = asyncio.run(server.get_audit_report())
    text = res.content[0].text

    assert "/reports/signed/abc" in text, text
    assert any("/api/v2/recent-chemicals" in u for u in sent), sent
    assert any(u.endswith("/sessions") for u in sent), "没有真的把 session 建出来"
    # 🔴 报告必须自报覆盖范围：用户没说过范围，文档就得自己说清楚它涵盖了什么
    assert "acetone" in text and "methanol" in text, text
    assert res.structured_content["built_from_recent_analyses"] is True


def test_no_recent_analyses_does_not_create_an_empty_session(wired):
    """没东西可报时给一句能照做的话，**且不建 session**。

    反向变异：把空列表那段 early-return 删掉，本条必红（会去建一个空 session，
    用户拿到一份什么都没有的签名 PDF——比拿不到更糟）。
    """
    sent = wired({"chemicals": [], "days": 7, "calls": 0})

    res = asyncio.run(server.get_audit_report())

    assert isinstance(res, str)
    assert "last 7 days" in res and "batch_safety_check" in res, res
    assert not any(u.endswith("/sessions") for u in sent), f"建了空 session：{sent}"


def test_explicit_session_id_path_is_untouched(wired):
    """老路径一步都不能变——老客户端传着 session_id，不该因为这次改动多打一个请求。"""
    sent = wired({"chemicals": ["acetone"], "days": 7, "calls": 1})

    res = asyncio.run(server.get_audit_report(session_id="DEMO-OLDPATH"))

    assert "/reports/signed/abc" in res.content[0].text
    assert not any("/api/v2/recent-chemicals" in u for u in sent), \
        f"带了 session_id 还去查最近分析：{sent}"
    assert not any(u.endswith("/sessions") for u in sent), "带了 session_id 还建了新 session"
    assert res.structured_content["built_from_recent_analyses"] is False


def test_session_id_is_optional_in_the_schema():
    """判据打在**客户端看得到的 schema** 上：函数签名给了默认值，但如果 schema 仍把它列进
    `required`，模型依然会觉得必须先有个 id——那这次改动对调用方等于没发生。"""
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = tools["get_audit_report"].input_schema or {}
    assert "session_id" not in (schema.get("required") or []), schema
    desc = (schema.get("properties", {}).get("session_id", {}).get("description") or "").lower()
    assert "omit" in desc, f"描述没告诉模型可以不传：{desc!r}"


def test_batch_hint_points_at_the_zero_argument_call():
    """那句转化为 0 的提示必须改口——它此前指的正是用户做不到的那一步。

    仍然只挂在 `batch_safety_check` 一个工具上：同一个工具、同样的曝光、只变「两步→一步」
    这一个变量，下一轮才读得出是不是这一步的问题。
    """
    import inspect
    src = inspect.getsource(server.batch_safety_check)
    assert "get_audit_report()" in src, "提示没改口，还在指向 create_audit_session"
    assert "create_audit_session" not in src.split("---")[-1], src.split("---")[-1]
