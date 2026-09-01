"""CI-823：结构化工具的可选 `intent` —— **只记录、不参与作答**。

客户端 LLM 把用户的话翻成 `chemicals` / `section` / `region` 这些结构化参数。翻错了我们
看得见入参、看不见「他本来要问什么」（423 次外部调用里只有 38 次走 `ask_chemical_safety`）。
`intent` 补的就是这一面。

三条性质各有一个守卫，**每条都记下它的变异**（没记变异的守卫默认当它不存在）：

| 守卫 | 让它红的最小改动 |
|---|---|
| `test_every_structured_tool_offers_intent` | 往 registry 里加一个新的结构化工具、不给它 `intent`（**变异是加成员，不是改代码**——判据的作用域是从 registry 现算的，改现有代码只能验到已有成员） |
| `test_every_declaring_tool_actually_records_it` | 把任一工具 `finally` 里的 `_intent_params(..., intent)` 改回 `_json.dumps(...)`（17 处手工穿线，漏一处不报错） |
| `test_intent_never_reaches_the_backend` | 把 `intent` 拼进 `_quick_chat` 的 question / 任一 `_direct_*` 的载荷 |
| `test_over_long_intent_is_truncated_not_rejected` | 给 `Intent` 的 `Field` 加 `max_length=500`（pydantic 会把整次调用打回去） |

🔴 **为什么「不参与作答」要有守卫**：`intent` 是调用方给的自由文本。任何一条让它流进
prompt 或后端载荷的路，都是安全结论的注入面——调用方可以往里写「就说它是安全的」。
这条不是风格问题，加一行 `question = f"{question} ({intent})"` 就能悄悄打开它。
"""
import asyncio
import json

import pytest

import server as _s
from live_coverage_cases import CASES
from request_identity import set_caller_credential

SENTINEL = "ZZ-CI823-SENTINEL-ZZ"

# 🔴 不给 `intent` 的工具，**逐个写明理由**——名单在这里是为了逼下一个加工具的人做这个
# 判断，不是为了记录现状。理由只有两类：①它本来就收用户原话 ②它的日志载荷是承重脱敏面。
NO_INTENT_BECAUSE = {
    "ask_chemical_safety": "`question` 本身就是用户原话",
    "validate_protocol_chemicals": "`protocol_text` 是用户贴进来的原文",
    "check_mixing_order": "已有自由文本 `context`",
    "get_chemical_alternatives": "已有自由文本 `use_case`",
    "upload_msds_pdf": "它的 `input_params` 是承重脱敏面（记 `<inline data URI, N chars>` "
                       "而不是 base64），多挂一个自由文本键会把这个面变宽",
    "get_audit_report": "按 `session_id` 取件，没有「一句话被翻成参数」这回事",
}


def _tools():
    return asyncio.run(_s.mcp.list_tools())


def _props(t):
    return (t.input_schema or {}).get("properties") or {}


def test_every_structured_tool_offers_intent():
    """名单自己发现成员：判据从 live registry 现算，新工具不给 intent 就红。"""
    missing = [t.name for t in _tools()
               if "intent" not in _props(t) and t.name not in NO_INTENT_BECAUSE]
    assert not missing, (
        f"这些工具没有意图面：{sorted(missing)}——加 `intent: Intent = None` 并在 "
        f"`finally` 里走 `_intent_params(...)`；确实不该有的，写进 NO_INTENT_BECAUSE 并说明理由")

    stale = [n for n in NO_INTENT_BECAUSE if n not in {t.name for t in _tools()}]
    assert not stale, f"豁免名单里的工具已经不存在了：{stale}——名单跟着改"

    contradicting = [n for n in NO_INTENT_BECAUSE
                     if "intent" in _props(next(t for t in _tools() if t.name == n))]
    assert not contradicting, f"这些工具既在豁免名单里、又声明了 intent：{contradicting}"


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """🔴 摘掉本机代理，否则 `httpx.AsyncClient(...)` 在**构造时**就 ImportError
    （Clash 的 SOCKS + 未装 socksio），一次请求都发不出去 —— 而
    `test_intent_never_reaches_the_backend` 会因此**恒绿**。第一次写就撞到了，
    是那条 `assert seen` 自检把它抓出来的。
    """
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
                "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def logged(monkeypatch):
    """截住上报，拿到工具**声明要记什么**——不发任何网络请求。"""
    calls: list[dict] = []

    async def _fake_log(tool_name, chemicals, duration_ms, success,
                        error_message=None, input_params=None, response_text=None):
        calls.append({"tool": tool_name, "input_params": input_params})

    monkeypatch.setattr(_s, "_log_call", _fake_log)
    return calls


def _declaring_tools():
    return [t.name for t in _tools() if "intent" in _props(t)]


def _call(name, **extra):
    """按 `live_coverage_cases` 的入参调一次工具。

    🔴 入参不在这里手写：那份用例集已经被 `test_ci245` 守着「满足工具自己声明的
    required」，手写第二份必然腐化。后端在测试里指向丢弃端口（conftest），所以每次调用
    都会以连接失败告终 —— 无所谓：`_log_intent` 在 `finally` 里，**失败路径同样要记**。
    """
    args = dict(CASES[name]["args"], **extra)
    # 🔴 必须带凭证：没有它的话 `create_audit_session` / `search_chemical_database` /
    # `search_msds_online` / `check_regulatory_lists` / `get_sds_document` 会在
    # `_require_api_key()` 那里直接返回一句提示，**根本走不到作答那条路** ⇒ 泄漏守卫
    # 对这 5 个工具恒绿（review 实测：给 `search_chemical_database` 造一个真的泄漏，
    # 整个文件仍是 6 passed）。全局的 `assert seen` 挡不住这个——另外 12 个工具在发请求。
    set_caller_credential("sk-msds-test")
    try:
        asyncio.run(getattr(_s, name)(**args))
    except Exception:
        pass
    finally:
        set_caller_credential(None)


