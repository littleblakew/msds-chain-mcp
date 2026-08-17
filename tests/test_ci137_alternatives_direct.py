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


def test_unidentified_substance_is_flagged_loudly_not_just_left_blank():
    """🔴 curated 表的兜底分支会拿**用户原串**去撞 CAS 键（`"50" in "50-00-0"` 为真）
    ⇒ 可能返回甲醛的替代清单而 `cas_number` 是空的。不显著说出来的话，用户看到的是
    「一份针对某物质的替代清单」，而我们根本没认出那是什么。"""
    out = _fmt({"chemical": "50", "cas_number": "",
                "alternatives": [{"name": "Water-based cleaner", "rationale": "…"}],
                "note": "…"}, "50")
    assert "could not identify" in out.lower(), out
    assert "not** a substitution recommendation" in out or "not a substitution" in out.lower(), out


def test_ci226_disclosures_are_rendered():
    """风险等级是从**某一份** SDS 推出来的，而那份可能是替代品或不同浓度的（CI-226）。
    丢掉这两条披露，等于把有前提的判断说成无条件的。"""
    out = _fmt({**_REAL_SHAPE, "source_info": {
        "supplier": "X", "substitution": "used toluene SDS",
        "concentration_mismatch": "asked 99%, SDS is 5%"}}, "benzene")
    assert "substituted SDS" in out and "toluene" in out, out
    assert "Concentration mismatch" in out and "5%" in out, out


def test_backend_error_is_surfaced_as_a_failure_not_an_empty_answer(monkeypatch):
    """handler 对空入参返回 `{"error": …}` ⇒ 那是**失败**，不是「没有替代品」。
    不特判的话它会被渲染成一份正常的空答案，并且在调用日志里记成 success=True
    （「非异常失败被记成成功」——CI-344 基线比对抓到过同一形状）。"""
    async def _err(*a, **kw):
        return {"error": "chemical name is required"}

    captured: list = []

    async def _log(tool, chemicals, ms, success, err, *a, **kw):
        captured.append({"success": success, "error": err})

    monkeypatch.setattr(server, "_direct_alternatives", _err)
    monkeypatch.setattr(server, "_log_call", _log)
    set_caller_credential("sk-msds-test")
    try:
        res = asyncio.run(server.get_chemical_alternatives("   "))
    finally:
        set_caller_credential(None)

    assert "Could not look up alternatives" in res.content[0].text, res
    assert captured and captured[0]["success"] is False, captured


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


def test_plain_english_query_takes_the_fast_deterministic_path(sent):
    """🔴 正题。判据打在**实际发出的 URL** 上，不是「代码里有没有那个函数名」。"""
    asyncio.run(server.get_chemical_alternatives("benzene"))
    urls = [u for u, _ in sent]
    assert all("/quick-chat" not in u for u in urls), f"还在走 quick-chat：{urls}"
    assert any("/api/v2/chemical-alternatives" in u for u in urls), urls


@pytest.mark.parametrize("kwargs,why", [
    ({"use_case": "degreasing"}, "curated 表不按 use_case 裁剪"),
    ({"lang": "zh"}, "curated 表里的文案是英文常量"),
])
def test_requests_the_curated_table_cannot_serve_still_go_through_the_llm(sent, kwargs, why):
    """🔴 **窄回退，不是硬切换**：确定性路径快 30 倍，但 curated 表给不了两样东西——
    非英文答复、按 use_case 裁剪。硬切换会让 zh 调用方从中文退回英文、让写了 use_case
    的人拿到与上下文无关的通用建议，**而且是静默的**。

    ⚠️ 我量不出受影响的人有多少（`input_params` 只有 08-15 之后的数据，这个工具在那之后
    没有调用）⇒「没人用 zh」是猜的不是测的，所以按原则保守。
    反向变异：把 `wants_more_than_curated` 恒设为 False，本条必红。
    """
    asyncio.run(server.get_chemical_alternatives("benzene", **kwargs))
    urls = [u for u, _ in sent]
    assert any("/quick-chat" in u for u in urls), f"{why}，却走了确定性路径：{urls}"


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
