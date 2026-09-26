"""CI-1099: 限制条件必须长在**判定句**上，不能只待在平级的披露块里。

上游 CI-1096 的实测形状：客户端模型把披露块整段丢掉、判定句留下，于是用户读到一个
看起来**无条件的放行**。我们自己的头句还曾把那块标成 "informational only" 并把读者
指向下面的结论——丢弃授权是我们签发的。

🔴 **本文件的载荷刻意造成 `compatible`**。上游票里那个实测用例是
`hydrogen peroxide + acetone` → `incompatible`，拿它验等于空跑：用户不会据一个
incompatible 判定去供货。真正危险的是**判定放行、而某一支带限售**。

🔴 **判据落在渲染出来的那串文本上**（模型读的就是它），不是「helper 返回了什么」。
每条守卫下面注明它的变异：撤掉那一处改动时它必须红。
"""
import asyncio

import server


# EU 2019/1148 Annex I 的阈值具文——后端逐字抄自法规、**刻意不翻译**，所以它同时是
# 「限制串」最好的搜索锚点。🔴 `12 % w/w` 带空格，`12%` 零命中。
ANNEX_I_NOTE = ("limit value 12 % w/w; licensing possible up to 35 % w/w "
                "(Article 5(3))")
H2O2_STATEMENT = ("Hydrogen peroxide is listed on EU 2019/1148 Annex I as a restricted "
                  "explosives precursor: it may not be supplied to members of the public "
                  f"above the {ANNEX_I_NOTE}. This tool only reports list membership; it "
                  "is not a compliance determination.")

DISCLOSURE = [{
    "cas": "7722-84-1", "query_name": "hydrogen peroxide",
    "matched_name": "Hydrogen peroxide", "regime": "explosive_precursor",
    "tier": "EU 2019/1148 Annex I", "authority": "European Commission",
    "note": ANNEX_I_NOTE, "statement": H2O2_STATEMENT,
}]

# 🔴 判定是 `compatible` —— 见模块 docstring。水 + 过氧化氢在登记表里无冲突。
COMPAT_GREEN = {
    "pairs": [{"chem1": "hydrogen peroxide", "chem2": "water", "level": "compatible",
               "reason": "no known incompatibility in the registry",
               "traceability": "rule_based", "source": "reactive_groups",
               "cas_a": "7722-84-1", "cas_b": "7732-18-5"}],
    "unresolved": [], "documents": [], "precursor_disclosure": DISCLOSURE,
}
BATCH_GREEN = {
    "compatibility": {"pairs": [{"chem1": "hydrogen peroxide", "chem2": "water",
                                 "level": "compatible", "reason": "no known incompatibility",
                                 "traceability": "rule_based"}],
                      "summary": {"compatible": 1, "caution": 0, "incompatible": 0}},
    "risk_warnings": [], "ppe": {}, "unresolved": [], "documents": [],
    "precursor_disclosure": DISCLOSURE,
}


def _run(tool, patch_name, payload, *args):
    async def _fake(*_a, **_k):
        return payload
    orig = getattr(server, patch_name)
    setattr(server, patch_name, _fake)
    try:
        res = asyncio.run(tool(*args))
        return res if hasattr(res, "content") else res
    finally:
        setattr(server, patch_name, orig)


def _text(tool, patch_name, payload, *args):
    res = _run(tool, patch_name, payload, *args)
    return res.content[0].text if hasattr(res, "content") else res


def _verdict_line(text: str) -> str:
    """判定所在的那一行。**按行取**，不是在全文里搜——全文搜等于把披露块也算进来，
    那正是本票要区分开的两个位置（[[green-test-that-executed-nothing]] 的形状）。"""
    for line in text.splitlines():
        if line.startswith("- **hydrogen peroxide**") and "compatible" in line:
            return line
    raise AssertionError(f"no verdict line in:\n{text}")


