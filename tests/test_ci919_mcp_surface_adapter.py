"""CI-919：`scripts/mcp_surface_adapter.py` 的反向变异，两侧各造。

这层适配把 MCP 的 `structuredContent` 映射成 CI-336 那组不变量能读的形状。
**它是一份手写的映射** —— 也就是 `testing-unreliability-seven-forms` 里
「替身在被测的那条性质上与真货不同形」的入口 ⇒ 必须有变异，且两侧都要。

🔴 **不变量本体不在这个仓**（在 `msds-chain/backend/tests/eval/replay_invariants.py`）。
拿不到就 **skip 并说明**，别静默通过 —— skip 与绿在 CI 输出里同形，
所以这里把原因印出来（同 `test_cron_alert_coverage.py` 顶部那条理由）。

🔴 **本文件只用合成/公开夹具**（`67-64-1` 丙酮 · `71-43-2` 苯 —— **两个都已在 CI-906 白名单里，本次不扩白名单**），
理由见 [[CI-906]]：这是公开仓，真实语料绝不进来。
"""
import os
import pathlib
import sys

import pytest

_ADAPTER_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_ADAPTER_DIR))
from mcp_surface_adapter import adapt  # noqa: E402


def _run_all():
    """→ replay_invariants.run_all，拿不到就 skip（带原因，不静默）。"""
    d = os.environ.get("INVARIANTS_DIR")
    if not d or not (pathlib.Path(d) / "replay_invariants.py").is_file():
        pytest.skip(
            "INVARIANTS_DIR 未指向 msds-chain 的 backend/tests/eval —— "
            "跨仓依赖，本仓单独 clone 时拿不到。适配层自身的形状断言仍然会跑。"
        )
    sys.path.insert(0, d)
    from replay_invariants import run_all
    return run_all


def _red(body, run_all):
    return sorted(k for k, v in run_all(body).items() if v)


# 真实 Prod 载荷的形状（按 2026-09-11 实调的**形状**手写，🔴 取值换成合成夹具——原始那个 CAS 来自真实语料，按 CI-906 红线不进公开仓）
REAL = {
    "results": [{"chemical_name": "acetone",
                 "cas": "67-64-1", "storage_class": "general",
                 "data_source": "msds_parsed", "insufficient_hazard_data": False}],
    "unresolved": [], "unresolved_detail": [],
}


# ── 适配层自身的形状断言（不依赖跨仓，永远会跑）────────────────────────────
def test_results_become_chemicals_and_resolved_comes_from_position():
    """🔴 `resolved` 由**位置**决定：进 results[] 就是解析出来了，不从内容推断。"""
    out = adapt(REAL)
    assert out["chemicals"] == [
        {"name": "acetone", "cas": "67-64-1", "resolved": True}
    ], out.get("chemicals")


def test_unresolved_names_become_unresolved_chemicals():
    out = adapt({"results": [], "unresolved": ["unobtainium"]})
    assert {"name": "unobtainium", "cas": None, "resolved": False} in out["chemicals"]


def test_bool_unresolved_is_normalised_and_leaves_a_trace():
    """🔴 单端点的 `unresolved` 是 **bool**（CI-714 记过），不变量期望名字列表。

    没有名字可给时保持空列表而**不造一个假名字**，并留痕说明这一格我们读不到。
    反向变异：把 `_unresolved_was_bool` 那行删掉 ⇒ 本条红（那时「读不到」与「没有」同形）。
    """
    out = adapt({"results": [{"chemical_name": "x", "cas": "71-43-2"}], "unresolved": True})
    assert out["unresolved"] == []
    assert out["_unresolved_was_bool"] is True


def test_already_v2_shaped_body_is_returned_untouched():
    """🔴 不该红的那侧：已经是 `/api/v2` 形状的 body，adapt 一个字都不许改。"""
    v2 = {"chemicals": [{"name": "benzene", "cas": "71-43-2", "resolved": True}],
          "documents": [], "unresolved": []}
    assert adapt(v2) == v2


def test_non_dict_passes_through():
    assert adapt(None) is None and adapt("x") == "x"


# ── 跨仓：证明适配真的让 I2 睁眼（这才是它存在的理由）──────────────────────
@pytest.mark.parametrize("warn, why", [
    ({"message": "flammable"}, "警告完全没有身份"),
    ({"chemical": "unobtainium", "message": "toxic"}, "警告指向响应自己说没解析出来的名字"),
    ({"cas": "7647-01-0", "message": "corrosive"}, "警告的 CAS 不在已解析集合里"),
])
def test_adapter_unblinds_i2(warn, why):
    """🔴 I2 开头是 `if not body.get("chemicals"): return []` —— MCP 发的是 `results[]`
    ⇒ 不适配就**直接 early-return**，跑出来的 0 是**保证为 0**。

    判据是**「不适配看不见、适配后现形」**，不是「适配后红了」——后者单独成立不能
    证明适配有价值（I1 不适配也会红）。
    """
    run_all = _run_all()
    body = {"results": [{"chemical_name": "benzene", "cas": "71-43-2"}],
            "unresolved": ["unobtainium"], "risk_warnings": [warn]}
    before, after = _red(body, run_all), _red(adapt(body), run_all)
    assert "I2_warning_cas_matches_resolution" not in before, f"{why}: 不适配本来就红了，这条用例证明不了适配的价值"
    assert "I2_warning_cas_matches_resolution" in after, f"{why}: 适配后仍然没红 —— 适配是空跑"


def test_adapter_does_not_manufacture_false_reds():
    """🔴 不该红的那侧：真实载荷经适配后必须仍然全绿。

    误报是豁免表被污染的入口 —— 只造「该红的」会漏掉这一半。
    """
    run_all = _run_all()
    assert _red(adapt(REAL), run_all) == []
