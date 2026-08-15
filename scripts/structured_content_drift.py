#!/usr/bin/env python3
"""CI-342：真调后端 + 真调工具，把「后端给了、structuredContent 没给」的键差出来。

## 为什么必须是 live，不能进 CI

MCP 仓 **import 不到 backend**，所以 CI 里的守卫（`tests/test_ci342_structured_passthrough.py`）
只能验「透传属性还在不在」——它给后端响应塞一个合成键，看得到就算过。那守不住
「后端**真的**新增了一个字段」这件事，因为 CI 根本不知道后端现在返回什么。
这个脚本是另一半：它拿真实响应做差集。**两层各守各的，别指望其中一层覆盖另一层。**

## 跑法

    MCP_COVERAGE_KEY=<test@lagentbot.com 的 sk-msds- key> python3 scripts/structured_content_drift.py

🔴 它**打生产**（没有 Dev 的 MCP/后端对应物），每跑一次真扣 credits ⇒ 别挂进 deploy，
和 `live_tool_coverage.py` 一样走手动或定时。用 test@ 的 key（`@lagentbot.com` 会被
增长口径自动排除）。

## 判据

退出码由「未申报的丢失」决定。**有意重塑**的键写进 `RESHAPED`，并写清楚为什么——
一个没有理由的豁免和一个 bug 长得一模一样。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from request_identity import set_caller_credential  # noqa: E402

# 后端有、structuredContent 顶层没有，但**是有意的**——每条都要有理由。
RESHAPED: dict[str, dict[str, str]] = {
    "check_regulatory_compliance": {
        "cas": "工具按化学品循环调后端，每次的整份响应原样进 results[]，这些键在那一层",
        "chemical": "同上",
        "region_results": "同上",
        "summary_level": "同上",
    },
}

# 每条：工具名 → (后端 helper, helper 参数, 工具函数, 工具参数, 要比对的嵌套路径)
# 嵌套路径写成 (后端取法, 我们取法)，都返回一个 dict 的 list。
CASES = [
    ("check_chemical_compatibility",
     lambda: server._direct_compat(["sulfuric acid", "sodium hydroxide"]),
     lambda: server.check_chemical_compatibility(chemicals=["sulfuric acid", "sodium hydroxide"]),
     [("pairs", lambda d: d.get("pairs") or [], lambda s: s.get("pairs") or [],
       {"chem1": "chemical_a", "chem2": "chemical_b"})]),
    ("get_chemical_risk_warnings",
     lambda: server._direct_risk(["sulfuric acid"]),
     lambda: server.get_chemical_risk_warnings(chemicals=["sulfuric acid"]),
     [("warnings", lambda d: d.get("warnings") or [], lambda s: s.get("warnings") or [], {})]),
    ("batch_safety_check",
     lambda: server._direct_batch(["sulfuric acid", "sodium hydroxide"]),
     lambda: server.batch_safety_check(chemicals=["sulfuric acid", "sodium hydroxide"]),
     [("compatibility.pairs",
       lambda d: (d.get("compatibility") or {}).get("pairs") or [],
       lambda s: (s.get("compatibility") or {}).get("pairs") or [],
       {"chem1": "chemical_a", "chem2": "chemical_b"}),
      ("risk_warnings", lambda d: d.get("risk_warnings") or [],
       lambda s: s.get("risk_warnings") or [], {})]),
    ("get_ppe_recommendation",
     lambda: server._direct_ppe(["acetone"]),
     lambda: server.get_ppe_recommendation(chemicals=["acetone"]), []),
    ("check_regulatory_compliance",
     lambda: server._direct_compliance("benzene", ["EU", "US"]),
     lambda: server.check_regulatory_compliance(chemicals=["benzene"]), []),
    ("search_msds_online",
     lambda: server._direct_online_search("acetonitrile", ""),
     lambda: server.search_msds_online(chemical_name="acetonitrile"), []),
    ("get_sds_document",
     lambda: server._direct_sds_document("acetone"),
     lambda: server.get_sds_document(chemical="acetone"), []),
]

# 我们自己加的、后端没有的键——出现在 structuredContent 里是正常的，不报。
_OURS = {"usage", "chemicals", "summary", "query", "expires_in_seconds", "regions",
         "regions_defaulted", "results", "documents", "pairs"}
_INTERNAL = {"_usage"}


def _keys(d) -> set[str]:
    return set(d) if isinstance(d, dict) else set()


async def main() -> int:
    key = os.environ.get("MCP_COVERAGE_KEY", "")
    if not key:
        print("MCP_COVERAGE_KEY 未设置（需要一个 sk-msds- 开头的 per-user key）", file=sys.stderr)
        return 2
    set_caller_credential("Bearer " + key)
    print("目标：Prod 后端（没有 Dev 对应物）——会真扣 credits\n")

    problems: list[str] = []
    for name, direct, tool, nested in CASES:
        try:
            backend = await direct()
            result = await tool()
        except Exception as e:  # noqa: BLE001
            problems.append(f"{name}: 调用失败 {type(e).__name__}: {e}")
            print(f"❌ {name:32} {type(e).__name__}: {e}")
            continue

        sc = getattr(result, "structured_content", None) or {}
        declared = set(RESHAPED.get(name, {}))
        dropped = _keys(backend) - _keys(sc) - _INTERNAL - declared
        line = f"{'✅' if not dropped else '❌'} {name:32}"
        if dropped:
            problems.append(f"{name} 顶层丢: {sorted(dropped)}")
            line += f" 顶层丢 {sorted(dropped)}"
        print(line)

        for path, b_get, s_get, rename in nested:
            b_items, s_items = b_get(backend), s_get(sc)
            if not b_items:
                print(f"   ⏭  {path}: 后端这次没返回条目，未比对")
                continue
            if not s_items:
                problems.append(f"{name}.{path} 后端有 {len(b_items)} 条，我们一条都没给")
                print(f"   ❌ {path}: 后端 {len(b_items)} 条 → 我们 0 条")
                continue
            b_keys = {rename.get(k, k) for k in _keys(b_items[0])}
            nd = b_keys - _keys(s_items[0]) - _INTERNAL
            if nd:
                problems.append(f"{name}.{path} 丢: {sorted(nd)}")
                print(f"   ❌ {path} 丢 {sorted(nd)}")
            else:
                print(f"   ✅ {path}")

    print("\n" + "=" * 72)
    if problems:
        print(f"::error::structuredContent 有 {len(problems)} 处未申报的丢失：")
        for p in problems:
            print(f"   {p}")
        print("\n修法：让那一层走 `_expose()` 透传；确实有意不给，就写进本文件的 RESHAPED 并说明理由。")
        return 1
    print("没有未申报的丢失。")
    if RESHAPED:
        n = sum(len(v) for v in RESHAPED.values())
        print(f"（{n} 个键按 RESHAPED 豁免——理由写在那张表里，别当成「没问题」读）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