def test_green_verdict_line_itself_says_it_is_conditional():
    """变异：撤掉 `check_chemical_compatibility` 里 `{restrict_clause}` 的插值。"""
    out = _text(server.check_chemical_compatibility, "_direct_compat", COMPAT_GREEN,
                ["hydrogen peroxide", "water"])
    line = _verdict_line(out)
    assert "conditional" in line, line
    assert "not clearance to supply, sell or transfer" in line, line


def test_the_concrete_restriction_survives_dropping_the_disclosure_block():
    """限制串必须在**判定行的紧邻上下文**里，而不是只在那个可被整段丢掉的块里。

    模拟客户端模型的实际行为：把披露块整段删掉，看剩下的文本还说不说得出限制。
    变异：撤掉 `restrict_lines` 的拼接（判定行只剩补语、没有具体清单与阈值）。
    """
    out = _text(server.check_chemical_compatibility, "_direct_compat", COMPAT_GREEN,
                ["hydrogen peroxide", "water"])
    survived = "\n".join(
        l for l in out.splitlines() if "Regulated-precursor notice" not in l
        and H2O2_STATEMENT not in l)
    assert "EU 2019/1148 Annex I" in survived, survived
    assert "12 % w/w" in survived, survived


def test_structured_pair_carries_the_restriction_too():
    """只读 structuredContent 的客户端（Coze / 部分 ChatGPT 形态）拿到的是 `pairs[]`，
    没有客户端会自己按名字回顶层数组里对。

    变异：撤掉 `struct_pair["precursor_restrictions"]` 那一段。
    """
    res = _run(server.check_chemical_compatibility, "_direct_compat", COMPAT_GREEN,
               ["hydrogen peroxide", "water"])
    pair = res.structured_content["pairs"][0]
    hits = pair["precursor_restrictions"]
    assert [h["tier"] for h in hits] == ["EU 2019/1148 Annex I"], hits
    assert hits[0]["note"] == ANNEX_I_NOTE, hits
    # 成句仍只有一份（顶层），别在这里复制 `statement`
    assert "statement" not in hits[0], hits[0]


def test_batch_matrix_verdict_line_also_carries_it():
    """batch 的文本是另一套 `sections` 拼装，共用不了上面的守卫——CI-553 当年就是这处
    最后才想起来。变异：撤掉 `batch_safety_check` 里 `{restrict_clause}`。
    """
    out = _text(server.batch_safety_check, "_direct_batch", BATCH_GREEN,
                ["hydrogen peroxide", "water"])
    # 🔴 按「判定行」的形状取，别只按行首 —— 披露块那一行行首也是 `- **hydrogen peroxide**`，
    # 用行首取会取到披露块自己，那样这条守卫就在验它本来不该验的那一处。
    line = next(l for l in out.splitlines()
                if l.startswith("- **hydrogen peroxide**") and " + **water**" in l)
    assert "conditional" in line, line
    assert "EU 2019/1148 Annex I" in out, out


def test_mixing_order_fallback_path_also_carries_it():
    """`check_mixing_order` 落回 `_direct_compat` 的那条路（CI-869）——它只在 RAI 拒答时
    才走，而触发拒答的恰恰是真危险对。变异：撤掉那条路上的 `{restrict_clause}`。
    """
    async def _fake(*_a, **_k):
        return COMPAT_GREEN
    orig = server._direct_compat
    server._direct_compat = _fake
    try:
        out = asyncio.run(server._mixing_order_grounded_fallback(
            "hydrogen peroxide", "water", None))
    finally:
        server._direct_compat = orig
    text = out["answer"]
    line = _verdict_line(text)
    assert "conditional" in line, line
    assert "EU 2019/1148 Annex I" in text, text


def test_a_pair_with_no_listed_chemical_is_untouched():
    """🔴 反方向：没有命中时判定行必须**一个字节都不变**。否则这道改动就变成给每条判定
    加一句无差别的免责声明——那是 `precursor_disclosure.py`「分档语气」记过的另一个失效
    方向（把不提示换成无差别指控）。
    """
    clean = {"pairs": [{"chem1": "water", "chem2": "ethanol", "level": "compatible",
                        "reason": "miscible", "traceability": "rule_based",
                        "source": "reactive_groups"}],
             "unresolved": [], "documents": [], "precursor_disclosure": []}
    out = _text(server.check_chemical_compatibility, "_direct_compat", clean,
                ["water", "ethanol"])
    assert "conditional" not in out, out
    assert "regulated-precursor" not in out.lower(), out


