"""CI-529：四个工具在调用日志里把 `chemicals` 记成 NULL ⇒ 按化学品聚合时它们是盲的。

Prod 实测（2026-08-15）：`upload_msds_pdf` 的行 `input_params`/`response_text` 都有值、
`chemicals` 为 0。问题**可以**从 `input_params` 复现（[[CI-344]] 已解决），缺的只是聚合维度。
2026-08-16 起这不再只是「口径不全」——[[CI-174]] 的报告范围就是按 `chemicals` 取的，
`validate_protocol_chemicals`（最该进报告的一种调用）因此**进不了报告**。

🔴 判据全部打在 **`_log_call` 实际收到的 `chemicals` 参数**上，不是「函数返回了什么」：
这一列的唯一消费者是日志/聚合，看返回值证明不了它被记下来了。

🔴 另一条同样重要：**只从后端已解析的结构化字段取**。从 `answer` 正文里抽名字是 CI-527 的
地盘且是已判定错误的路——现在是「缺」，那样会变成「错」，而错的看不出来。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential


@pytest.fixture
def logged(monkeypatch):
    """捕获 `_log_call` 收到的 (tool_name, chemicals)。"""
    box: list = []

    async def _capture(tool_name, chemicals, ms, success, error_message, *a, **kw):
        box.append({"tool": tool_name, "chemicals": chemicals})

    monkeypatch.setattr(server, "_log_call", _capture)
    set_caller_credential("sk-msds-test")
    yield box
    set_caller_credential(None)


def _quick_chat_returning(payload):
    async def _fake(*a, **kw):
        return payload
    return _fake


# ---------------------------------------------------------------- 提取器本身

def test_extractor_reads_documents_and_tool_results():
    out = server._chemicals_from_response({
        "answer": "Toluene is also flammable.",           # ← 正文里的名字不许被抽走
        "documents": [{"chemical_name": "Acetone", "cas": "67-64-1"}],
        "tool_results": [{"tool": "check_regulatory_lists",
                          "result": {"chemical": "benzene", "cas": "71-43-2"}}],
    })
    assert out == ["Acetone", "benzene"], out
    assert "Toluene" not in out, "从 answer 正文里抽了名字——那是 CI-527 的地盘且是错的路"


def test_extractor_reads_compatibility_pairs():
    out = server._chemicals_from_response({
        "tool_results": [{"result": {"pairs": [
            {"chemical_a": "acetone", "chemical_b": "bleach"},
            {"chem1": "acetone", "chem2": "water"},
        ]}}],
    })
    assert out == ["acetone", "bleach", "water"], out


def test_extractor_returns_none_not_empty_list():
    """🔴 取不到要给 None。空列表会让「这次调用没有化学品」看起来像个结论，
    而事实是「我们没取到」——下游按 NULL 和按 [] 聚合出来的意思不一样。"""
    assert server._chemicals_from_response({"answer": "sorry"}) is None
    assert server._chemicals_from_response(None) is None
    assert server._chemicals_from_response({"documents": [], "tool_results": []}) is None


def test_extractor_dedupes_case_insensitively():
    out = server._chemicals_from_response({
        "documents": [{"chemical_name": "Acetone"}],
        "tool_results": [{"result": {"chemicals": ["acetone", "ACETONE", "methanol"]}}],
    })
    assert out == ["Acetone", "methanol"], out


# ---------------------------------------------------------------- 四个工具的接线

def test_ask_chemical_safety_logs_the_chemicals(logged, monkeypatch):
    monkeypatch.setattr(server, "_quick_chat", _quick_chat_returning({
        "answer": "…", "documents": [{"chemical_name": "Acetone"}],
        "tool_results": [], "_timed_out": False,
    }))
    asyncio.run(server.ask_chemical_safety("What PPE for acetone?"))
    assert logged[0]["chemicals"] == ["Acetone"], logged


def test_validate_protocol_chemicals_logs_the_chemicals(logged, monkeypatch):
    """这条是本票 2026-08-16 之后最要紧的一个：CI-174 的报告范围按 `chemicals` 取，
    协议校验恰恰是最该进报告的一种调用。"""
    monkeypatch.setattr(server, "_quick_chat", _quick_chat_returning({
        "answer": "…", "documents": [],
        "tool_results": [{"result": {"chemicals": ["acetone", "toluene"]}}],
    }))
    asyncio.run(server.validate_protocol_chemicals("Add 10 mL acetone, then toluene."))
    assert logged[0]["chemicals"] == ["acetone", "toluene"], logged


def test_a_failed_quick_chat_still_logs_none_not_a_lie(logged, monkeypatch):
    """后端超时降级时没有解析结果 ⇒ 记 None，别拿用户的问句凑数。"""
    monkeypatch.setattr(server, "_quick_chat", _quick_chat_returning({
        "answer": "timed out", "_timed_out": True,
    }))
    asyncio.run(server.ask_chemical_safety("acetone hazards?"))
    assert logged[0]["chemicals"] is None, logged


def test_upload_logs_the_parsed_chemical_names(logged, monkeypatch):
    """上传的化学品名来自**后端解析结果**（`chemical_name`），不是文件名。"""
    class _Resp:
        status_code = 200
        headers: dict = {}

        def raise_for_status(self): ...

        def json(self):
            return {"session_id": "DEMO-X", "results": [
                {"chemical_name": "Acetone", "cas_number": "67-64-1", "status": "parsed"},
                {"chemical_name": "Methanol", "cas_number": "67-56-1", "status": "parsed"},
            ]}

    class _Client:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return _Resp()
        async def get(self, *a, **kw): return _Resp()

    monkeypatch.setattr(server.httpx, "AsyncClient", _Client)
    pdf = "data:application/pdf;base64," + __import__("base64").b64encode(
        b"%PDF-1.4 fake").decode()
    asyncio.run(server.upload_msds_pdf(pdf))

    assert logged[0]["tool"] == "upload_msds_pdf"
    assert logged[0]["chemicals"] == ["Acetone", "Methanol"], logged
