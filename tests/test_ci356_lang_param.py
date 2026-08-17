"""CI-356：调用方给的 `lang` 必须真的进到发给后端的请求体里。

🔴 判据打在 **HTTP 边界捕获到的 payload** 上，不是「参数在不在 schema 里」，也不是
「工具函数收没收到这个形参」。这三件事是分开的，而**只有最后一件对用户有意义**：
schema 有参数、函数收下了、却在拼 payload 时仍然用服务端那个全局 `LANG`——这正是
CI-356 之前的状态（`"lang": LANG` 硬编码在 13 处）。同族：memory「修了，但没到达真正
的消费者」——这里真正的消费者是**后端**。

🔴 第二条判据同样重要：**没有后端支持的工具不许挂这个参数**。2026-08-15 逐端点实测
（同一入参跑 `lang=en` 与 `lang=zh` 比响应）：6 个端点认、6 个完全不认（en/zh 响应
逐字节等长、零中文）。给不认的工具挂上 `lang`＝在 schema 里承诺一件做不到的事，
而客户端无从分辨。等后端补齐再加，别提前写。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential

# 后端实测**认** lang 的端点所对应的工具 → 调用参数
SUPPORTED = {
    "check_chemical_compatibility": {"chemicals": ["a", "b"]},
    "get_chemical_risk_warnings": {"chemicals": ["a"]},
    "batch_safety_check": {"chemicals": ["a", "b"]},
    "get_storage_guidance": {"chemicals": ["a"]},
    "get_emergency_response": {"chemical": "a", "scenario": "spill"},
    "ask_chemical_safety": {"question": "q"},
    "get_chemical_alternatives": {"chemical": "a"},
    "validate_protocol_chemicals": {"protocol_text": "add acetone"},
    "check_mixing_order": {"chemical_a": "a", "chemical_b": "b"},
    "check_regulatory_lists": {"chemical": "a"},
    # 🔴 下面两条是 2026-08-17 从 UNSUPPORTED **搬上来**的——搬的依据是**重跑对比**，
    # 不是「后端说改好了」：
    #   get_ppe_recommendation：CI-361 第二步切片一让 P 码描述跟 lang 走（此前那 11 条
    #     描述是本模块自己的英文副本，所以 en/zh 逐字节等长）。
    #   get_sds_section：CI-361 第一步把 `no_section_text_note` 搬进 5 语言 catalog，
    #     Prod 实测 zh 与 en 的响应 md5 不同（此前返回的是 SDS 原文，确实无从翻译；
    #     现在**说明文字**这一段是我们自己的）。
    "get_ppe_recommendation": {"chemicals": ["a"]},
    "get_sds_section": {"chemical": "a", "section": 4},
}

# 后端实测**不认** lang 的端点 → 这些工具**不该**有 lang 参数。
# value = 实测证据，改这张表之前先重跑那个对比。
UNSUPPORTED = {
    "check_regulatory_compliance": "compliance：en/zh 均 372 字节、零中文",
    "search_msds_online": "online-search：en/zh 均 1599 字节、零中文",
    "get_transport_classification": "transport-classification：en/zh 均 325 字节、零中文",
    "get_waste_disposal": "waste-disposal：en/zh 均 792 字节、零中文",
}

# 🔴 **这张表是快照，不是事实**：它记的是「某天实测后端不认 lang」。后端每修好一个端点，
# 这里就多一条**过期证据**——而过期证据长得和有效证据一模一样。2026-08-17 一次就搬走了
# 两条（ppe / sds-section）。⇒ 改这张表之前**重跑那个对比**（同一入参、两种语言、比响应），
# 别照抄括号里的旧字节数。


class _Resp:
    status_code = 200
    headers: dict[str, str] = {}

    def raise_for_status(self): ...
    def json(self): return {"answer": "", "tool_results": [], "pairs": [], "warnings": [],
                            "results": [], "unresolved": [], "documents": [],
                            "compatibility": {}, "risk_warnings": [], "chemicals": []}


class _CapturingClient:
    """替掉 httpx.AsyncClient，把发出去的 json body 记下来。

    捕获点选在 HTTP 边界——再往上任何一层（工具函数 / `_direct_*` 的形参）都可能
    「收到了但没往下传」，那正是本票要修的 bug 形状。
    """

    def __init__(self, sent: list):
        self._sent = sent

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, headers=None, **kw):
        self._sent.append((url, json or {}))
        return _Resp()

    async def get(self, url, headers=None, **kw):
        self._sent.append((url, {}))
        return _Resp()


@pytest.fixture
def sent(monkeypatch):
    box: list = []
    monkeypatch.setattr(server.httpx, "AsyncClient", _CapturingClient(box))
    set_caller_credential("sk-msds-test")
    yield box
    set_caller_credential(None)


@pytest.mark.parametrize("tool", sorted(SUPPORTED))
def test_caller_lang_reaches_the_backend_payload(sent, tool):
    asyncio.run(getattr(server, tool)(**SUPPORTED[tool], lang="zh"))
    langs = [body.get("lang") for _, body in sent if "lang" in body]
    assert langs, f"{tool} 一次带 lang 的后端请求都没发出去（捕获到 {len(sent)} 个请求）"
    assert all(l == "zh" for l in langs), (
        f"{tool} 把 lang 发成了 {langs} —— 调用方给的值没到后端，"
        f"检查 payload 里是不是还写着硬编码的 `LANG`"
    )


@pytest.mark.parametrize("tool", sorted(SUPPORTED))
def test_omitting_lang_falls_back_to_server_default(sent, tool):
    """不传就用服务端默认（英文）——CI-258 定的兜底语义，不该因为加了参数而改变。"""
    asyncio.run(getattr(server, tool)(**SUPPORTED[tool]))
    langs = [body.get("lang") for _, body in sent if "lang" in body]
    assert langs and all(l == server.LANG for l in langs), f"{tool} 不传 lang 时发出了 {langs}"


def test_tools_without_backend_support_do_not_advertise_lang():
    """后端不认的工具不许挂 lang —— schema 不能承诺做不到的事。"""
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    wrong = [n for n in UNSUPPORTED
             if "lang" in (tools[n].input_schema or {}).get("properties", {})]
    assert not wrong, (
        f"这些工具的后端端点实测忽略 lang，却在 schema 里挂了这个参数：{wrong}。"
        f"证据见本文件 UNSUPPORTED 表；要加请先让后端支持并重跑那个对比"
    )


def test_supported_tools_all_advertise_lang():
    """反过来也要钉住：后端支持了却漏挂参数 ⇒ 用户依然无法表达语言。"""
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    missing = [n for n in SUPPORTED
               if "lang" not in (tools[n].input_schema or {}).get("properties", {})]
    assert not missing, f"这些工具的后端认 lang，但工具没暴露参数：{missing}"


def test_lang_is_documented_and_not_a_hard_enum():
    """描述必须列出支持的语言码；但**故意不做 `Literal`**。

    与 CI-521 给 `scenario` 加 enum 相反：`scenario` 传错后端硬拒（没有答案），
    而语言有明确的英文兜底 ⇒ 把 `"zh-CN"` 打成参数校验错误、整次调用失败，
    对一个装饰性参数来说代价过高。

    🔴 **`enum` 要递归找**：这条初版写成 `assert "enum" not in schema`，而
    `Literal[...] | None` 生成的是 `anyOf: [{"enum": [...]}, {"type": "null"}]`
    —— `enum` 根本不在顶层。反向变异（把 `Lang` 改成 `Literal`）时这条**照样绿**，
    是个空跑。空跑的守卫比没有守卫更糟：它让人以为这个决定被钉住了。
    """
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = (tools["ask_chemical_safety"].input_schema or {})["properties"]["lang"]
    desc = schema.get("description") or ""
    assert all(code in desc for code in server._BACKEND_LANGS), f"描述没列全语言码：{desc}"

    def _has_enum(node) -> bool:
        if isinstance(node, dict):
            return "enum" in node or any(_has_enum(v) for v in node.values())
        if isinstance(node, list):
            return any(_has_enum(v) for v in node)
        return False

    assert not _has_enum(schema), f"lang 不该是硬枚举——见本用例 docstring。schema={schema}"


@pytest.mark.parametrize("given,expected", [
    ("zh", "zh"), ("ZH", "zh"), (" zh ", "zh"),
    ("en", "en"), (None, "en"), ("", "en"),
    # 🔴 这三条是本票最重要的断言。后端对**任何非 "en" 的值**都返回中文（2026-08-15 实测
    # `ja`/`de`/`id`/`fr`/`zh-CN`/空串全部→中文，quick-chat 亦然）。不归一的话，一个日语
    # 用户会拿到**中文**——比现在的英文更糟：看不懂，还误以为我们支持日语。
    ("ja", "en"), ("de", "en"), ("zh-CN", "en"),
])
def test_unsupported_languages_normalize_to_english_not_chinese(given, expected):
    assert server._normalize_lang(given) == expected


def test_supported_set_matches_what_the_backend_actually_does():
    """🔴 往 `_BACKEND_LANGS` 加语言的判据是**实测那个语言真的出来了**，
    不是后端文档说支持、更不是往元组里加一行。这条钉住当前实测结论。"""
    assert server._BACKEND_LANGS == ("en", "zh"), (
        "改了支持集合？请先重跑逐语言实测（同一入参跑各 lang 比响应），"
        "确认新语言真的产出该语言的文本，再改这里和参数描述"
    )


def test_normalized_value_is_what_hits_the_wire(sent):
    """归一化必须发生在**发出去之前**——在工具层归一、在 payload 里又用原值等于没归一。"""
    asyncio.run(server.get_chemical_risk_warnings(chemicals=["a"], lang="ja"))
    langs = [b.get("lang") for _, b in sent if "lang" in b]
    assert langs and all(l == "en" for l in langs), f"lang=ja 应归一成 en，实际发出 {langs}"
