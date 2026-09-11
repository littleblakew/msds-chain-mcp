"""把真实用户查询回放到**真实 MCP 链路**，并跑 CI-336 的那组不变量。

## 为什么它和 CI-336 不是一回事（存在的全部理由）

CI-336 的回放打的是 `/api/v2/*` —— 那是 MCP server **调用**的代码路径，不是 MCP server 本身
（它自己的 docstring 第一行就写着）。⇒ **包装层（工具 schema · structuredContent · 文本渲染）
结构性没有覆盖**。[[CI-342]] 就是那一类：手工维护的白名单把后端新增字段静默吞掉，
`/api/v2` 的测试永远抓不到。

2026-09-11 首次跑通，当场从 331 条真实查询里逮到 [[CI-919]]（借来的 SDS 借到空却声称
`sds_backed`）与 [[CI-914]]/[[CI-915]]（超时谎报 `is_error=False` + 归因文案是错的）。

## 🔴 这是公开仓 —— 语料绝不进来

语料路径走 `CORPUS` 环境变量，**永远不 vendored**（[[CI-906]] 的红线；那份语料里有
某客户的完整配方组分表）。不变量同理走 `INVARIANTS_DIR` 指向 `msds-chain` 仓。
**改这个文件的人：别为了「方便」把任何一份样例查询硬编码进来。**

## 用法

    INVARIANTS_DIR=<msds-chain>/backend/tests/eval \
    CORPUS=<msds-chain>/backend/tests/eval/golden/replay_user_queries.yaml \
    MCP_KEY_FILE=~/.mcpkey TOOL=get_ppe_recommendation ARG=chemicals \
    LIMIT=0 SLEEP=1.2 OUT=out.json python3 scripts/mcp_surface_replay.py

🔴 **`SLEEP` 别调小。** 网关是令牌桶：**容量 60、补充约 0.83/s**（2026-09-11 实测）。
`SLEEP=1.2`（周期 ~1.4s）永不耗尽；`SLEEP=0.3` 时**正好前 60 条成功、之后全部返回同一句
41 字符文案且 `is_error=False`** —— 看起来像「数据覆盖缺口」，实际是限流。
**判据是「成功的序号是不是连续 1..60 然后全灭」，不是文案内容。**

🔴 **选工具决定这一轮有没有意义。** 四条不变量各自要吃的键不同：
`get_storage_guidance` 的载荷没有 `documents`、也没有那三个 `_POSITIVE_CLAIMS` 取值
⇒ I1/I2/I4 **无从触发**，跑出来的 0 是**保证为 0**。`get_ppe_recommendation` 有
`documents` 与 `sds_backed` ⇒ I1/I3/I4 才真的跑。**起跑前先单调一次看载荷里有没有它们。**

🔴 **用内部测试账号的 key**（`@lagentbot.com`）—— 增长口径自动排除，不污染外部用量指标。
"""

import asyncio, json, os, sys, time, pathlib

sys.path.insert(0, os.environ["INVARIANTS_DIR"])
# 🔴 ALL_CHECKS 是 dict(name->fn)，不是函数列表；直接迭代拿到的是**字符串**。
# 用现成的 run_all(body) -> {name: [violations]}，别自己重拼一遍。
from replay_invariants import run_all  # noqa: E402
# 🔴 不加这层适配，I2/I4 会在 `if not body.get("chemicals"): return []` 处直接 early-return
# ⇒ 跑出来的 0 是**保证为 0**。三个只有 I2 抓得到的变异实测：不适配=[] / 适配后=I2 红。
from mcp_surface_adapter import adapt  # noqa: E402

import httpx2  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

