"""CI-245：live 工具覆盖用例集的**防腐**守卫（离线，不联网、不花 credits）。

为什么需要这个守卫：23 个工具的调用用例如果是一份**手工维护的清单**，它一定会腐化——
新增一个工具、没人记得加用例，覆盖率就静默地从 23/23 掉到 23/24，而**没有任何一步会报错**
（同族：memory「列表型守卫腐化」「我自己写的守卫反复是空跑」）。

所以用例集不做成清单，做成**必须与 live registry 精确相等的集合**：
少一个 → 红（新工具没覆盖）；多一个 → 也红（工具改名/下线了，用例是死的）。
"""
import asyncio

import live_coverage_cases as lc
import server


def _registered() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_every_registered_tool_has_a_case():
    """新增工具但没加用例 ⇒ 红。这是本守卫存在的首要理由。"""
    missing = _registered() - set(lc.CASES)
    assert not missing, (
        f"这些工具已注册但没有 live 覆盖用例：{sorted(missing)}——"
        f"加用例，或显式写进 lc.INTENTIONALLY_UNCOVERED 并说明理由"
    )


def test_no_case_for_a_tool_that_no_longer_exists():
    """工具改名/下线后用例还留着 ⇒ 红。否则它会一直「跑过」一个不存在的东西。"""
    stale = set(lc.CASES) - _registered()
    assert not stale, f"这些用例指向已不存在的工具：{sorted(stale)}"


def test_write_tools_are_marked_as_writes():
    """写工具必须被标出来：它们会往 Prod 落数据，而按 [[decision-no-auto-cleanup-user-data]]
    写进去就留着。标错 ⇒ 默认跑 Prod 时会静默产生用户数据。

    判据取自 registry 的 `readOnlyHint`，不靠人记——annotations 是工具自己声明的。
    """
    tools = asyncio.run(server.mcp.list_tools())
    for t in tools:
        ro = getattr(t.annotations, "read_only_hint", None) if t.annotations else None
        if ro is False:
            assert lc.CASES[t.name].get("writes") is True, (
                f"{t.name} 的 readOnlyHint=False（会写数据），但用例没标 writes=True"
            )


def test_every_case_supplies_all_required_args():
    """用例给的参数必须满足工具自己声明的 required——否则跑起来是参数错误，
    而「调用失败」会被读成「工具坏了」，把可用性层的信号污染掉。"""
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name, case in lc.CASES.items():
        required = (tools[name].input_schema or {}).get("required", [])
        given = set(case.get("args", {}))
        missing = set(required) - given
        assert not missing, f"{name} 用例缺必填参数 {sorted(missing)}"


def test_quality_checks_are_attached_to_real_cases():
    """质量层断言只能挂在存在的用例上（否则它永远不会被执行——又一种空跑）。"""
    for name in lc.QUALITY_CHECKS:
        assert name in lc.CASES, f"质量断言挂在不存在的用例 {name} 上，永远不会跑"


# ── deploy gate 的宽松下限（CI-245 点名的现成 bug）──────────────────


def test_smoke_gate_uses_exact_registry_count_not_a_loose_floor():
    """🔴 `scripts/smoke_mcp.py` 原本用 `MCP_SMOKE_MIN_TOOLS` 默认 **20**，而实际 23
    ⇒ **线上掉 3 个工具，deploy gate 依然全绿**。宽松下限＝没测过。

    这条守住两件事：①判据来自 live registry 的精确值 ②那个下限没被谁改回来。
    """
    import asyncio
    import os
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "scripts", "smoke_mcp.py")

    src = open(path).read()
    # 🔴 判据是「这个变量还有没有被**读取**」，不是「文本里出现过没有」。
    # 初版写成 `"MCP_SMOKE_MIN_TOOLS" not in src`，立刻被**解释这段历史的注释**命中而误红
    # ——正是 memory 里那条「判据用自由文本匹配 → 被含同关键词的反例命中」。
    assert 'environ.get("MCP_SMOKE_MIN_TOOLS"' not in src.replace("'", '"'), (
        "宽松下限回来了——它让掉工具也能全绿；判据必须是 registry 的精确值"
    )
    assert ">=" not in src.split("assert len(names)")[1].split("\n")[0], (
        "工具数断言又变成了下限比较（>=），必须是 ==")

    # 🔴 必须按**CI 实际执行脚本的方式**验：`python3 scripts/smoke_mcp.py` 时
    # sys.path[0] 是 `scripts/`，仓根不在 path 上。用 importlib 从测试进程里加载
    # 会带上仓根的 path，于是 `import server` 能过、`asyncio.run` 也能用——
    # **两个真 bug 都被这种加载方式掩盖过**（2026-08-15 把生产 gate 弄红）。
    import subprocess
    out = subprocess.run(
        [sys.executable, "-c",
         "import asyncio, smoke_mcp; print(asyncio.run(smoke_mcp._expected_tool_count()))"],
        cwd=os.path.join(root, "scripts"), capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert out.returncode == 0, (
        f"按 CI 的方式（scripts/ 为 cwd）加载脚本就炸——这正是 gate 会红的原因：\n{out.stderr[-600:]}"
    )
    assert int(out.stdout.strip()) == len(asyncio.run(server.mcp.list_tools()))


def test_non_error_cases_carry_a_real_assertion():
    """每条「期望成功」的用例都必须带 expect_mentions——否则它的可用性判据退化成
    「非空」，而工具级失败是包在成功结果里的文本，非空判据一定放行（实测栽过）。"""
    for name, case in lc.CASES.items():
        if case.get("expect_error_ok"):
            continue
        assert case.get("expect_mentions"), (
            f"{name} 没有 expect_mentions ⇒ 它的判据是恒真的「非空」，等于没判"
        )
