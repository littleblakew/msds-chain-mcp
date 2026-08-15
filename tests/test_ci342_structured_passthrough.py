"""CI-342：后端往响应里加字段，客户端必须能看到——而不是被手抄的白名单静默丢掉。

判据直接打在**失败模式本身**：给后端响应塞一个我们代码里从没出现过的键
（`__new_backend_field__`），断言它出现在 `structuredContent` 里。

🔴 为什么不能拿「真实字段名」当判据：写 `assert "citation" in sc` 只证明**这一次**我把
citation 加进去了，下一个后端新增字段照样静默消失——那种断言测的是「我记得的那些字段」，
不是「新字段会不会到达」。用一个代码里不存在的合成键，是唯一能表达后者的写法。

🔴 也不能只测顶层：实测丢得最狠的是**嵌套层**——`compat.pairs[]` 11 个字段只透出 5 个
（丢 `cas_a`/`cas_b`/`citation`/`source_detail`/`verdict`），`risk.warnings[]` 丢
`additional_hazards`。所以每个工具都要连嵌套一起验。

⚠️ 这一层看不到真后端（MCP 仓不能 import backend）⇒ 它只能守住「透传属性没被改回白名单」。
「后端**真的**加了什么而我们丢了」要靠 `scripts/structured_content_drift.py`（live，真调
后端做差集）。两层各守各的，别指望其中一层覆盖另一层。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential

_NEW = "__new_backend_field__"
_SENTINEL = "sentinel-value"


@pytest.fixture(autouse=True)
def _credential():
    set_caller_credential("sk-msds-test")
    yield
    set_caller_credential(None)


def _fake(payload):
    async def _f(*a, **kw):
        return payload
    return _f


def test_compat_passes_through_new_top_level_and_pair_fields(monkeypatch):
    monkeypatch.setattr(server, "_direct_compat", _fake({
        "pairs": [{"chem1": "a", "chem2": "b", "level": "caution", "reason": "r",
                   "traceability": "rule_based", _NEW: _SENTINEL}],
        "unresolved": [], "documents": [], _NEW: _SENTINEL,
    }))
    sc = asyncio.run(server.check_chemical_compatibility(chemicals=["a", "b"])).structured_content
    assert sc.get(_NEW) == _SENTINEL, "后端新增的顶层字段没到客户端"
    assert sc["pairs"][0].get(_NEW) == _SENTINEL, "后端新增的 pair 字段没到客户端"
    # 有意的对外命名仍然生效（改名是对外契约，不能因为透传就变回 chem1/chem2）
    assert sc["pairs"][0]["chemical_a"] == "a" and "chem1" not in sc["pairs"][0]


def test_risk_passes_through_new_top_level_and_warning_fields(monkeypatch):
    monkeypatch.setattr(server, "_direct_risk", _fake({
        "warnings": [{"chemical": "a", "level": "high", "description": "d", _NEW: _SENTINEL}],
        "unresolved": [], "documents": [], _NEW: _SENTINEL,
    }))
    sc = asyncio.run(server.get_chemical_risk_warnings(chemicals=["a"])).structured_content
    assert sc.get(_NEW) == _SENTINEL
    assert sc["warnings"][0].get(_NEW) == _SENTINEL


def test_batch_passes_through_new_nested_fields(monkeypatch):
    monkeypatch.setattr(server, "_direct_batch", _fake({
        "chemicals": ["a", "b"],
        "compatibility": {"summary": {}, "pairs": [
            {"chem1": "a", "chem2": "b", "level": "caution", "reason": "r", _NEW: _SENTINEL}]},
        "risk_warnings": [{"chemical": "a", "level": "high", "description": "d", _NEW: _SENTINEL}],
        "unresolved": [], "documents": [], _NEW: _SENTINEL,
    }))
    sc = asyncio.run(server.batch_safety_check(chemicals=["a", "b"])).structured_content
    assert sc.get(_NEW) == _SENTINEL
    assert sc["compatibility"]["pairs"][0].get(_NEW) == _SENTINEL
    assert sc["risk_warnings"][0].get(_NEW) == _SENTINEL


@pytest.mark.parametrize("tool,attr,payload", [
    ("check_chemical_compatibility", "_direct_compat",
     {"pairs": [], "unresolved": [], "documents": [], "_usage": {"cost": 1, "balance": 9}}),
    ("get_chemical_risk_warnings", "_direct_risk",
     {"warnings": [], "unresolved": [], "documents": [], "_usage": {"cost": 1, "balance": 9}}),
])
def test_internal_usage_key_never_leaks(monkeypatch, tool, attr, payload):
    """透传不能把内部键一起放出去——`_usage` 是 `_INTERNAL_KEYS` 存在的理由。

    客户端看到的是 `_with_usage` 加的干净 `usage` 块，不是后端那个内部 `_usage`。
    """
    monkeypatch.setattr(server, attr, _fake(payload))
    kw = {"chemicals": ["a", "b"]}
    sc = asyncio.run(getattr(server, tool)(**kw)).structured_content
    assert "_usage" not in sc, "内部键 `_usage` 漏进了 structuredContent"
    assert sc.get("usage", {}).get("balance") == 9, "干净的 usage 块应该还在"


def test_expose_drops_only_what_it_is_told_to():
    """`_expose` 的默认行为必须是**全透**，挡掉的键必须是显式列出的那些。

    反过来写（默认挡、列出来的才给）就是我们刚修掉的白名单，换个位置而已。
    """
    out = server._expose({"a": 1, "_usage": 2, "b": 3})
    assert out == {"a": 1, "b": 3}
    assert server._expose({"chem1": "x"}, rename={"chem1": "chemical_a"}) == {"chemical_a": "x"}
    assert server._expose({"t": 1}, override={"t": 2}) == {"t": 2}
    assert server._INTERNAL_KEYS == frozenset({"_usage"}), (
        "改动内部键集合＝改动对外可见面，请在这里显式写下新键并说明为什么不外泄"
    )
