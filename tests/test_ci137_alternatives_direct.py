"""CI-137：`get_chemical_alternatives` 不再走 quick-chat 的三轮 LLM。

Prod 实测（2026-08-16，近 30 天 `mcp_call_logs.duration_ms`）：走 quick-chat 的工具
p50 **7–11 秒**，`/api/v2` 直连的 0.3–0.4 秒，**中间没有过渡带**。而后端
`agent/tools/chemical_substitution.py` 早就是确定性实现（curated 替代表 + `resolve_cas`
+ GHS 风险比较，**全文件零 LLM 引用**）——同 [[CI-523]] 一族：信息在，这条通道没去拿。

🔴 **fixture 的字段名照后端抄**（`chemical_substitution.py` 的 `alternatives.append`）：
`cas` / `name` / `risk_level` / `rationale` / `trade_offs`。初版我按想当然写了
`reason` / `trade_off`——那正是 [[CI-529]] 当天刚栽过的「键名是我编的、fixture 也是我编的，
于是互相自证」。改 fixture 前先回那个文件看一眼。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential

_fmt = server._format_alternatives

_REAL_SHAPE = {
    "chemical": "benzene",
    "cas_number": "71-43-2",
    "risk_level": "High (H350 carcinogen)",
    "source_info": {"supplier": "PANREAC", "revision_date": "2023-05-24"},
    "alternatives": [{
        "cas": "141-78-6",
        "name": "Ethyl acetate",
        "risk_level": "Lower hazard (verify with SDS before use)",
        "rationale": "Not a known carcinogen; lower chronic toxicity",
        "trade_offs": "Still flammable; suitable for polar-to-moderately-polar applications",
    }],
    "note": "Substitution recommendations are based on common hazard reduction practices…",
}


def test_renders_the_real_backend_fields():
    out = _fmt(_REAL_SHAPE, "benzene")
    assert "Ethyl acetate" in out and "141-78-6" in out
    assert "Not a known carcinogen" in out, "rationale 没渲染出来"
    assert "Still flammable" in out, "trade_offs 没渲染出来"


def test_three_things_must_appear_in_the_text_not_only_in_structured_content():
    """🔴 这三样只留在 structuredContent 等于没说：

    - `note`：curated 表的**边界**（我们的建议基于通用减害实践，不是针对你的工艺）
    - 原物质的 `risk_level`：替代建议的**前提**——不知道原来多危险，「更安全」没有意义
    - `source_info`：CI-65 定的可追溯性红线（供应商 + 版本）
    """
    out = _fmt(_REAL_SHAPE, "benzene")
    assert "hazard reduction practices" in out, "note 丢了"
    assert "H350" in out, "原物质的风险等级丢了"
    assert "PANREAC" in out and "2023-05-24" in out, "来源丢了"


def test_unresolved_chemical_says_so_instead_of_pretending():
    out = _fmt({"chemical": "zzz", "cas_number": "", "alternatives": [], "note": ""}, "zzz")
    assert "not resolved" in out, out
    assert "No alternative" in out, out


class _Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self): ...

    def json(self):
        return self._p


class _Capturing:
    def __init__(self, sent, payload):
        self._sent, self._payload = sent, payload

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        self._sent.append((url, json or {}))
        return _Resp(self._payload)


@pytest.fixture
def sent(monkeypatch):
    box: list = []
    monkeypatch.setattr(server.httpx, "AsyncClient", _Capturing(box, _REAL_SHAPE))

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(server, "_log_call", _noop)
    set_caller_credential("sk-msds-test")
    yield box
    set_caller_credential(None)


def test_tool_calls_the_deterministic_endpoint_never_quick_chat(sent):
    """🔴 正题。判据打在**实际发出的 URL** 上，不是「代码里有没有那个函数名」。"""
    asyncio.run(server.get_chemical_alternatives("benzene", use_case="degreasing"))
    urls = [u for u, _ in sent]
    assert all("/quick-chat" not in u for u in urls), f"还在走 quick-chat：{urls}"
    assert any("/api/v2/chemical-alternatives" in u for u in urls), urls


def test_use_case_reaches_the_backend(sent):
    """`use_case` 是调用方给的上下文，必须真的传到后端——不然它只是个装饰参数。"""
    asyncio.run(server.get_chemical_alternatives("benzene", use_case="degreasing"))
    bodies = [b for _, b in sent if "chemical" in b]
    assert bodies and bodies[0].get("use_case") == "degreasing", bodies


def test_keyless_call_is_refused_not_answered_anonymously(monkeypatch):
    """换端点不该顺带改访问控制：老路径（quick-chat）要求凭证，新路径也要。
    （CI-523 踩过一次，这里是同一形状。）"""
    called: list = []

    async def _should_not_run(*a, **kw):
        called.append(1)
        return {}

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(server, "_direct_alternatives", _should_not_run)
    monkeypatch.setattr(server, "_log_call", _noop)
    set_caller_credential(None)
    res = asyncio.run(server.get_chemical_alternatives("benzene"))
    assert "Authentication required" in res.content[0].text, res
    assert not called, "没有凭证却还是打了后端"