def test_hit_without_tier_is_still_disclosed_on_the_verdict():
    """命中了但 `tier` 缺失不许静默跳过——那正是最该说话的时候（同 `_precursor_disclosure_block`
    的 `statement` 缺失分支）。变异：把 `_precursor_pair_note` 的 else 分支删掉。

    🔴 「一个分面都不剩」才说「清单名没拿到」；还剩 regime / authority 时要印它们
    （见下面 review ② 那条）。本条只钉「判定行仍被标成有条件」这一半。
    """
    payload = dict(COMPAT_GREEN)
    payload["precursor_disclosure"] = [
        {**DISCLOSURE[0], "tier": "", "note": "", "regime": "", "authority": ""},
    ]
    out = _text(server.check_chemical_compatibility, "_direct_compat", payload,
                ["hydrogen peroxide", "water"])
    line = _verdict_line(out)
    assert "conditional" in line, line
    assert "list name was not returned" in out, out


def test_cas_only_handle_still_matches():
    """后端按 CAS 去重 ⇒ 同一次调用里用 CAS 问的那个物质，其披露条目的 `query_name`
    可能是**另一个**写法。只按名字对会漏，漏的方向是少披露。
    变异：把 `_precursor_index` 里的 `e.get("cas")` 把手去掉。
    """
    payload = dict(COMPAT_GREEN)
    payload["pairs"] = [{**COMPAT_GREEN["pairs"][0], "chem1": "7722-84-1"}]
    out = _text(server.check_chemical_compatibility, "_direct_compat", payload,
                ["7722-84-1", "water"])
    line = next(l for l in out.splitlines() if l.startswith("- **7722-84-1**"))
    assert "conditional" in line, line


def test_one_chemical_is_not_disclosed_twice_on_one_verdict():
    """名字与 CAS 两个把手指向同一条披露时只能出一行——否则每条判定行会把同一句限制
    说两遍。变异：去掉 `_precursor_pair_note` 里的 `done` 去重。
    """
    out = _text(server.check_chemical_compatibility, "_direct_compat", COMPAT_GREEN,
                ["hydrogen peroxide", "water"])
    assert out.count("is on a regulated-precursor list") == 1, out


# ─────────────────── review 抓到的三条，各钉一个守卫 ───────────────────
# 🔴 三条的共同形状值得单记：**同一件事有两个面 / 两个子情形，我只做了自己正在看的那个**。
# ①文本面挂了、结构化面漏了 ②主路径挂了、tier 缺失那条子路径把读者指回可丢弃的块
# ③覆盖对了、但显示名对不上判定行。三条都不会报错。

def test_batch_structured_pairs_also_carry_the_restriction():
    """review ①：`batch_safety_check` 的结构化 `compatibility.pairs` 此前没挂上，
    而它旁边 CI-570 那条注释正写着**这个工具**有只读 structuredContent 的消费者
    （claude.ai 连接器）⇒ 漏这一处等于 CI-1096 从这个端点原样复发。

    变异：把 `_batch_pair_structured` 换回裸 `_expose(...)`。
    """
    res = _run(server.batch_safety_check, "_direct_batch", BATCH_GREEN,
               ["hydrogen peroxide", "water"])
    pairs = res.structured_content["compatibility"]["pairs"]
    hits = pairs[0]["precursor_restrictions"]
    assert [h["tier"] for h in hits] == ["EU 2019/1148 Annex I"], hits


