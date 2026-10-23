"""CI-226：收 `suppliers` 的工具，要把归一化后的那一份记进 `input_params`。

不记的话，「有没有人在用 supplier 参数」这个问题在调用日志里结构上答不了：
全历史 0 次和「从来没记过」同形。

| 守卫 | 让它红的最小改动 |
|---|---|
| `test_every_supplier_tool_records_it` | 把任一工具 `finally` 里的 `**_supplier_params(...)` 删掉（7 处手工穿线） |
| `test_absent_suppliers_leave_the_payload_unchanged` | 让 `_supplier_params` 在没带时返回 `{"suppliers": {}}` |
| `test_recorded_value_is_the_normalized_one` | `_supplier_params` 改成直接记原始入参 |

成员从 live registry 现算（schema 里有 `suppliers` 的工具），新工具加了参数没穿线就红。
"""
import asyncio
import json

import pytest

import server as _s
from live_coverage_cases import CASES
from request_identity import set_caller_credential

SUPPLIER = "ZZ-CI226-SUPPLIER-ZZ"


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """摘掉本机代理，否则 `httpx.AsyncClient(...)` 构造时就 ImportError（同 test_intent_param）。"""
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
                "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def logged(monkeypatch):
    calls: list[dict] = []

    async def _fake_log(tool_name, chemicals, duration_ms, success,
                        error_message=None, input_params=None, response_text=None,
                        response_kind=None):
        calls.append({"tool": tool_name, "input_params": input_params})

    monkeypatch.setattr(_s, "_log_call", _fake_log)
    return calls


def _supplier_tools() -> list[str]:
    tools = asyncio.run(_s.mcp.list_tools())
    return [t.name for t in tools
            if "suppliers" in ((t.input_schema or {}).get("properties") or {})]


def _call(name, **extra):
    args = dict(CASES[name]["args"], **extra)
    set_caller_credential("sk-msds-test")
    try:
        asyncio.run(getattr(_s, name)(**args))
    except Exception:
        pass
    finally:
        set_caller_credential(None)


def _recorded(logged, name) -> list[dict]:
    return [json.loads(c["input_params"] or "{}") for c in logged if c["tool"] == name]


def test_supplier_tools_are_discovered():
    """阳性对照：成员集合不是空的，否则下面两条恒绿。"""
    assert len(_supplier_tools()) >= 7, _supplier_tools()


def test_every_supplier_tool_records_it(logged):
    silent = []
    for name in _supplier_tools():
        first = CASES[name]["args"]["chemicals"][0]
        logged.clear()
        _call(name, suppliers={first: SUPPLIER})
        rows = _recorded(logged, name)
        if not any(r.get("suppliers") == {first: SUPPLIER} for r in rows):
            silent.append(f"{name}: {rows}")
    assert not silent, f"这些工具收了 suppliers 却没记下来：{silent}"


def test_absent_suppliers_leave_the_payload_unchanged(logged):
    for name in _supplier_tools():
        logged.clear()
        _call(name)
        rows = _recorded(logged, name)
        assert rows, f"{name} 没发日志"
        assert all("suppliers" not in r for r in rows), (name, rows)


def test_recorded_value_is_the_normalized_one(logged):
    """记的是发给后端的那一份：键被修成 `chemicals` 里的逐字写法。"""
    _call("get_storage_guidance", chemicals=["Acetone"], suppliers={" acetone ": SUPPLIER})
    rows = _recorded(logged, "get_storage_guidance")
    assert rows and rows[-1].get("suppliers") == {"Acetone": SUPPLIER}, rows
