"""把 MCP 的 `structuredContent` 映射成 CI-336 不变量能读的形状。

## 为什么需要它（这是它存在的全部理由）

CI-336 的 4 条不变量是为 `/api/v2/*` 的 body 写的，读 `body["chemicals"]`。
而 MCP 包装层发的是 `body["results"]` —— **是改名不是丢数据**，但后果一样：
`check_warning_cas_matches_resolution` 与 `check_no_positive_claim_without_hazard_data`
开头都有 `if not body.get("chemicals"): return []` ⇒ **直接 early-return**。
⇒ 不加适配就跑，得到的 0 是**保证为 0**，不是「干净」（[[green-run-that-executed-nothing]]）。

📌 `_published_names_by_cas` 已经顺带读了 `results`，所以 I3 本来就部分可用 ——
**不是整组都瞎，是 I2/I4 瞎**。别把它说成「不变量在 MCP 上全都没用」。

## 🔴 这层适配本身的风险，写在这里别忘

它是**一份手写的映射**，也就是 [[testing-unreliability-seven-forms]] 里
「替身在被测的那条性质上与真货不同形」的入口。两条约束：
1. **只搬运，不发明**：`resolved` 不是从内容推断的，而是**位置**——
   进了 `results[]` 就是解析出来了，进了 `unresolved[]` 就是没有。这是 MCP 载荷的定义。
2. **必须两侧变异**（见 `_MUTATIONS`），且变异要造在「适配之后不变量还能不能红」上，
   而不是造在适配函数自己的返回值上。
"""
from typing import Any


def adapt(sc: dict) -> dict:
    """MCP structuredContent → CI-336 不变量吃的形状。非 dict 原样返回。"""
    if not isinstance(sc, dict):
        return sc
    body: dict[str, Any] = dict(sc)
    if "chemicals" in body:            # 已经是那个形状，别覆盖
        return body
    chem = []
    for r in body.get("results") or []:
        if not isinstance(r, dict):
            continue
        chem.append({
            # 🔴 只搬运：名字与 CAS 直接取，`resolved` 由**位置**决定（在 results 里＝已解析）
            "name": r.get("chemical_name") or r.get("chemical") or r.get("name"),
            "cas": r.get("cas"),
            "resolved": True,
        })
    # `unresolved` 在 MCP 单端点上可能是 **bool**（CI-714 记过），不变量期望名字列表 ⇒
    # bool 时没有名字可给，保持空列表而不是造一个假名字。
    unres = body.get("unresolved")
    if isinstance(unres, bool):
        body["unresolved"] = []
        body["_unresolved_was_bool"] = unres      # 留痕：这一格我们确实读不到名字
    for n in body.get("unresolved") or []:
        if isinstance(n, str):
            chem.append({"name": n, "cas": None, "resolved": False})
    if chem:
        body["chemicals"] = chem
    return body


# 🔴 变异记录（两侧各造，实跑见 verify_adapter.py）
_MUTATIONS = """
【该红】① 造一个 results[] 条目的 cas 与 documents 里同名条目的 cas 冲突 → I3 必红
        ② 把 unresolved 名字同时放进 documents → I1 必红
【不该红】③ 真实 Prod 载荷（results 正常、unresolved 空）→ 全绿
        ④ 已经是 chemicals[] 形状的 /api/v2 body 喂进来 → adapt 不改它，结果与不适配时逐字相同
"""
