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

# 🔴 下面这些 fixture 是**照后端代码抄的**，不是想当然写的：
#   `quick_engine.py:257`  → search_chemical 的 result 形状（chemicals 是**匹配记录**，
#                            query 才是用户原词）
#   `agent/tools/compatibility.py:111,177` → chemical_a/b 在**顶层**，多组分在 `matrix`
# 初版这两处都写错了（`chemicals` 当成字符串列表、相容性放在 `pairs`），于是提取器在
# 生产上恒空而测试全绿——review 抓到的，正是 [[narrow-hand-rolled-fixtures]] 的形状。
# 🔴 改这些 fixture 前先回那两个文件看一眼，别照着这里的样子继续编。

def test_extractor_reads_the_real_search_chemical_shape():
    out = server._chemicals_from_response({
        "answer": "Toluene is also flammable.",           # ← 正文里的名字不许被抽走
        "documents": [],
        "tool_results": [{"tool": "search_chemical", "result": {
            "chemicals": [{"name": "Acetone", "cas_number": "67-64-1"}],
            "query": "acetone", "match_count": 1}}],
    })
    # `query`（用户原词）与命中名大小写相同 ⇒ 去重后只留先出现的那个，这是期望行为
    assert out == ["acetone"], out
    assert "Toluene" not in out, "从 answer 正文里抽了名字——那是 CI-527 的地盘且是错的路"


def test_extractor_reads_the_real_compatibility_shape():
    """顶层 `chemical_a`/`chemical_b`（单对）与 `matrix[]`（多组分），不是 `pairs`。"""
    single = server._chemicals_from_response({"tool_results": [
        {"tool": "check_compatibility",
         "result": {"chemical_a": "acetone", "chemical_b": "bleach", "level": "danger"}}]})
    assert single == ["acetone", "bleach"], single

    matrix = server._chemicals_from_response({"tool_results": [
        {"tool": "check_all_compatibility", "result": {"matrix": [
            {"chemical_a": "acetone", "chemical_b": "water", "level": "safe"}]}}]})
    assert matrix == ["acetone", "water"], matrix


def test_extractor_reads_documents_and_risk_warnings():
    out = server._chemicals_from_response({
        "documents": [{"chemical": "acetone",
                       "chemical_name": "Acetone, ACS reagent, ≥99.5%"}],
        "tool_results": [{"result": {"warnings": [{"chemical": "benzene", "level": "high"}]}}],
    })
    # 🔴 用调用方问的词，不用供应商 SDS 的产品标题——后者拿去 resolve_cas 可能解析成别的
    assert out == ["acetone", "benzene"], out


def test_names_with_commas_are_dropped_not_split_downstream():
    """🔴 这一列在后端按 `",".join()` 存、读回来按逗号切 ⇒ `N,N-dimethylformamide`
    会变成 `N` + `N-dimethylformamide` 两条，而 CI-174 的报告范围正是从这里取的。
    丢掉比劈开好：缺一条是缺，劈开是**编造**。"""
    out = server._chemicals_from_response({"tool_results": [{"result": {
        "chemicals": [{"name": "N,N-dimethylformamide"}, {"name": "acetone"}], "query": ""}}]})
    assert out == ["acetone"], out


def test_extractor_never_raises_out_of_the_finally_block():
    """它在 `finally` 里跑：抛出去会**同时**毁掉工具返回值和整条调用日志。"""
    assert server._chemicals_from_response({"documents": 3, "tool_results": "nope"}) is None
    assert server._chemicals_from_response({"tool_results": [{"result": {"matrix": 7}}]}) is None


def test_extractor_returns_none_not_empty_list():
    """🔴 取不到要给 None。空列表会让「这次调用没有化学品」看起来像个结论，
    而事实是「我们没取到」——下游按 NULL 和按 [] 聚合出来的意思不一样。"""
    assert server._chemicals_from_response({"answer": "sorry"}) is None
    assert server._chemicals_from_response(None) is None
    assert server._chemicals_from_response({"documents": [], "tool_results": []}) is None


def test_extractor_dedupes_case_insensitively():
    out = server._chemicals_from_response({
        "documents": [{"chemical_name": "Acetone"}],
        "tool_results": [{"result": {"chemicals": [
            {"name": "acetone"}, {"name": "ACETONE"}, {"name": "methanol"}]}}],
    })
    assert out == ["Acetone", "methanol"], out


def test_plain_string_hits_are_ignored_because_the_backend_never_sends_them():
    """阳性对照的反面：`chemicals` 是**匹配记录**的列表，不是字符串列表。
    如果哪天后端真改成字符串列表，这条会红——那正是该回来改提取器的信号。"""
    assert server._chemicals_from_response(
        {"tool_results": [{"result": {"chemicals": ["acetone"]}}]}) is None


# ---------------------------------------------------------------- 四个工具的接线

def test_ask_chemical_safety_logs_the_chemicals(logged, monkeypatch):
    monkeypatch.setattr(server, "_quick_chat", _quick_chat_returning({
        "answer": "…", "documents": [{"chemical": "acetone"}],
        "tool_results": [{"tool": "search_chemical", "result": {
            "chemicals": [{"name": "Acetone", "cas_number": "67-64-1"}],
            "query": "acetone", "match_count": 1}}],
        "_timed_out": False,
    }))
    asyncio.run(server.ask_chemical_safety("What PPE for acetone?"))
    assert logged[0]["chemicals"] == ["acetone"], logged


def test_validate_protocol_chemicals_logs_the_chemicals(logged, monkeypatch):
    """这条是本票 2026-08-16 之后最要紧的一个：CI-174 的报告范围按 `chemicals` 取，
    协议校验恰恰是最该进报告的一种调用。"""
    monkeypatch.setattr(server, "_quick_chat", _quick_chat_returning({
        "answer": "…", "documents": [],
        # 真实形状：generic intent 走 search_chemical，一个化学品一条 tool_result
        "tool_results": [
            {"tool": "search_chemical", "result": {
                "chemicals": [{"name": "Acetone"}], "query": "acetone", "match_count": 1}},
            {"tool": "search_chemical", "result": {
                "chemicals": [{"name": "Toluene"}], "query": "toluene", "match_count": 1}},
        ],
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
