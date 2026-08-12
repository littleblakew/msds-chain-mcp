"""CI-322 B2: 无 CAS 记录的披露必须出现在**文本**里，不能只躺在 structuredContent。

后端把「这份记录没有 CAS、未纳入判定」建模好了，但 `search_chemical_database` 原本对
一个未知 `record_kind` 走的是 else 分支，渲染成 `• **name** (CAS: —)` —— 一句披露都
没有，还把「没有 CAS」渲染成一个破折号，正好是下游读成「没查到 / 无所谓」的形状。

这是本仓反复栽的两个坑叠在一起：
  · [[fix-never-reaches-the-real-consumer]]（后端建好了，模型读的是另一条通道）
  · [[feedback-safety-fix-made-it-worse]]（危害数据不给，"空"在下游＝无危害）

所以判据落在**用户/模型真正读到的那串文本**上，与 test_ci347/test_ci408 同源。
"""
import asyncio

import server

CATALOG = "EN300-1803711"
NAME = "2-(aminomethyl)-1-methylcyclopropane-1-carboxamide"

NO_CAS_ROW = {
    "id": 1,
    "cas_number": None,
    "cas_status": "no_cas_assigned",
    "record_kind": "substance_no_cas",
    "catalog_number": CATALOG,
    "name": NAME,
    "supplier": "Enamine",
    "included_in_assessment": False,
    "disclosure_code": "no_cas_not_assessed",
    "ghs": {"signal_word": "Warning", "hazard_statements": ["H315", "H319", "H335"]},
}

ORDINARY_ROW = {
    "id": 2, "cas_number": "67-64-1", "name": "Acetone",
    "record_kind": "substance", "flammability": "high", "toxicity": "low",
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
            return res.content[0].text, res.structuredContent
        return res, None
    finally:
        (server.httpx.AsyncClient, server._require_api_key,
         server._log_call) = orig_client, orig_key, orig_log


def test_no_cas_disclosure_reaches_the_text_the_model_reads():
    txt, _ = _search([NO_CAS_ROW])

    assert "no CAS number" in txt, f"文本里没说这条记录没有 CAS —— 实际：{txt!r}"
    assert "NOT included" in txt, f"文本里没说它未纳入判定 —— 实际：{txt!r}"
    # 🔴 别渲染成 `(CAS: —)`：破折号读起来像「暂缺/没查到」，而事实是「不存在」。
    assert "(CAS: —)" not in txt


def test_the_ghs_we_hold_is_quoted_not_withheld():
    """B2 不是 B1：手里有的危害分类要照录出来。

    没有这一条，把渲染写成「只说一句『没有 CAS，不予判定』」也能让上面那条通过——
    而那正是 B1，是被否掉的方案（扣留危害数据比给出并标注更危险）。
    """
    txt, _ = _search([NO_CAS_ROW])

    for code in ("H315", "H319", "H335"):
        assert code in txt, f"我们持有的 GHS 分类 {code} 没有出现在文本里：{txt!r}"
    assert "Warning" in txt
    assert CATALOG in txt, "供应商目录号是这类记录唯一的身份键，必须给出来"


def test_structured_content_marks_exclusion_on_every_row():
    """机器判的那一面：`included_in_assessment` 每一行都要有。

    只在被排除的行上加这个键，会让「字段缺失」和「False」在调用方眼里长得一样——
    普通物质那一行必须显式是 True，缺席不能被当成默认值来读（CI-408 同一条原则）。
    """
    _, struct = _search([NO_CAS_ROW, ORDINARY_ROW])
    by_name = {r["name"]: r for r in struct["results"]}

    assert by_name[NAME]["included_in_assessment"] is False
    assert by_name[NAME]["catalog_number"] == CATALOG
    assert by_name["Acetone"]["included_in_assessment"] is True


def test_no_cas_row_survives_truncation_when_ordinary_hits_fill_the_budget():
    """🔴 后端把无 CAS 行**追加在结果尾部**（只在前两层没填满时才补），而这里原本只
    渲染前 5 条 —— 一个名字片段撞上 5~9 条无关的普通物质（"aminomethyl"、
    "cyclopropane" 这类片段在语料里成千上万条），用户真正要找的那条连同它的 GHS 和
    披露就被整段切掉，抬头却仍写着 "Found 10 result(s)"，模型看不出发生过截断。
    """
    ordinary = [dict(ORDINARY_ROW, id=100 + i, name=f"Unrelated {i}") for i in range(9)]
    txt, struct = _search([*ordinary, NO_CAS_ROW])

    assert NAME in txt, f"无 CAS 行被截断掉了，披露没有发生 —— 实际：{txt!r}"
    assert "NOT included" in txt
    assert any(r["name"] == NAME for r in struct["results"])


def test_truncation_is_announced_rather_than_silent():
    """静默的上限读起来和「这就是全部」一模一样。"""
    ordinary = [dict(ORDINARY_ROW, id=200 + i, name=f"Unrelated {i}") for i in range(9)]
    txt, _ = _search([*ordinary, NO_CAS_ROW])

    assert "not shown" in txt, f"截断了却没说 —— 实际：{txt!r}"
