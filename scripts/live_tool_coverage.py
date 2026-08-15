#!/usr/bin/env python3
"""CI-245：把全部 23 个 MCP 工具在**真实链路**上逐个调一遍。

## 为什么不是 deploy gate

`scripts/smoke_mcp.py` 是 deploy gate，只调 2 个快工具——**故意的**。这个脚本不同：
它每跑一次都**真扣 credits**（上次手工 sweep ~11 credits）、慢、且含内容质量断言。
按 CI-245 的设计要点：**别挂进每次 deploy**，走定时（cron/Hermes）或手动。

⚠️ 票里还写了「默认打 Dev」——**那一条当前做不到**，原因见下面 ENDPOINTS 处的注释
（没有 Dev MCP 部署）。别照票面以为跑的是隔离环境。

## 两层，退出码只由第一层决定

- **可用性层**：调得通、非空、不是 `isError`（除非用例声明 `expect_error_ok`）。→ 决定退出码
- **质量层**：内容是否合理。→ **只报告**。内容断言天生脆（模型措辞会变），
  混进 gate 只会训练人忽略红灯。

## 用法

    # 🔴 今天只有生产一套 MCP（没有 Dev 部署）⇒ 必须用**内部测试账号**的 key，
    # 它的邮箱是 @lagentbot.com，会被增长口径自动排除，不污染外部用量指标。
    MCP_COVERAGE_KEY=sk-msds-... python3 scripts/live_tool_coverage.py

    # 连写工具一起（会在目标环境留下数据，且按 decision-no-auto-cleanup-user-data 不清理）
    ... --include-writes

用例集在 `live_coverage_cases.py`（repo 根），由 `tests/test_ci245_live_coverage_cases.py`
守住「用例 == live registry」——新增工具没加用例会红，不会静默漏测。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_coverage_cases as lc  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

# 🔴 **今天没有 Dev 的 MCP 部署**（2026-08-15 `az containerapp list` 核实：`rg-msds-chain-prod`
# 下只有 msds-chain-mcp-core + msds-chain-mcp-gateway 一套，没有 -dev 对应物）。
# ⇒ CI-245 票里的设计要点①「默认打 Dev、Prod 版走定时」**当前无法实现**。
# 这里不造一个指向 Prod 的假 "dev" 选项来粉饰——那会让人以为跑的是安全环境。
# 真要隔离，得先起一套 Dev MCP（另开票）；在那之前，降风险只能靠：
#   ①用内部测试账号（`@lagentbot.com` 会被增长口径自动排除）②默认跳过写工具。
PROD_URL = "https://mcp.lagentbot.com/mcp"
ENDPOINTS = {
    "prod": PROD_URL,
    # 将来真起了 Dev MCP，用 MCP_COVERAGE_URL 指过去即可；没设就是没有。
    "custom": os.environ.get("MCP_COVERAGE_URL", ""),
}


def _text(result) -> str:
    return "\n".join(t for b in (result.content or []) if (t := getattr(b, "text", None)))


def _answered_the_question(text: str, expect_mentions: list[str]) -> bool:
    """响应有没有真的谈到**你问的那个东西**。

    🔴 为什么不能只判「非空」：MCP 把**工具级失败**包在正常的 JSON-RPC result 里
    （见本仓 gateway `_jsonrpc_succeeded` 的注释）。实测 `get_emergency_response`
    传非法 scenario 时返回 `Emergency response error: ...`——`isError=False`、文本非空，
    于是「非空」判据把它判成通过。**恒真判据比没有判据更糟**，它给的是假绿。

    🔴 也不能靠**文本形状**猜：初版写成「必须含 `**` 或 `###`」，当场把
    `validate_protocol_chemicals` 误杀了——它走 LLM，返回用 `-` 列表、没有粗体。
    形状判据会随措辞漂移，制造假红，而假红比假绿更快被人学会忽略。

    改成**语义**判据：响应必须提到用例问的主体（化学品名等，从 args 机械取）。
    错误话术不会提到主体（那句 scenario 报错里没有 "hydrochloric acid"），
    而真答案无论用什么排版都一定会提。
    """
    low = text.lower()
    return any(m.lower() in low for m in expect_mentions)


async def _run(url: str, key: str, include_writes: bool) -> int:
    headers = {"Authorization": f"Bearer {key}"}
    avail_fail: list[str] = []
    quality_notes: list[str] = []
    skipped: list[str] = []

    async with streamablehttp_client(url, headers=headers, timeout=180) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            registered = {t.name for t in (await session.list_tools()).tools}

            # 🔴 覆盖面判据打在**线上实际注册**的工具集上，不是打在用例表上。
            # 只遍历用例表的话，线上多出来的新工具永远不会被发现——
            # 「跑了 23 条全绿」会掩盖「线上其实有 24 个」。
            # 未覆盖的工具单独记，不混进 avail_fail——它不是"某个工具失败"，
            # 混进去会让下面的通过数算成负数（真出故障时那行数字会误导人）。
            uncovered = sorted(registered - set(lc.CASES) - set(lc.INTENTIONALLY_UNCOVERED))
            if uncovered:
                print(f"::error::线上注册了但没有用例的工具：{uncovered}")

            for name in sorted(registered & set(lc.CASES)):
                case = lc.CASES[name]
                if case.get("writes") and not include_writes:
                    skipped.append(name)
                    print(f"⏭  {name:32} 跳过（写工具，--include-writes 才跑）")
                    continue

                t0 = time.time()
                try:
                    res = await session.call_tool(name, case["args"])
                    text = _text(res)
                    ms = int((time.time() - t0) * 1000)

                    if res.isError and not case.get("expect_error_ok"):
                        avail_fail.append(name)
                        print(f"❌ {name:32} isError  {ms:>6}ms  {text[:120]}")
                        continue
                    if not case.get("expect_error_ok"):
                        if not text.strip():
                            avail_fail.append(name)
                            print(f"❌ {name:32} 空响应  {ms:>6}ms")
                            continue
                        mentions = case.get("expect_mentions") or []
                        if mentions and not _answered_the_question(text, mentions):
                            avail_fail.append(name)
                            print(f"❌ {name:32} 没提到 {mentions}（疑似错误话术）  "
                                  f"{ms:>6}ms  {text[:100]}")
                            continue
                    print(f"✅ {name:32} {ms:>6}ms  {len(text):>6}字符")

                    for desc, check in lc.QUALITY_CHECKS.get(name, []):
                        try:
                            ok = bool(check(text))
                        except Exception as e:  # 质量断言自己炸了也不许影响退出码
                            quality_notes.append(f"⚠️  {name}: 断言异常 {type(e).__name__}: {e}")
                            continue
                        if not ok:
                            quality_notes.append(f"⚠️  {name}: {desc}")
                except Exception as e:  # noqa: BLE001
                    avail_fail.append(name)
                    print(f"❌ {name:32} {type(e).__name__}: {e}")

    print("\n" + "=" * 72)
    attempted = len(registered & set(lc.CASES)) - len(skipped)
    print(f"可用性层：{attempted - len(avail_fail)} 通过 / {len(avail_fail)} 失败 / "
          f"{len(skipped)} 跳过（写工具）")
    if uncovered:
        print(f"   🔴 线上有 {len(uncovered)} 个工具没有用例（未测）：{uncovered}")
    if skipped:
        # 🔴 明说跳过了什么。静默跳过会让「全绿」读起来像「全覆盖」。
        print(f"   跳过的写工具：{skipped}（本次未验证）")
    if quality_notes:
        print(f"\n质量层（仅报告，不影响退出码）：{len(quality_notes)} 条")
        for n in quality_notes:
            print(f"   {n}")
    else:
        print("质量层：全部通过")

    if avail_fail or uncovered:
        if avail_fail:
            print(f"\n::error::可用性层失败：{avail_fail}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=sorted(ENDPOINTS), default="prod",
                    help="prod=唯一存在的 MCP 环境；custom=用 MCP_COVERAGE_URL 指向自建的")
    ap.add_argument("--include-writes", action="store_true",
                    help="连写工具一起跑（会在目标环境留下数据且不清理）")
    args = ap.parse_args()

    key = os.environ.get("MCP_COVERAGE_KEY", "")
    if not key:
        print("MCP_COVERAGE_KEY 未设置（需要一个 sk-msds- 开头的 per-user key）", file=sys.stderr)
        return 2

    url = ENDPOINTS[args.env]
    if not url:
        print("custom 需要设 MCP_COVERAGE_URL；当前没有 Dev MCP 部署可用", file=sys.stderr)
        return 2
    print(f"目标：{args.env} → {url}")
    if url == PROD_URL:
        print("⚠️  这是**生产** MCP（今天没有 Dev 对应物）：会真扣 credits，"
              "并留在真实调用日志里 ⇒ 请用内部测试账号的 key")
    try:
        return asyncio.run(_run(url, key, args.include_writes))
    except Exception as e:  # noqa: BLE001
        print(f"::error::live coverage 跑挂了：{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
