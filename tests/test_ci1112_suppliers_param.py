"""CI-1112：MCP 工具收 `suppliers`，键机械修一次，修不上的**原样交给后端的 422**。

裁决（Blake 2026-09-26）：**机械修 + 透传 422**。两个被显式否掉的方向：
- **静默丢弃**修不上的键 —— 后端明确拒绝这条（调用方会以为我们按他点的供应商作答了）。
- **加宽 `except`** 把 422 变成返回值 —— 本线已证伪（会把 401/402/422 一起变掉）。

🔴 **为什么「机械修」该在 MCP 这边**：后端那个 422 是为**看不见 `chemicals` 的调用方**设的；
我们手里就有那份即将发出的列表 ⇒ 大小写与前后空白这类差异我们自己对得上，不该为它烧掉
用户的一整条安全答案。而 MCP 的调用方是 LLM：它拿到 422 的原因文本能自己改对（CI-410
早就把后端的 detail 带给调用方了），人类调用方才需要「宁可 422」。

🔴 判据落在**真正发出去的那个请求体**上，不是 helper 的返回值 —— 这一票的全部内容就是
「那个 key 到没到后端」。
"""
import asyncio

import pytest

import server


def _sent_body(tool, patch_name, *args, **kwargs) -> dict:
    """调一次工具，把它发给 **`/api/v2/` 那个请求**的 json 抓回来。

    🔴 **必须按 URL 挑，不能「抓最后一次 POST」**：`_log_intent` 在 `finally` 里还会 POST
    一次调用日志 ⇒ 抓最后一次会拿到**日志载荷**。第一版就是这么写的，后果是
    「没带 suppliers 时请求体一字不变」那 7 条**假绿**（日志载荷里当然没有这个键），
    而它们本该是这次改动的主判据。[[green-test-that-executed-nothing]] 的形状。
    🔴 抓不到就 `AssertionError`，不是返回空 dict —— 空 dict 会让下游断言再假绿一次。
    """
    captured: dict = {}

    class _Resp:
        status_code = 200
        headers: dict = {}

        @staticmethod
        def json():
            return {"pairs": [], "warnings": [], "unresolved": [], "documents": [],
                    "compatibility": {"pairs": [], "summary": {}}, "risk_warnings": [],
                    "ppe": {}, "items": []}

        @staticmethod
        def raise_for_status():
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            if "/api/v2/" in url:          # 不是调用日志那一发
                captured["url"] = url
                captured["json"] = json
            return _Resp()

    orig = server.httpx.AsyncClient
    server.httpx.AsyncClient = _Client
    try:
        asyncio.run(tool(*args, **kwargs))
    finally:
        server.httpx.AsyncClient = orig
    assert "json" in captured, (
        f"没抓到任何 /api/v2/ 请求 —— 这条断言本身没跑到被测的那一步（tool={tool!r}）")
    return captured


# 七个收 `suppliers` 的工具（＝后端 `ChemicalsRequest` 带这个字段的那七个端点）。
# 🔴 清单**不手抄成散文**：下面那条守卫拿它和 `published_tool_surface.json` 对，
# 少接一个就红。
SEVEN = [
    (server.check_chemical_compatibility, ["acetone", "water"]),
    (server.get_chemical_risk_warnings, ["acetone"]),
    (server.get_ppe_recommendation, ["acetone"]),
    (server.get_storage_guidance, ["acetone"]),
    (server.get_transport_classification, ["acetone"]),
    (server.get_waste_disposal, ["acetone"]),
    (server.batch_safety_check, ["acetone", "water"]),
]


@pytest.mark.parametrize("tool,chems", SEVEN, ids=lambda v: getattr(v, "__name__", ""))
def test_all_seven_tools_forward_suppliers(tool, chems):
    """变异：任意一个工具的调用点去掉 `suppliers=suppliers`。"""
    body = _sent_body(tool, None, chems, suppliers={chems[0]: "Sigma-Aldrich"})
    assert body["json"]["suppliers"] == {chems[0]: "Sigma-Aldrich"}, body


@pytest.mark.parametrize("tool,chems", SEVEN, ids=lambda v: getattr(v, "__name__", ""))
def test_not_passing_suppliers_leaves_the_request_byte_identical(tool, chems):
    """🔴 没带时请求体里**不许出现** `suppliers` 这个键。

    一个空 dict 对后端等价，但它会把七个端点的请求形状同时改掉，而「不带这个参数的调用
    一字未变」是这次改动唯一能便宜验证的事。
    变异：把 `_with_suppliers` 改成无条件 `body["suppliers"] = cleaned`。
    """
    body = _sent_body(tool, None, chems)
    assert "suppliers" not in body["json"], body


def test_case_and_whitespace_are_repaired_to_the_exact_chemical_string():
    """模型最常见的两种偏差。变异：`_normalize_suppliers` 里去掉 `casefold()` 或 `strip()`。"""
    body = _sent_body(server.check_chemical_compatibility, None,
                      ["acetone", "sulfuric acid"],
                      suppliers={" Acetone ": "Sigma-Aldrich"})
    # 修成 `chemicals` 里的逐字写法 —— 后端拿它与 `{c.strip() for c in chemicals}` 比
    assert body["json"]["suppliers"] == {"acetone": "Sigma-Aldrich"}, body


