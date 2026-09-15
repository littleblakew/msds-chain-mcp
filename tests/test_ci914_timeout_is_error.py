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

📌 用 `get_storage_guidance` 是因为**它就是 回放里真超时的那个工具**
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
    # `get_storage_guidance` 收列表 ⇒ 期望值是「通用句 + 批量提示」，用生产那个函数算，
    # 但 `batch=True` 是本测试**独立**断言的（工具签名里确实有 `chemicals`），不是抄它的判断。
    expected = server._timeout_message(lang, batch=True)
    assert _text(res) == expected, (
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
    #
    # 🔴 **升级 `mcp` 之后这条红了，别当 flaky 删掉。** 依赖钉的是 `mcp>=2.0.0,<3`，
    # 而实测 `2.2.0` 的 `Tool.run()` 不再把原异常文本 `{e}` 包进 `ToolError`
    # （PR #50 第二轮 review 顺带量到的）⇒ 这条红 **＝ [[CI-410]] 的保证真的断了**：
    # 「422 的原因要到达调用方」不再成立，模型又看不出是「化学品超过 24 个」还是参数名写错。
    # 处置是去修那条路（把原因放进我们自己构造的 CallToolResult），不是放宽这条断言。
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


# ---------------------------------------------------------------- CI-915 第二条


def _wrapped_tools():
    """**自己发现成员**：扫出所有被 `@_graceful_timeout` 包住的工具，别手写名单。

    手写名单会腐化——新加一个工具没人回来改它，而守卫会一直是绿的
    （本仓 [[my-own-guards-are-often-no-ops]]：`CI-420` 那道守卫就是这么漏掉 10 个成员的）。
    **变异方式＝往 `server.py` 里加一个新的 `@_graceful_timeout` 工具，看这条会不会跟着覆盖它。**
    """
    import inspect
    import re
    src = inspect.getsource(server)
    out = []
    for name, params in re.findall(r"@_graceful_timeout\n(?:@\w+\n)*async def (\w+)\(([^)]*)\)", src):
        out.append((name, "chemicals" in params))
    assert len(out) >= 15, f"扫描面塌了，只找到 {len(out)} 个工具——正则跟代码脱钩了"
    return out


def _args_for(name: str) -> dict:
    sig = __import__("inspect").signature(getattr(server, name))
    args = {}
    for pname, p in sig.parameters.items():
        if p.default is not p.empty:
            continue
        # 🔴 给**两个**化学品：`check_chemical_compatibility` / `batch_safety_check` 在打后端之前
        # 先校验「至少 2 个」，只给 1 个的话请求根本走不到超时那条路 ——
        # 测试会「绿着但什么都没测」，而那正是本文件要防的形状。
        args[pname] = ["acetone", "bleach"] if pname == "chemicals" else "acetone"
    return args


@pytest.mark.parametrize("tool,takes_batch", _wrapped_tools())
def test_batch_hint_only_goes_to_tools_that_take_a_list(monkeypatch, tool, takes_batch):
    """CI-915 第二条：「拆成更少的化学品」只能发给**收列表**的工具。

    17 个被包住的工具里有 8 个根本不收 `chemicals`（`get_emergency_response` /
    `get_sds_document` / `get_audit_report` …）。对它们说这句话，就是 CI-915 要消灭的那种
    **听起来可行动、实际不适用**的建议，只是换了个地方犯——PR #50 第二轮 review 抓到。

    🔴 判据按**签名**推导，与实现用的是同一条规则但**各自独立计算**；
    覆盖面由 `_wrapped_tools()` 全量扫出来，新工具自动进来。
    """
    async def _timeout(*a, **kw):
        raise httpx.ReadTimeout("")

    for helper in [n for n in dir(server) if n.startswith("_direct_")] + ["_quick_chat"]:
        monkeypatch.setattr(server, helper, _timeout)

    params = CallToolRequestParams(name=tool, arguments={**_args_for(tool), "lang": "en"})
    res = asyncio.run(server.mcp._handle_call_tool(None, params))
    text = _text(res)
    assert res.is_error is True, f"{tool} 超时被报成了成功：{text!r}"

    hint = server._DIRECT_TIMEOUT_HINT_BATCH["en"].strip()
    if takes_batch:
        assert hint in text, f"{tool} 收列表却没拿到拆小建议：{text!r}"
    else:
        assert hint not in text, (
            f"{tool} 不收化学品列表，却被告知「拆成更少的化学品」——"
            f"这正是 CI-915 那类不适用的建议：{text!r}")


# ---------------------------------------------------------------- 文案能不能活着到调用方


def test_no_bare_runtimeerror_reaches_the_caller():
    """🔴 `server.py` 里不许再出现裸 `raise RuntimeError(...)` —— 用 `ToolReason`。

    **机制**（2026-09-15，PR #50 合进 main 后 Deploy 红换来的）：SDK 的 `Tool.run()` 把异常分两类，
    `ToolError` 子类＝「工具故意抛的」⇒ 文案随 `f"…: {exc}"` 保留；其它 `Exception` ＝「崩溃」
    ⇒ **mcp 2.2.0 起只回 `f"Error executing tool {name}"`，我们写的原因全部丢掉**。
    裸 `RuntimeError` 在 mcp 2.0.0 上**碰巧**带着文案，所以这个回归在旧版本上测不出来。

    🔴 **判据打在解析出来的代码上，不是文件文本上**：注释、docstring、字符串里写
    `raise RuntimeError` 都不该让这条红（本仓 [[my-own-guards-are-often-no-ops]]：
    散文与代码混在一个平面，grep 分不清）。
    🔴 **变异（两侧）**：把任意一处 `ToolReason` 改回 `RuntimeError` ⇒ 本条红；
    只在注释里写下 `raise RuntimeError(...)` ⇒ 本条**必须仍绿**。
    """
    import ast
    import inspect as _inspect

    tree = ast.parse(_inspect.getsource(server))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            fn = node.exc.func
            if isinstance(fn, ast.Name) and fn.id == "RuntimeError":
                offenders.append(node.lineno)
    assert not offenders, (
        f"server.py:{offenders} 抛了裸 RuntimeError —— 在 mcp>=2.2 上这些文案到不了调用方，"
        f"改用 ToolReason（它同时是 ToolError 与 RuntimeError）")