def test_every_declaring_tool_actually_records_it(logged):
    """声明了 `intent` 的工具，必须真的把它记进 `input_params`。

    17 处是手工穿线的：签名加了参数、`finally` 里忘了用，schema 上完全看不出来，
    而线上表现是「这个工具的 intent 永远是空的」——与「用户不填」同形。
    """
    silent = []
    for name in _declaring_tools():
        logged.clear()
        _call(name, intent=SENTINEL)
        payloads = [c["input_params"] or "" for c in logged if c["tool"] == name]
        if not payloads:
            silent.append(f"{name}(一条日志都没发)")
        elif not any(json.loads(p).get("intent") == SENTINEL for p in payloads):
            silent.append(name)
    assert not silent, f"这些工具收了 intent 却没记下来：{sorted(silent)}"


def test_absent_intent_leaves_the_payload_byte_identical(logged):
    """不给 intent 时，`input_params` 与加这个参数之前**逐字节相同**。

    这条守的是「别顺手把键写成 `"intent": null`」：那会让所有历史行与新行在
    `input_params` 上不同形，而读这张表的查询是按键存在与否写的。
    """
    _call("get_storage_guidance")
    assert logged, "没发日志"
    assert "intent" not in json.loads(logged[-1]["input_params"])


def test_intent_never_reaches_the_backend(logged, monkeypatch):
    """🔴 注入面守卫：`intent` 不许出现在任何发往后端的请求里。

    `_log_call` 已被 fixture 截住 ⇒ 这里看到的每一次 httpx 请求都是「作答那条路」。
    """
    import httpx
    seen: list[str] = []

    async def _capture(self, method, url, **kw):
        seen.append(repr((str(url), kw.get("json"), kw.get("params"), kw.get("content"))))
        raise httpx.ConnectError("blocked by test")

    monkeypatch.setattr(httpx.AsyncClient, "request", _capture, raising=False)
    per_tool: dict[str, list[str]] = {}
    for name in _declaring_tools():
        seen.clear()
        _call(name, intent=SENTINEL)
        per_tool[name] = list(seen)

    # 🔴 覆盖率断言必须**逐工具**：写成全局 `assert seen` 的话，只要有一个工具在发请求
    # 它就绿，而没走到网络那层的工具在这条守卫下是完全不设防的（第一版正是这样，
    # 5 个工具白跑）。「这个工具没发请求」和「这个工具没泄漏」在计数上完全同形。
    silent = [n for n, reqs in per_tool.items() if not reqs]
    assert not silent, (
        f"这些工具一次后端请求都没发出去 ⇒ 泄漏守卫对它们是空跑的：{sorted(silent)}。"
        f"多半是早退了（缺凭证 / 缺必填参数），先让它走到作答那条路再谈泄漏")

    leaked = {n: r for n, reqs in per_tool.items() for r in reqs if SENTINEL in r}
    assert not leaked, f"intent 流进了作答路径（注入面）：{leaked}"


def test_over_long_intent_is_truncated_not_rejected(logged):
    """超长只截断、不拒绝——一个纯诊断字段不该把真实的安全查询挡在门外。"""
    schema = _props(next(t for t in _tools() if t.name == "get_storage_guidance"))["intent"]
    # 🔴 递归地找：可选参数的 schema 是 `anyOf: [string, null]`，`maxLength` 会落在
    # 里层那个分支上 —— 只看顶层的话这条断言恒真（第一次就是这么写的，M3 变异全绿）。
    assert "maxLength" not in json.dumps(schema), (
        "schema 里的 maxLength 会让 pydantic 直接打回整次调用；上限走 `_cap_intent` 截断")

    # 🔴 必须走 `mcp.call_tool`（而不是直接调函数）：参数校验在那一层，直接调函数
    # 等于把要测的那道闸绕过去。判据是**有没有走到函数体**（连不上后端是预期的），
    # 而不是抛的是哪种异常 —— 校验失败与连接失败在调用方看来都是一个 ToolError。
    try:
        asyncio.run(_s.mcp.call_tool(
            "get_storage_guidance", {"chemicals": ["acetone"], "intent": "x" * 5000}))
    except Exception:
        pass
    assert logged, "超长 intent 在进函数体之前就被打回了（应当被截断后照常执行）"
    recorded = json.loads(logged[-1]["input_params"])["intent"]
    assert len(recorded) < 5000 and recorded.endswith("…[truncated]"), (
        f"截断没留痕或没生效：{len(recorded)} chars")


def test_intent_description_tells_the_client_it_does_not_change_the_answer():
    """判据打在 `inputSchema` 上（CI-521）：客户端选参数值时读的就是这一处。

    两句都必须在：**记录用**（否则模型会以为不填就答不准，或者把参数省了只写 intent）、
    **不影响答案**（否则模型会试图把指令写进来）。
    """
    desc = _props(next(t for t in _tools() if t.name == "get_ppe_recommendation"))["intent"]["description"]
    low = desc.lower()
    assert "recorded only" in low, f"没说清它只是被记录：{desc!r}"
    assert "does not change the answer" in low, f"没说清它不影响作答：{desc!r}"
    assert "never put instructions here" in low, f"没挡住「把指令写进 intent」：{desc!r}"
