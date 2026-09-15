"""CI-937：`preparation_disclosure_tail` 不许进 MCP 的 structuredContent。

**它是什么**：安全披露**首句之后**那半段。CI-917① 为网页端加的——前端要「自己写首句 +
接着念其余」，此前它在客户端拿正则剥第一句，而正文里的 `0.03%` 被从**小数点**处切开、
屏幕上印成 `03%`（不报错、不空白，只是把一个安全相关的数字换成了另一个）。

**为什么 MCP 面要剥掉它**：风险是**不对称**的 —— 完整的 `preparation_disclosure` 带着浓度，
tail **不带** ⇒ 只印 tail 的消费者会**静默丢掉浓度**，正好是 CI-917① 要消灭的那个失败。
而 MCP 面本来就拿得到完整那句 ⇒ 删掉 tail 不减少任何信息。

🔴 **本文件的判据形状是这张票最值钱的部分，别改软**：

1. **替身必须放在 HTTP 层**，不能替换 `_direct_*`。实现是在**入口** `_billed_json` 剥的；
   把 `_direct_*` 整个换掉会绕过它 ⇒ 守卫**永远看不见修复**（我第一版探针就是这么写的，
   撤掉修复与带上修复给出完全一样的结果）。这是 [[narrow-hand-rolled-fixtures-and-engine-specific-branches]]
   的形状：**替身在被测的那条性质上与真货不同形**。
2. **成员自己发现**：按 `mcp.list_tools()` 全量扫，新工具自动进来。
   变异＝往仓里加一个透传后端载荷的新工具，它应当自动出现在覆盖里。
3. **必须断言「真的跑到了几个」**，不只断言「没有泄漏」——参数不对时工具会抛异常，
   而「全部抛掉了」与「一个都没漏」在只看 `leaks` 的守卫里完全同形
   （[[green-run-that-executed-nothing]]）。
4. **阳性对照实测**（2026-09-15）：把 `_drop_tail_keys` 从 `_billed_json` 撤掉
   ⇒ 23 个工具里 **15 个**漏；带上 ⇒ **0 个**。
"""
import asyncio
import inspect

import pytest

import server
from mcp.types import CallToolRequestParams
from request_identity import set_caller_credential

_TAIL = ("Hazards, exposure limits and handling for the pure material can be "
         "substantially more severe. Use the SDS for the material you actually hold.")
_ITEM = {
    "chemical_name": "acetone", "cas": "67-64-1", "storage_class": "general",
    "preparation_percent": 5,
    "preparation_disclosure": "⚠️ …a PREPARATION containing this substance at 5% by weight…",
    "preparation_disclosure_tail": _TAIL,
}
# 🔴 每个容器都塞一份：这里**不是**在模拟真实载荷的形状，而是在问
# 「**只要后端把这个键放在任何一个位置，它会不会到达客户端**」。
_PAYLOAD = {
    "results": [_ITEM], "chemicals": [_ITEM], "warnings": [_ITEM], "pairs": [_ITEM],
    "risk_warnings": [_ITEM], "source_info": dict(_ITEM), "sections": [_ITEM],
    "alternatives": [_ITEM], "tool_results": [_ITEM],
    "answer": "x", "url": "https://example.invalid/y.pdf", "session_id": "S1", **_ITEM,
}

_ARGS = {
    "chemicals": ["acetone", "bleach"], "chemical": "acetone", "session_id": "S1",
    "question": "hazards?", "protocol": "mix a and b", "section": 4,
    "url": "https://example.invalid/y.pdf", "file_path": "/tmp/x.pdf",
    "region": "EU", "days": 7, "scenario": "spill",
}


class _Resp:
    status_code = 200
    headers: dict = {}

    def json(self):
        return _PAYLOAD

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self, *a, **k): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **k): return _Resp()
    async def get(self, *a, **k): return _Resp()


@pytest.fixture(autouse=True)
def _harness(monkeypatch):
    async def _noop(*a, **kw):
        return None
    # 🔴 只换 HTTP 层，别换 `_direct_*`（见文件头第 1 条）
    monkeypatch.setattr(server.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(server, "_log_call", _noop)
    set_caller_credential("sk-msds-test")
    yield
    set_caller_credential(None)


def _call_every_tool():
    leaked, ran = [], []
    for tool in asyncio.run(server.mcp.list_tools()):
        fn = getattr(server, tool.name, None)
        if fn is None:
            continue
        args = {p: _ARGS[p] for p, v in inspect.signature(fn).parameters.items()
                if v.default is v.empty and p in _ARGS}
        try:
            res = asyncio.run(server.mcp._handle_call_tool(
                None, CallToolRequestParams(name=tool.name, arguments=args)))
        except Exception:
            continue
        ran.append(tool.name)
        if res.structured_content is not None and "preparation_disclosure_tail" in str(
                res.structured_content):
            leaked.append(tool.name)
    return ran, leaked


def test_tail_never_reaches_structured_content():
    ran, leaked = _call_every_tool()
    # ③ 先断言覆盖面：全部抛异常时 `leaked` 也是空的，与「一个都没漏」同形
    assert len(ran) >= 20, f"只跑到 {len(ran)} 个工具（{ran}）——覆盖面塌了，这一轮什么都没测"
    assert not leaked, (
        f"这些工具把 preparation_disclosure_tail 透进了 structuredContent：{leaked}。"
        f"只印 tail 的消费者会静默丢掉浓度——完整的 preparation_disclosure 才带浓度。")


def test_the_full_disclosure_itself_is_still_delivered():
    """🔴 反方向：别把浓度那句一起剥掉了。

    只验「tail 不在」会漏掉**误伤方向** —— 如果 `_drop_tail_keys` 的键集写宽了
    （比如按前缀匹配 `preparation_disclosure*`），完整那句会一起消失，而那句**才是**
    带浓度的、CI-917① 真正要送达的东西。这条守的就是它。
    """
    ran, _ = _call_every_tool()
    assert ran, "一个工具都没跑到"
    delivered = []
    for tool_name in ran:
        fn = getattr(server, tool_name)
        args = {p: _ARGS[p] for p, v in inspect.signature(fn).parameters.items()
                if v.default is v.empty and p in _ARGS}
        res = asyncio.run(server.mcp._handle_call_tool(
            None, CallToolRequestParams(name=tool_name, arguments=args)))
        if res.structured_content is not None and "preparation_disclosure" in str(
                res.structured_content):
            delivered.append(tool_name)
    assert delivered, "没有任何工具送出 preparation_disclosure —— 剥得太狠，把浓度那句也剥掉了"
