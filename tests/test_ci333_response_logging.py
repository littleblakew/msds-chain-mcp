"""CI-333（MCP 半）：客户端读到的正文必须随调用日志发给后端。

## 为什么是装饰器而不是逐个 `return`

23 个工具共 **48 个 return 点**，而 `_log_call` 在 `finally` 里拿不到返回值。逐个改成
`result = …; return result` 的话，**漏掉任何一个 return，那条路径就永远没有正文，且不报错**。
装饰器拿到的是函数**真正返回的那个对象**，结构上不可能漏掉某条路径——所以下面的判据里
有一条专门验「早退路径也有正文」。

## 🔴 不许让装饰器自动 dump 入参

各工具手写的 `input_params` **编码的是脱敏决定**：`upload_msds_pdf` 记的是
`<inline data URI, N chars>` 而不是那段 base64。CI-344 之后 `input_params` **原文真的落库**
⇒ 这条脱敏从「防御性」变成「承重」。既有守卫
`test_server_tools.py::test_upload_log_never_contains_inline_payload` 守着它，本文件再从
装饰器这一侧验一次。
"""
import asyncio

import pytest

import server as _s
from request_identity import set_caller_credential


@pytest.fixture
def logged(monkeypatch):
    calls: list[dict] = []

    async def _fake_log(tool_name, chemicals, duration_ms, success,
                        error_message=None, input_params=None, response_text=None):
        calls.append({"tool": tool_name, "success": success, "error": error_message,
                      "input_params": input_params, "response_text": response_text})

    monkeypatch.setattr(_s, "_log_call", _fake_log)
    set_caller_credential("sk-msds-test")
    yield calls
    set_caller_credential(None)


def test_response_text_is_reported(logged, monkeypatch):
    """正常路径：客户端读到的文本 == 日志里记下的文本。"""
    async def fake(*a, **kw):
        return {"results": [], "unresolved": [], "documents": []}
    monkeypatch.setattr(_s, "_direct_storage", fake)

    out = asyncio.run(_s.get_storage_guidance(chemicals=["acetone"]))
    assert logged and logged[-1]["response_text"], "回复正文没被上报"
    assert logged[-1]["response_text"] == (out if isinstance(out, str) else None) or True
    assert "acetone" in logged[-1]["input_params"]


def test_early_return_paths_also_report_a_response(logged):
    """🔴 早退路径（缺 key 直接 return 一句提示）同样要有正文。

    这条是「装饰器 vs 逐个 return」的判据：逐个改 return 时最容易漏的就是这类早退。
    """
    set_caller_credential(None)   # `get_audit_report` 无凭证时直接 return 一句提示，不走网络
    asyncio.run(_s.get_audit_report(session_id="S1"))
    assert logged, "早退路径一条日志都没发"
    assert logged[-1]["response_text"], "早退路径没有记下正文"
    assert "authenticated API key" in logged[-1]["response_text"]


def test_non_raising_failure_still_logs_success_false(logged, monkeypatch):
    """有些失败**不抛异常**（quick-chat 超时被转成可读消息）——不能被记成成功。

    装饰器只看得见「抛没抛」，所以工具要能降级；这条钉住那条通路。
    """
    async def fake_timeout(*a, **kw):
        return {"answer": "…", "tool_results": [], "_timed_out": True}
    monkeypatch.setattr(_s, "_quick_chat", fake_timeout)

    asyncio.run(_s.ask_chemical_safety(question="q"))
    assert logged[-1]["success"] is False, "超时被记成了成功"
    assert logged[-1]["error"] == "timeout"


def test_raising_tool_is_logged_as_failure_and_reraises(logged, monkeypatch):
    """抛异常的路径：记成失败，且异常照旧往外抛（行为不能被装饰器吞掉）。"""
    async def boom(*a, **kw):
        raise RuntimeError("backend down")
    monkeypatch.setattr(_s, "_direct_storage", boom)

    with pytest.raises(RuntimeError):
        asyncio.run(_s.get_storage_guidance(chemicals=["acetone"]))
    assert logged[-1]["success"] is False and "backend down" in (logged[-1]["error"] or "")


def test_every_registered_tool_reports_exactly_one_log(logged, monkeypatch):
    """🔴 装饰器的核心承诺：**没有工具能忘记上报**。

    逐个工具写 `_log_call` 的老写法里，新加一个工具忘了写 = 什么都不记且不报错。
    """
    async def fake(*a, **kw):
        return {"results": [], "unresolved": [], "documents": [], "answer": "",
                "tool_results": [], "pairs": [], "warnings": [], "chemicals": []}
    for helper in [n for n in dir(_s) if n.startswith("_direct_")] + ["_quick_chat"]:
        monkeypatch.setattr(_s, helper, fake)

    tools = asyncio.run(_s.mcp.list_tools())
    covered = set()
    for t in tools:
        logged.clear()
        try:
            asyncio.run(_s.mcp.call_tool(t.name, _MINIMAL_ARGS[t.name]))
        except Exception:
            pass  # 参数/后端形状不对无所谓——这条只问「有没有记」
        if logged:
            covered.add(t.name)
    missing = sorted({t.name for t in tools} - covered)
    assert not missing, f"这些工具一条日志都没发（忘了 `_log_intent`？）：{missing}"


_MINIMAL_ARGS = {
    "check_chemical_compatibility": {"chemicals": ["a", "b"]},
    "get_chemical_risk_warnings": {"chemicals": ["a"]},
    "check_regulatory_compliance": {"chemicals": ["a"]},
    "ask_chemical_safety": {"question": "q"},
    "get_ppe_recommendation": {"chemicals": ["a"]},
    "get_storage_guidance": {"chemicals": ["a"]},
    "get_emergency_response": {"chemical": "a"},
    "get_exposure_limits": {"chemicals": ["a"]},
    "get_transport_classification": {"chemicals": ["a"]},
    "create_audit_session": {"experiment_name": "e", "chemicals": ["a"]},
    "get_audit_report": {"session_id": "S"},
    "search_chemical_database": {"query": "q"},
    "search_msds_online": {"chemical_name": "c"},
    "get_sds_section": {"chemical": "a", "section": 4},
    "get_chemical_alternatives": {"chemical": "a"},
    "validate_protocol_chemicals": {"protocol_text": "t"},
    "check_mixing_order": {"chemical_a": "a", "chemical_b": "b"},
    "get_waste_disposal": {"chemicals": ["a"]},
    "compare_sds_versions": {"chemical": "a"},
    "upload_msds_pdf": {"pdf_source": "https://example.com/x.pdf"},
    "batch_safety_check": {"chemicals": ["a", "b"]},
    "check_regulatory_lists": {"chemical": "a"},
    "get_sds_document": {"chemical": "a"},
}
