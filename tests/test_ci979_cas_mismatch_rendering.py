"""CI-979：用户自己打进来的那个 CAS，我们库里有，却被 40 条向量近邻挤到看不见。

被测的形状（🔴 **这是公开仓**：实际现场、查询串、CAS 一律留在私有票
`docs/pm/tickets/CI-979.md`；本文件夹具全合成，CAS 用白名单里的占位号 ——
见 `test_ci906_no_real_corpus_in_fixtures.py`）：

    query   "<某个名字> <某个 CAS>"     ← 两半都是用户自己打的
    返回    一屏名字相近的**别的**物质，零披露
    而      他打的那个 CAS 我们有，排在候选列表最后一位

机制不是「没查到」，是**排序 + 截断**叠出来的：后端 CI-210 的 display fallthrough 把
用户打的那个 CAS 作为一条 `match_type="cas_mismatch"` 的行**追加在最后**（`rank=6`），
`chemical_resolver` 那段注释早就写明「a consuming UI should surface this row as a
MISMATCH WARNING rather than as the weakest suggestion. The resolver cannot enforce
that presentation.」—— 而本仓在 CI-979 之前**一次都没读过 `match_type`**，于是那条行
排在候选列表最后一位、被 `[:5]` 切掉。前端 `/lookup` 有这道渲染（`handlers.ts`），MCP 面没有。

判据落在**模型/用户真正读到的那串文本**上，与 test_ci322 / test_ci347 / test_ci408 同源。
🔴 与 test_ci322 是**同一个失败形状的第二个实例**（名额被无关行占满 ⇒ 披露没有发生），
所以两边的 fixture 刻意长得一样。
"""
import asyncio

import server

MISMATCH_CAS = "1234-56-7"  # 白名单占位号，故意不是真的
MISMATCH_NAME = "Some Systematic Name We Hold"

MISMATCH_ROW = {
    "id": 1,
    "cas_number": MISMATCH_CAS,
    "name": MISMATCH_NAME,
    "name_zh": "",
    "match_type": "cas_mismatch",
}
ORDINARY_ROW = {
    "id": 2, "cas_number": "67-64-1", "name": "Acetone",
    "match_type": "exact_name", "flammability": "high", "toxicity": "low",
}


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, *_a, **_k):
        return _FakeResponse(self._payload)


def _search(payload):
    """跑 search_chemical_database，返回 (模型读到的文本, structuredContent)。"""
    orig_client, orig_key, orig_log = (
        server.httpx.AsyncClient, server._require_api_key, server._log_call
    )

    async def _no_log(*_a, **_k):
        return None

    server.httpx.AsyncClient = lambda *a, **k: _FakeClient(payload)
    server._require_api_key = lambda: None
    server._log_call = _no_log
    try:
        res = asyncio.run(server.search_chemical_database("anything"))
        if hasattr(res, "content"):
            return res.content[0].text, res.structured_content
        return res, None
    finally:
        (server.httpx.AsyncClient, server._require_api_key,
         server._log_call) = orig_client, orig_key, orig_log


def _prod_shaped_payload():
    """被测形状：一批名字相近的近邻在前，`cas_mismatch` 追加在最后一位。

    近邻条数取 40 只是为了稳稳超过 `[:5]` 的名额，不是一个有含义的数字。
    """
    neighbours = [
        dict(ORDINARY_ROW, id=100 + i, cas_number=f"1000-0{i % 10}-0",
             name=f"Some other neighbour {i}", match_type="vector")
        for i in range(40)
    ]
    return [*neighbours, MISMATCH_ROW]


def test_typed_cas_row_survives_truncation_in_the_prod_shape():
    """🔴 反向变异：把 `shown = _mismatch[:2] + …` 改回 `_ordinary[:5] + _no_cas[:3]`
    ⇒ 本条红（文本里既没有那个 CAS 也没有那条记录名）。这就是 Prod 上发生的事。
    """
    txt, _ = _search(_prod_shaped_payload())

    assert MISMATCH_CAS in txt, f"用户打的那个 CAS 被截断掉了 —— 实际：{txt!r}"
    assert MISMATCH_NAME in txt, f"我们持有的那条记录没出现 —— 实际：{txt!r}"


def test_typed_cas_row_is_rendered_as_a_warning_not_as_an_ordinary_bullet():
    """名额留住了还不够：渲染成普通一条 `• name (CAS: x)` 等于把它端成一个可选答案。

    🔴 反向变异：删掉 `if _is_cas_mismatch(c):` 那一支（让它落进 else）⇒ 本条红。
    """
    txt, _ = _search(_prod_shaped_payload())

    assert "NOT an identified chemical" in txt, (
        f"没说这一行不构成身份 —— 实际：{txt!r}")
    assert "no hazard, compatibility or storage verdict" in txt, (
        f"没说它不参与判定 —— 实际：{txt!r}")
    assert f"`{MISMATCH_CAS}` alone" in txt, (
        f"没给出可执行的下一步（拿 CAS 单独再查一次）—— 实际：{txt!r}")


def test_the_warning_does_not_claim_a_proven_contradiction():
    """🔴 这一行有**两种**同形成因，文案不许挑一种说死。

    ①名字半边指向别的已知物质（真矛盾）②名字半边什么都没印证（我们缺这个俗名）。
    ②并不罕见：用户打的是那个 CAS 的常用俗名，而别名表里没有它
    —— 这时候说「你的名字和 CAS 互相矛盾」是一句假话。
    🔴 反向变异：把文案改成单侧断言（只留 "the CAS belongs to a different substance"）
    ⇒ 本条红。
    """
    txt, _ = _search(_prod_shaped_payload())

    assert "cannot tell them apart" in txt
    assert "a synonym we do not hold" in txt, (
        f"只说了「CAS 属于别的物质」这一侧 —— 实际：{txt!r}")
    assert "a different substance than" in txt


def test_typed_cas_row_is_marked_excluded_in_structured_content():
    """机器可读的那一面不许和文本面说反话。

    后端这条通道不返回 `included_in_assessment` ⇒ 默认值 `kind == "substance"` 会把
    这一行标成 True，而 `is_identity_grade_candidate` 那边它不是身份级。
    🔴 反向变异：把 `False if _is_cas_mismatch(c) else …` 改回原表达式 ⇒ 本条红。
    """
    _, struct = _search([MISMATCH_ROW, ORDINARY_ROW])
    by_cas = {r["cas_number"]: r for r in struct["results"]}

    assert by_cas[MISMATCH_CAS]["included_in_assessment"] is False
    assert by_cas[MISMATCH_CAS]["match_type"] == "cas_mismatch"
    # 误伤侧：普通行照旧。
    assert by_cas["67-64-1"]["included_in_assessment"] is True


def test_ordinary_results_are_untouched():
    """🔴 **不该红的那一侧**（只造「该红的」变异会漏掉误伤面）。

    没有 `cas_mismatch` 行时，这个工具的输出必须与 CI-979 之前逐字一致：不加警告、
    不吃名额、`match_type` 只是多带出来的一个键。
    """
    txt, struct = _search([ORDINARY_ROW])

    assert "NOT an identified chemical" not in txt
    assert "Acetone" in txt and "Flammability: high" in txt
    assert struct["results"][0]["included_in_assessment"] is True


def test_truncation_is_still_announced():
    """名额分配改了，截断提示不能跟着失效（静默上限读起来＝「这就是全部」）。"""
    txt, _ = _search(_prod_shaped_payload())

    assert "not shown" in txt, f"截断了却没说 —— 实际：{txt!r}"