def test_tier_missing_fallback_does_not_point_back_at_the_droppable_block():
    """review ②：tier 缺失时的兜底句原来写 "see the notice for the full statement"
    ——指回的正是本票证明了会被整段丢掉的那块。它必须自带还剩下的分面。

    变异：把 residual 那段删掉 / 把 "see the notice" 加回去。
    """
    payload = dict(COMPAT_GREEN)
    payload["precursor_disclosure"] = [{
        **DISCLOSURE[0], "tier": "", "note": "",
        "regime": "explosive_precursor", "authority": "European Commission",
    }]
    out = _text(server.check_chemical_compatibility, "_direct_compat", payload,
                ["hydrogen peroxide", "water"])
    assert "see the notice" not in out, out
    # 分面必须活在**判定行的紧邻上下文**里，而不是只在那个可丢弃的块里
    survived = "\n".join(l for l in out.splitlines()
                         if "Regulated-precursor notice" not in l)
    assert "explosive_precursor" in survived, survived
    assert "European Commission" in survived, survived


def test_the_label_is_the_name_on_the_verdict_line_not_the_cas():
    """review ③：名字没命中而 CAS 命中时（后端按 CAS 去重 ⇒ 第二个别名没有自己的条目），
    披露行不许印出一串 CAS 号——读者对不上判定行里的名字。

    变异：把 `_pair_sides` 摊平回一串把手（名字与 CAS 不分层）。
    """
    payload = dict(COMPAT_GREEN)
    # 判定行用别名 `H2O2`，而披露条目的 query_name/matched_name 都不是它
    payload["pairs"] = [{**COMPAT_GREEN["pairs"][0], "chem1": "H2O2"}]
    payload["precursor_disclosure"] = [{
        **DISCLOSURE[0], "query_name": "hydrogen peroxide",
        "matched_name": "Hydrogen peroxide",
    }]
    out = _text(server.check_chemical_compatibility, "_direct_compat", payload,
                ["H2O2", "water"])
    line = next(l for l in out.splitlines() if "is on a regulated-precursor list" in l)
    assert "**H2O2**" in line, line
    assert "7722-84-1" not in line, line
    res = _run(server.check_chemical_compatibility, "_direct_compat", payload,
               ["H2O2", "water"])
    facet = res.structured_content["pairs"][0]["precursor_restrictions"][0]
    assert facet["chemical"] == "H2O2", facet
    assert facet["cas"] == "7722-84-1", facet   # CAS 仍在，只是不当显示名


def test_no_known_incompatibility_is_the_most_dangerous_green_and_carries_it():
    """🔴 Prod 上真实存在的第四种 level（`hydrogen peroxide + water` 实测就是它），而本文件
    原来只覆盖 `compatible` / `caution` —— 是部署后跑判据探针时才发现的覆盖缺口。

    它读起来就是放行，而仓里 `_mixing_order_grounded_fallback` 早就为这句写过红线
    （「没查到不相容」不是顺序许可）⇒ **这一档最该带限制**，不是最不该。
    变异：撤掉 `check_chemical_compatibility` 的 `{restrict_clause}`（同 M1）。

    🔴 另一半也钉住：`no_known_incompatibility` 与 `incompatible` 只差一个词尾，
    任何按**子串**判「是不是 incompatible」的判据都会把这一档误判成不用管。
    """
    assert "incompatible" not in "no_known_incompatibility", (
        "词尾假设变了：`no_known_incompatibility` 现在包含 `incompatible` 这个子串 ⇒ "
        "任何按子串判级的地方会把这一档误判成不用管，去重查")
    payload = dict(COMPAT_GREEN)
    payload["pairs"] = [{**COMPAT_GREEN["pairs"][0],
                         "level": "no_known_incompatibility",
                         "reason": "no conflict found in the registry"}]
    out = _text(server.check_chemical_compatibility, "_direct_compat", payload,
                ["hydrogen peroxide", "water"])
    line = next(l for l in out.splitlines()
                if l.startswith("- **hydrogen peroxide**") and " + **water**" in l)
    assert "no_known_incompatibility" in line, line
    assert "conditional" in line, line
    assert "not clearance to supply, sell or transfer" in line, line
