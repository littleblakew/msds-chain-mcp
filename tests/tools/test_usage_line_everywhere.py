"""CI-1661：每个被计费的工具都要在文本尾部说「这次用了多少 · 还剩多少」。

模型读不到单价就不会省着调：查询类工具（`get_sds_section` 每次 1 credit）原来只在
structuredContent 里剥掉 `_usage`、文本一字不提，于是一个问题会被拆成几十次并行调用。
补行的位置在 `_reported`（每个工具都戴着它），所以这里挑**没有自己调 `_with_usage`**
的工具来测——挑判断类工具会恒绿，什么都没测到。
"""
import asyncio

import pytest

import server
from mcp.types import CallToolRequestParams
from msds_mcp import calls
from request_identity import set_caller_credential

_PAYLOAD = {"results": [{"chemical_name": "acetone", "cas": "67-64-1",
                         "storage_class": "general"}],
            "chemical": "acetone", "cas": "67-64-1", "content": "Keep away from heat."}

_METERED = {"X-Msds-Credits-Cost": "1", "X-Msds-Credits-Balance": "19",
            "X-Msds-Credits-Reason": ""}


class _Resp:
    status_code = 200

    def __init__(self, headers):
        self.headers = headers

    def json(self):
        return dict(_PAYLOAD)

    def raise_for_status(self):
        pass


def _client(headers):
    class _C:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp(headers)
        async def get(self, *a, **k): return _Resp(headers)
    return _C


@pytest.fixture(autouse=True)
def _cred(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(server, "_log_call", _noop)
    set_caller_credential("sk-msds-test")
    yield
    set_caller_credential(None)


def _call(monkeypatch, headers, tool, args):
    monkeypatch.setattr(server.httpx, "AsyncClient", _client(headers))
    res = asyncio.run(server.mcp._handle_call_tool(
        None, CallToolRequestParams(name=tool, arguments=args)))
    text = "".join(c.text for c in res.content if getattr(c, "text", None))
    return text, res


@pytest.mark.parametrize("tool,args", [
    ("get_sds_section", {"chemical": "acetone", "section": 7}),
    ("get_storage_guidance", {"chemicals": ["acetone"]}),
])
def test_lookup_tools_report_cost_and_balance(monkeypatch, tool, args):
    text, res = _call(monkeypatch, _METERED, tool, args)
    assert "used 1 credits" in text and "Balance: 19 credits remaining" in text, text[-300:]
    assert text.count("💳") == 1, text[-300:]
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None) or {}
    assert sc.get("usage", {}).get("cost") == 1, sc.keys()
    assert "_usage" not in sc


def test_tool_that_renders_its_own_line_is_not_doubled(monkeypatch):
    text, _ = _call(monkeypatch, _METERED, "get_chemical_risk_warnings",
                    {"chemicals": ["acetone"]})
    assert text.count("💳") == 1, text[-300:]


def test_unmetered_lookup_adds_nothing(monkeypatch):
    text, _ = _call(monkeypatch, {}, "get_sds_section", {"chemical": "acetone", "section": 7})
    assert "💳" not in text, text[-300:]


def test_fan_out_sums_cost_and_keeps_lowest_balance():
    async def run():
        token = calls._call_usage.set({})
        try:
            async def one(bal):
                calls._note_call_usage({"cost": 1.0, "balance": bal, "reason": ""})
            await asyncio.gather(one(19.0), one(17.0), one(18.0))
            return dict(calls._call_usage.get())
        finally:
            calls._call_usage.reset(token)
    acc = asyncio.run(run())
    assert acc["cost"] == 3.0 and acc["balance"] == 17.0, acc


def test_hosted_instructions_tell_the_model_calls_cost_credits():
    text = (server_root() / "hosted_instructions.txt").read_text(encoding="utf-8")
    assert "CREDITS:" in text and "get_sds_section" in text and "💳" in text


def server_root():
    from pathlib import Path
    return Path(server.__file__).resolve().parent