def test_an_invented_name_is_passed_through_untouched_for_the_backend_to_reject():
    """🔴 本票的裁决就在这一条：**修不上的不丢，原样交出去**。

    丢掉它 = 用户以为我们按他点的供应商作答了（后端明确拒绝的那个方向）。
    原样传 ⇒ 后端回一句可行动的 422，模型下一轮能自己改对。
    变异：在 `_normalize_suppliers` 里把匹配不上的键 `continue` 掉。
    """
    body = _sent_body(server.check_chemical_compatibility, None,
                      ["acetone", "water"],
                      suppliers={"toluene": "Merck"})
    assert body["json"]["suppliers"] == {"toluene": "Merck"}, body


def test_case_ambiguity_is_not_guessed():
    """`["Acetone","acetone"]` 同 casefold 撞上两个不同写法时**不修** —— 猜一个就是替用户
    做决定。原样传出去让后端说不清楚。
    变异：把 `len(cands) == 1` 放宽成 `cands`（取第一个）。
    """
    body = _sent_body(server.check_chemical_compatibility, None,
                      ["Acetone", "acetone"],
                      suppliers={"ACETONE": "Sigma-Aldrich"})
    assert body["json"]["suppliers"] == {"ACETONE": "Sigma-Aldrich"}, body


def test_a_422_reaches_the_model_with_the_backend_reason():
    """透传那一侧的另一半：后端说的原因必须到达模型，否则「透传 422」只是把它变成
    一句不可行动的状态行（CI-410 修过这个）。

    变异：把 `_raise_for_status_with_reason` 的 422 分支换回裸 `raise_for_status()`。
    """
    class _Resp:
        status_code = 422
        headers: dict = {}

        @staticmethod
        def json():
            return {"detail": [{"msg": "Value error, suppliers['toluene'] is not one "
                                       "of `chemicals`"}]}

    with pytest.raises(server.ToolReason) as ei:
        server._raise_for_status_with_reason(_Resp())
    msg = str(ei.value)
    assert "422" in msg and "not one of `chemicals`" in msg, msg


def test_the_published_surface_lists_suppliers_on_exactly_those_seven():
    """🔴 清单要能自己发现成员：拿**真的注册表**对，别在测试里手抄第二份名单。

    少接一个工具、或给一个不该有的工具挂上，这条都红。
    变异：给 `check_mixing_order` 也加 `suppliers`（它是**刻意不加**的 —— 见下条）。
    """
    import json
    import pathlib
    surface = json.loads(
        pathlib.Path(__file__).resolve().parent.parent
        .joinpath("published_tool_surface.json").read_text())["tools"]
    has = {name for name, spec in surface.items()
           if "suppliers" in (spec.get("optional") or [])}
    assert has == {t.__name__ for t, _ in SEVEN}, sorted(has)


def test_mixing_order_deliberately_does_not_take_suppliers():
    """🔴 `check_mixing_order` **刻意不收**，别当成漏接了就去补。

    它走的确实是 `/compatibility/check`，但它的答案逐字写着「加料顺序未判定」——
    换一家的 SDS 不会让顺序变得已判定，而每加一个收它的工具都要在 `tools/list` 里
    再付一份参数描述的 context（同 `Intent` 那条红线）。
    要改这个决定就改这条守卫，并在票面写清为什么。
    """
    import json
    import pathlib
    surface = json.loads(
        pathlib.Path(__file__).resolve().parent.parent
        .joinpath("published_tool_surface.json").read_text())["tools"]
    assert "suppliers" not in (surface["check_mixing_order"].get("optional") or [])


def test_two_supplier_keys_claiming_one_chemical_are_not_silently_merged():
    """🔴 review 抓到的：初版在这里**静默覆盖** —— 后一个供应商赢，前一个一个字都没发出去，
    正是本票声明要避免的那个方向。

    🔴 **我自己的测试为什么没抓到**：上面那条 `test_case_ambiguity_is_not_guessed` 覆盖的是
    「**两个化学品**撞同一个键」，而这条是**反过来**「两个键撞同一个化学品」。同一个碰撞
    有两个方向，我只造了一个。

    🔴 **根因值得单独记**：后端**有**这道防碰撞检查（两个键归一到同一名字就 422），但它只
    `strip()`，而我们这层还多 casefold 了一道 ⇒ 我们的归一化**比上游宽**，造出来的碰撞落在
    上游那道检查**看不见的空间**里。⇒ 每加一层归一化，都要问「上游那道防碰撞的检查还看得
    见我造出来的碰撞吗」。

    变异：把 `_normalize_suppliers` 里的 `claims` 分组去掉（回到 `len(cands) == 1` 就改写）。
    """
    body = _sent_body(server.check_chemical_compatibility, None,
                      ["Acetone", "Toluene"],
                      suppliers={" acetone ": "Sigma-Aldrich", "ACETONE": "Merck"})
    sent = body["json"]["suppliers"]
    # 两个都原样传出去 —— 后端会说不清楚（要么「not one of chemicals」，要么它自己那句
    # 「两个键归一到同一个名字」），两种都可行动，且**一个供应商都没丢**
    assert sent == {" acetone ": "Sigma-Aldrich", "ACETONE": "Merck"}, sent
    assert "Sigma-Aldrich" in sent.values(), "第一个供应商被吃掉了"


def test_a_single_key_is_still_repaired_when_no_one_else_claims_it():
    """上一条的反面锚点：**别为了修那个 bug 把正常的修复也关掉**。

    变异：把 `one` 的条件写成恒 `False`（那时本条红、而上一条仍绿 ⇒ 两条一起才钉得住）。
    """
    body = _sent_body(server.check_chemical_compatibility, None,
                      ["Acetone", "Toluene"], suppliers={" acetone ": "Sigma-Aldrich"})
    assert body["json"]["suppliers"] == {"Acetone": "Sigma-Aldrich"}, body
