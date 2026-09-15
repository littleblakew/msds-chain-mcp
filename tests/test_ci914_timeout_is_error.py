"""CI-914：超时必须让 MCP 的 `isError` 位为真；CI-915：那句话不许猜成因。

**为什么判据不能打在工具函数的返回值上。** `server.mcp.call_tool()` 抛 `ToolError`，
`isError` 这一位是**再上一层** `MCPServer._handle_call_tool` 在 `except Exception` 里
设的（`mcp/server/mcpserver/server.py:424`）。在工具层断言「抛了没有」只能证明
「有东西抛出来」，证明不了**调用方最终看到的那一位**是什么 —— 而本票整个内容就是那一位。
⇒ 本文件一律走 `_handle_call_tool`，也就是线上真正产出 `CallToolResult` 的那条路。

🔴 **三种输入各自断言，不是只验超时那一格**（票面写死）：
只断言「超时 ⇒ True」会漏掉**误伤方向** —— 把正常返回也标成错误，
那种回归在只测超时的守卫下完全是绿的。

🔴 **反向变异（三个方向，实测过；别信这段话，自己再跑一遍）**：
① 把返回改成 `return msg` ⇒ 超时那组红（`is_error` 变 False）
② 把返回改成 `raise RuntimeError(msg)` ⇒ 超时那组红（文字被加上英文前缀 `Error executing tool …`）
   —— **这个方向是 PR #50 的 review 挖出来的，第一版实现就栽在这里**
③ 在 `_DIRECT_TIMEOUT_MSG["en"]` 里加回 "just after a deploy" ⇒ 文案那条红
④ 只改散文（注释/docstring）⇒ 全绿（守卫不该被散文影响）

📌 用 `get_storage_guidance` 是因为**它就是 2026-09-11 回放里真超时的那个工具**
（20 个化学品 / 45.2 秒 / `is_error=False`），不是随手挑的。
"""
import asyncio

import httpx
import pytest

import server
from mcp.types import CallToolRequestParams
from request_identity import set_caller_credential

_TOOL = "get_storage_guidance"
_ARGS = {"chemicals": ["acetone"]}


@pytest.fixture(autouse=True)
def _cred(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(server, "_log_call", _noop)
    set_caller_credential("sk-msds-test")
    yield
    set_caller_credential(None)


def _call(**extra):
    """走线上那条路：`_handle_call_tool` 才是产出 `is_error` 的那一层。"""
    params = CallToolRequestParams(name=_TOOL, arguments={**_ARGS, **extra})
    return asyncio.run(server.mcp._handle_call_tool(None, params))


def _text(result) -> str:
    return "".join(c.text for c in result.content if getattr(c, "text", None))


class _Resp:
    """最小的 httpx 响应替身（4xx 那格要走真实的 `_raise_for_status_with_reason`）。"""

    def __init__(self, payload, status):
        self._p, self.status_code, self.headers = payload, status, {}

    def json(self):
        return self._p

    def raise_for_status(self):
        raise AssertionError("不该走到 raise_for_status —— 422 应在它之前被特判")


def _client_returning(resp):
    class _C:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return resp
        async def get(self, *a, **kw): return resp
    return _C


# ---------------------------------------------------------------- 三种输入


def test_normal_return_is_not_flagged_as_error(monkeypatch):
    """① 正常返回 ⇒ `is_error` 必须是 False（这一条守的是**误伤方向**）。"""
    async def _ok(*a, **kw):
        return {"results": [{"chemical_name": "acetone", "storage_class": "general"}]}

    monkeypatch.setattr(server, "_direct_storage", _ok)
    res = _call()
    assert res.is_error is False, f"把正常返回标成了错误：{_text(res)!r}"


@pytest.mark.parametrize("lang", ["en", "zh", "ja", "de", "id"])
def test_timeout_sets_is_error_and_keeps_the_text_verbatim(monkeypatch, lang):
    """② 超时 ⇒ `is_error` 必须是 True，**且文字逐字是我们写的那句**。

    两条性质缺一不可，而它们各自对应一种改错的方式：
    · `return msg` ⇒ 位变成 False（本票的原始缺陷）
    · `raise RuntimeError(msg)` ⇒ 位是对的，但 `Tool.run()` 会包成
      `ToolError("Error executing tool {name}: …")` —— **英文硬编码前缀**粘在五个语种前面，
      正是 CI-55 要消灭的那种样板。

    🔴 **断言必须是逐字相等，不能是子串**。PR #50 第一版就是 `raise`，而当时这条写的是
    `assert "timed out" in text` ⇒ **前缀在探针输出里印着，测试也绿着，没有任何东西报警**。
    子串断言的粒度比它要保证的性质粗一档，这就是本仓 [[green-run-that-executed-nothing]]。
    """
    async def _timeout(*a, **kw):
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(server, "_direct_storage", _timeout)
    res = _call(lang=lang)
    assert res.is_error is True, f"超时被报成了成功：{_text(res)!r}"
    assert _text(res) == server._DIRECT_TIMEOUT_MSG[lang], (
        f"{lang} 的超时文字不是逐字那句（多半是被 ToolError 加了英文前缀）：{_text(res)!r}")


def test_backend_4xx_sets_is_error(monkeypatch):
    """③ 后端 4xx ⇒ `is_error` 必须是 True（这条路本来就抛，守它别被改回吞掉）。"""
    monkeypatch.setattr(
        server.httpx, "AsyncClient",
        _client_returning(_Resp({"detail": [
            {"loc": ["body", "chemicals"], "msg": "List should have at most 24 items",
             "type": "too_long"}]}, 422)))
    res = _call()
    assert res.is_error is True, f"422 被报成了成功：{_text(res)!r}"
    # 🔴 这一格**故意**只断言子串：4xx 走的是 `raise`（既有行为，不在本票范围），
    # 所以它的文字确实带着 `Tool.run()` 的英文前缀。拿逐字相等去卡它等于顺手改了另一条路。
    assert "422" in _text(res), f"原因没到调用方手里：{_text(res)!r}"


# ---------------------------------------------------------------- CI-915


def test_timeout_text_does_not_guess_a_cause():
    """CI-915：兜底文案不许猜成因。

    原文说「常见于刚部署后」，而实测那次超时时当天最后一次部署已在几小时之前，
    真实成因是查询本身很重（20 个化学品）。**误导性诊断比没有诊断更贵**：
    它让用户重试一个必然再超时的查询，也让排查的人先去翻空无一物的部署记录。

    🔴 判据是**五个语种都不许出现部署归因**，不是只查英文 —— 作用域是手写的，
    只断言 `["en"]` 会让另外四句悄悄留着那句猜测（本仓 [[green-run-that-executed-nothing]]）。
    """
    banned = ("deploy", "Deployment", "デプロイ", "部署")
    for lang, msg in server._DIRECT_TIMEOUT_MSG.items():
        for word in banned:
            assert word not in msg, f"{lang} 的超时文案又在猜成因（命中 {word!r}）：{msg!r}"

    # 而且必须留下一条**可行动**的路，否则「不猜成因」会退化成一句废话
    assert len(server._DIRECT_TIMEOUT_MSG) == 5, "语种少了——文案是逐语言写的，别只改英文"