URL = "https://mcp.lagentbot.com/mcp"
KEY = pathlib.Path(os.environ["MCP_KEY_FILE"]).read_text().strip()
LIMIT = int(os.environ.get("LIMIT", "10"))
# 🔴 工具可配：`ask_chemical_safety` 声明 structured_output=False，只返文本 ⇒
# 4 条不变量没东西可吃（pilot 实测 10/10 报 NO_STRUCTURED_CONTENT，那是探针的桶不是真 violation）。
# 要测包装层必须挑**透传 /api/v2 body** 的那 8 个之一。
TOOL = os.environ.get("TOOL", "get_storage_guidance")
ARG  = os.environ.get("ARG", "chemicals")
# `chemicals` 是 ChemicalList（列表），`question`/`chemical` 是标量 —— 形状不同，别猜。
AS_LIST = ARG.endswith("s")
OUT = os.environ.get("OUT", "mcp_replay_result.json")
# 🔴 不限速会被限流：首轮实测**正好前 60 条成功、之后 271 条全部返回同一句 41 字符文案**，
# 而 `is_error=False`、调用层零异常 ⇒ 看起来像「数据覆盖缺口」，实际是限流。
# 判据是「成功的序号 1..60 连续、之后全灭」，不是文案内容。
SLEEP = float(os.environ.get("SLEEP", "0.3"))

def load_queries():
    import yaml
    d = yaml.safe_load(open(os.environ["CORPUS"]))
    qs = []
    for bucket in ("external", "internal_ops"):
        for item in d.get(bucket, []):
            q = item if isinstance(item, str) else (item.get("query") or item.get("q") or item.get("text"))
            if q: qs.append((bucket, q))
    return qs

async def main():
    qs = load_queries()
    if LIMIT: qs = qs[:LIMIT]
    results, t0 = [], time.time()
    # 🔴 逐条落 JSONL：只在结尾 dump 的话，超时或中断 = 全丢。
    #    实测被 `timeout 900` 砍在 212/331，结果文件根本没生成。
    jl = open(OUT + 'l', 'a', buffering=1)
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {KEY}"},
                              timeout=httpx2.Timeout(180.0, read=300.0), follow_redirects=True)
    async with http, streamable_http_client(URL, http_client=http) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for i, (bucket, q) in enumerate(qs, 1):
                rec = {"i": i, "bucket": bucket, "query": q, "tool": TOOL}
                t = time.time()
                try:
                    res = await s.call_tool(TOOL, {ARG: ([q] if AS_LIST else q)})
                    rec["ms"] = int((time.time()-t)*1000)
                    # 🔴 `is_error` 不是 `isError` —— 与 `structured_content` 同族。写 camelCase 时
                    # `getattr(..., False)` 恒为 False ⇒ 「0 个 isError」是假的，等于没测。
                    rec["is_error"] = bool(getattr(res, "is_error", False))
                    # 🔴 属性名是 snake_case `structured_content`（pydantic 字段名）。写成 camelCase 时
                    # `getattr(..., default)` 会**静默返回 None** ⇒ 每条都报 NO_STRUCTURED_CONTENT，
                    # 那是探针自己的桶、看起来却像 100% 命中的真发现。实测连踩两轮（CI-242 同族）。
                    sc = getattr(res, "structured_content", None)
                    rec["has_structured"] = sc is not None
                    txt = ""
                    for c in (res.content or []):
                        if getattr(c, "type", "") == "text": txt += c.text
                    rec["text_len"] = len(txt)
                    # 🔴 不变量跑在 structuredContent 上——那正是包装层产出的东西
                    if isinstance(sc, dict):
                        try:
                            per = run_all(adapt(sc))
                            rec["violations"] = [f"{n}: {m}" for n, ms in per.items() for m in ms]
                        except Exception as e:
                            rec["violations"] = [f"CHECK_CRASHED {type(e).__name__}: {str(e)[:120]}"]
                        rec["sc_keys"] = sorted(sc)[:25]
                    else:
                        rec["violations"] = ["NO_STRUCTURED_CONTENT"]
                except Exception as e:
                    rec["ms"] = int((time.time()-t)*1000)
                    rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                results.append(rec)
                jl.write(json.dumps(rec, ensure_ascii=False) + '\n')
                await asyncio.sleep(SLEEP)
                print(f"  [{i}/{len(qs)}] {rec.get('ms')}ms viol={len(rec.get('violations',[]))} {q[:38]}", flush=True)
    jl.close()
    json.dump({"elapsed_s": round(time.time()-t0,1), "n": len(results), "results": results},
              open(OUT,"w"), ensure_ascii=False, indent=1)
    bad = [r for r in results if r.get("violations") or r.get("error")]
    print(f"\n跑了 {len(results)} 条 / 用时 {round(time.time()-t0,1)}s / 有问题 {len(bad)} 条 → {OUT}")

asyncio.run(main())
