"""CI-1133：`language_note` 必须出现在**文本**里，不能只躺在 structuredContent。

上游 CI-1123 让 `/api/v2` 在这一轮确实读过「只有 en/zh 两份」的文案表时回一个顶层
`language_note`（`lang=en`/`zh` 永远没有这个键）。`_expose()` 默认全透 ⇒ 它**已经在**
structuredContent 里；而模型与多数客户端读的是 `TextContent`，那是各工具逐字段显式拼的
⇒ 没人提的键永远不出现。同族 CI-553（`precursor_disclosure`）/ CI-360
（`insufficient_reason`）——都是「后端这半完成了，而修好的东西一个用户也到不了」。

🔴 判据落在**我们返回的那串文本**上，不落在 structuredContent，也不落在「客户端会不会
念出来」：CI-1096 已证明客户端模型会重写我们给的任何具体字符串，所以能被我们守住的
只有「这句话逐字在我们发出去的文本里」。

🔴 **成员自己发现**（照抄 `test_preparation_tail_not_exposed.py` 的判据形状）：
1. **替身放在 HTTP 层**，不替换 `_direct_*` —— 实现挂在 `_reported` 上，而
   `_direct_*` 在它里面；把 `_direct_*` 整个换掉仍然测得到，但换 HTTP 层才和真货同形
   （载荷从 `_billed_json` 进来，`_drop_tail_keys` 这类入口处理也一起走到）。
2. **按 `mcp.list_tools()` 全量扫**，新工具自动进来。
3. **「跑到了」＝发出过 HTTP 请求**，不是「没抛异常」：SDK 对缺参数的调用是**返回**
   `is_error` 而不是抛 ⇒ 用 try/except 计数会把一个从没走到后端的工具计进覆盖面，
   并顺理成章地「通过」。

## 🔴 变异记录（没记变异的守卫默认当它不存在）

- **把 `_relay_language_note(...)` 从 `_reported` 里撤掉**（改回 `result = await fn(...)`）
  ⇒ `test_note_reaches_the_text_of_every_tool_that_carries_it` 红，实测 **17 个**工具的
  文本面漏掉那句话（＝载荷里带了它的那 17 个，一个不剩）。**带上修复 ⇒ 0 个。**
  🔴 变异生效的判据打在**派生条件**上，不是「文件改了」：`ast` 解析 `_reported` 的
  源码，断言 `_relay_language_note` 不再出现在**调用**集合里 —— 我第一次写的是
  `'_relay_language_note' in inspect.getsource(...)`，那条**恒为真**，因为同名字串还
  留在我刚写的那条注释里（[[mutation-that-did-not-land]] 的标准形状）。
- **把 `if note in content[0].text: return result` 那行删掉** ⇒
  `test_relay_is_idempotent` 红（同一句话播两遍）。
- **把 `check_regulatory_compliance` 里那行 `**_language_note_of(results)` 删掉** ⇒
  同一条测试红，但**只被点名那两个断言抓到**：它掉出 `carried` 之后
  `assert not missing` 是绿的（它压根不在集合里），`len(carried) >= 10` 也绿（17→16）。
  🔴 这就是为什么那里要点名 —— 一条「条件成立才检查」的不变式，**对「条件不再成立」失明**。
- 🔴 **被替换掉的那条守卫，它的变异记录也一起换掉了**（改测试留旧记录 ＝ 让人以为某个
  场景有验过的守卫）：原来这里有一条「lang 还被夹着 ⇒ 本中继 inert」的前向红线，
  CI-1095 放开 catalog 族之后它**该红却全绿**（它钉的是 quick-chat 族的符号，而那一族
  没动）。现在换成 `test_catalog_lang_is_forwarded_so_the_relay_can_actually_fire`，
  变异＝把 `_direct_risk` 的 `_normalize_catalog_lang` 换回 `_normalize_lang` ⇒ 它红。
- 🔴 变异方向是「**集合变大**」而不是「撤回改动」：往 `_PAYLOAD` 里加一个**新工具也会
  读到的**载荷键测不出任何东西；真正该造的变异是**加一个新工具**（它应当自动出现在
  覆盖面里）。本文件按 `list_tools()` 发现成员，所以这条是结构性成立的，不是我声称的。
"""
import asyncio
import inspect

import pytest

import server
from mcp.types import CallToolRequestParams
from request_identity import set_caller_credential

# 后端 `messages.partial_english_fallback` 的 ja 那一列，逐字。
# 🔴 **刻意用真文案而不是 `"NOTE"` 这种哨兵**：这句话里有非 ASCII、有句读，而
# 「文本里逐字出现」正是本票的判据 —— 用哨兵串会让编码/转义类的失败静默通过。
_NOTE = "この回答の一部は現時点で英語のみのため、その部分は英語で表示しています。"

_ITEM = {"chemical_name": "acetone", "cas": "67-64-1", "level": "high",
         "chemical": "acetone", "description": "flammable", "storage_class": "general"}
# 每个容器都塞一份：这里不是在模拟真实载荷的形状，而是在问「后端把 `language_note`
# 放在顶层时，它会不会到达文本面」。顶层那一份才是被测的那个。
_PAYLOAD = {
    "results": [_ITEM], "chemicals": [_ITEM], "warnings": [_ITEM], "pairs": [_ITEM],
    "risk_warnings": [_ITEM], "source_info": dict(_ITEM), "sections": [_ITEM],
    "alternatives": [_ITEM], "tool_results": [_ITEM],
    "answer": "x", "url": "https://example.invalid/y.pdf", "session_id": "S1",
    "language_note": _NOTE, **_ITEM,
}

# 覆盖每个工具的全部必填参数。漏一个的后果不是「那个工具没测到」，是更糟的一种：
# SDK 在参数校验阶段返回 `is_error`（不抛）⇒ 那个工具被计进覆盖面并理所当然地「通过」。
_ARGS = {
    "chemicals": ["acetone", "bleach"], "chemical": "acetone", "session_id": "S1",
    "question": "hazards?", "protocol": "mix a and b", "section": 4,
    "url": "https://example.invalid/y.pdf", "file_path": "/tmp/x.pdf",
    "region": "EU", "days": 7, "scenario": "spill",
    "experiment_name": "probe run", "query": "acetone", "protocol_text": "mix a then b",
    "chemical_a": "acetone", "chemical_b": "bleach",
    "pdf_source": "data:application/pdf;base64,JVBERi0xLjQK",
    "ingredients": [{"chemical": "acetone", "concentration": "60%"}],
}

_HTTP_HITS = {"n": 0}
_BODY = {"payload": _PAYLOAD}


class _Resp:
    status_code = 200
    headers: dict = {}

    def json(self):
        return _BODY["payload"]

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self, *a, **k): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, *a, **k):
        _HTTP_HITS["n"] += 1
        return _Resp()

    async def get(self, *a, **k):
        _HTTP_HITS["n"] += 1
        return _Resp()


@pytest.fixture(autouse=True)
def _harness(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(server.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(server, "_log_call", _noop)
    set_caller_credential("sk-msds-test")
    yield
    set_caller_credential(None)
    _BODY["payload"] = _PAYLOAD


def _text_of(res) -> str:
    blocks = getattr(res, "content", None) or []
    return "\n".join(t for b in blocks if (t := getattr(b, "text", None)))


def _call_every_tool():
    """→ (真打过后端的, 载荷里带 note 的, 带了 note 却没进文本的, 一次 HTTP 都没发的)。

    🔴 「跑过了」＝ `_HTTP_HITS` 真的涨了，不是「没抛异常」。
    """
    reached, carried, missing, never = [], [], [], []
    for tool in asyncio.run(server.mcp.list_tools()):
        fn = getattr(server, tool.name, None)
        if fn is None:
            continue
        required = [p for p, v in inspect.signature(fn).parameters.items()
                    if v.default is v.empty]
        args = {p: _ARGS[p] for p in required if p in _ARGS}
        before = _HTTP_HITS["n"]
        try:
            res = asyncio.run(server.mcp._handle_call_tool(
                None, CallToolRequestParams(name=tool.name, arguments=args)))
        except Exception:
            res = None
        if _HTTP_HITS["n"] == before:
            never.append((tool.name, [p for p in required if p not in _ARGS]))
            continue
        reached.append(tool.name)
        if res is None:
            continue
        sc = res.structured_content
        if isinstance(sc, dict) and sc.get("language_note") == _NOTE:
            carried.append(tool.name)
            if _NOTE not in _text_of(res):
                missing.append(tool.name)
    return reached, carried, missing, never


def test_every_tool_actually_reaches_the_backend():
    """覆盖面自检：后面几条都是「没找到就绿」，先钉住「真的跑到了」。"""
    reached, _, _, never = _call_every_tool()
    assert not never, f"这些工具一次 HTTP 都没发（缺参数）：{never} —— 它们的『通过』是假的"
    assert len(reached) >= 20, f"只有 {len(reached)} 个工具打到后端：{reached}"


def test_note_reaches_the_text_of_every_tool_that_carries_it():
    """🔴 本票的判据：载荷里带了那句话，文本面就必须**逐字**有它。

    条件写成「structuredContent 里带了 ⇒ 文本里也要有」而不是写死一张工具清单：
    清单会在第 24 个工具出生时静默过期，而这个条件不会。
    """
    reached, carried, missing, never = _call_every_tool()
    assert not never, f"覆盖面有洞（见上一条）：{never}"
    assert not missing, (
        f"这些工具把 language_note 留在了 structuredContent 里、没进文本：{missing}。"
        f"模型与多数客户端读的是 TextContent —— 留在结构化里等于用户看不见，"
        f"而他会以为是自己把 lang 传错了（CI-1123 要消灭的正是这个误解）。")
    # 🔴 没有这一条，上面那句 `assert not missing` 在 `carried` 为空时是绿的空跑，
    # 而「空跑」与「全都通过了」完全同形。
    assert len(carried) >= 10, (
        f"只有 {len(carried)} 个工具把 language_note 带进了 structuredContent："
        f"{carried} —— 覆盖面塌了，这条守卫在自证空气")
    # 🔴 **点名这两个是有理由的，不是随手挑的清单**：它们背后的两个端点
    # （`/api/v2/compliance` · `/api/v2/risk-warnings`）是 CI-1123 在 Prod 上量到的、
    # 确实会播那句话的那两个（复测过）。而 `check_regulatory_compliance`
    # 的 structuredContent 是**手拼白名单**（CI-342 那条形状）⇒ 它掉出覆盖面时
    # 上面那条 `assert not missing` **是绿的**（它压根不在 `carried` 里），
    # `len(carried) >= 10` 也照样绿（17→16）。**只有点名才抓得住。**
    for pinned in ("check_regulatory_compliance", "get_chemical_risk_warnings"):
        assert pinned in carried, (
            f"{pinned} 没把 language_note 带进 structuredContent —— 它是 CI-1123 在 Prod "
            f"上验过会播那句话的端点之一，掉出去等于最该说话的工具正好是哑的。"
            f"手拼白名单的工具要显式 `**_language_note_of(...)`。")


def test_no_tag_when_the_backend_did_not_send_the_note():
    """反方向：后端没播（`lang=en`/`zh`，或这一轮压根没读二值表）⇒ 一个字节都不许改。

    🔴 说错的方向是**对英文用户说「有部分内容是英文」**——一句多余的废话，但它会让
    我们看起来不知道自己在返回什么。后端那一层已经为同一件事做了入口清账。
    """
    _BODY["payload"] = {k: v for k, v in _PAYLOAD.items() if k != "language_note"}
    reached, carried, _, never = _call_every_tool()
    assert not never, f"覆盖面有洞：{never}"
    assert not carried, f"载荷里没有 language_note，却有工具报告带了它：{carried}"
    leaked = []
    for tool_name in reached:
        fn = getattr(server, tool_name)
        args = {p: _ARGS[p] for p, v in inspect.signature(fn).parameters.items()
                if v.default is v.empty and p in _ARGS}
        res = asyncio.run(server.mcp._handle_call_tool(
            None, CallToolRequestParams(name=tool_name, arguments=args)))
        if "[language]" in _text_of(res):
            leaked.append(tool_name)
    assert not leaked, f"后端没播那句话，这些工具却自己播了：{leaked}"


def test_relay_is_idempotent():
    """渲染器哪天自己渲染了这句话时，不许再播一遍。

    后端那一层用的是 `setdefault`（handler 自己说的更具体的话优先）；这里的幂等是
    同一条口径在 MCP 侧的对应物。
    """
    from mcp.types import CallToolResult, TextContent
    already = CallToolResult(
        content=[TextContent(type="text", text=f"**Risk Warnings**\n\n{_NOTE}\n")],
        structured_content={"language_note": _NOTE},
    )
    out = server._relay_language_note(already)
    assert _text_of(out).count(_NOTE) == 1, "同一句披露被播了两遍"
    assert "[language]" not in _text_of(out)


def test_relay_leaves_non_text_and_noteless_results_alone():
    """🔴 三种「不该动」的输入，逐个钉住 —— 它们都是**静默**失败的入口。"""
    from mcp.types import CallToolResult, TextContent
    text_only = CallToolResult(content=[TextContent(type="text", text="body")])
    assert server._relay_language_note(text_only) is text_only, \
        "没有 structuredContent 也去改文本 ⇒ 会给纯文本工具凭空加一段披露"
    empty_note = CallToolResult(
        content=[TextContent(type="text", text="body")],
        structured_content={"language_note": ""})
    assert server._relay_language_note(empty_note) is empty_note, \
        "空字符串是「没有」不是「有一句空话」"
    assert server._relay_language_note("plain string") == "plain string", \
        "返回裸字符串的工具不该被这一层动到"


def test_catalog_lang_is_forwarded_so_the_relay_can_actually_fire():
    """🔴 这条**替换**了原来那条「lang 还被夹着，所以本中继是 inert 的」前向红线。

    本文件刚写下时，`/api/v2` 收到的 `lang` 被夹成 `en|zh`，而后端只对 en|zh **之外**
    的语言播 `language_note` ⇒ 两个条件互斥，中继一个用户也够不到。CI-1095 把
    catalog 族放开成五种之后，那个互斥消失了 —— 本条钉住**放开的方向不许被退回去**：
    退回去的表现不是报错，是这一整个文件的守卫**全绿而中继永远不触发**。

    🔴🔴 **原来那条红线没能在该红的时刻红，原因值得记下来**：它断言的是
    `_normalize_lang("ja") == "en"` 和 `_BACKEND_LANGS == ("en", "zh")` ——
    而 CI-1095 是**新增了 catalog 族**，那两个符号描述的 quick-chat 族**一个字没动**
    ⇒ 红线全绿，而它存在的唯一理由正好在那一刻兑现了。
    **教训：前向红线要钉在「被解除的那个条件」上，不是钉在「我当时手边的那个符号」上。**
    这里改成直接问被测通道本身：catalog 族发出去的 `lang` 是不是原样的 `ja`。
    """
    sent = []

    class _C:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url=None, *a, json=None, **k):
            sent.append((str(url), (json or {}).get("lang")))
            return _Resp()

        async def get(self, url=None, *a, **k):
            sent.append((str(url), None))
            return _Resp()

    orig = server.httpx.AsyncClient
    server.httpx.AsyncClient = _C
    try:
        asyncio.run(server.get_chemical_risk_warnings(chemicals=["acetone"], lang="ja"))
    finally:
        server.httpx.AsyncClient = orig

    v2 = [lg for u, lg in sent if "/api/v2/" in u]
    assert v2, f"没抓到任何 /api/v2 请求（抓到 {sent}）—— 先查这个探针，别读成「通过」"
    assert all(lg == "ja" for lg in v2), (
        f"catalog 族把 lang 夹掉了（发出 {v2}）⇒ 后端不会播 language_note，"
        f"本文件所有守卫都会变成永远不触发的绿灯。见 CI-1095。")


def test_relay_keeps_the_error_flag():
    """`is_error` 必须带过去：重建响应时丢掉它 ⇒ 一次失败被报成成功。"""
    from mcp.types import CallToolResult, TextContent
    failed = CallToolResult(
        content=[TextContent(type="text", text="backend said no")],
        structured_content={"language_note": _NOTE},
        is_error=True,
    )
    assert server._relay_language_note(failed).is_error is True
