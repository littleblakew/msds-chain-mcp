"""CI-493：合规结果有**两个轴**，渲染层不能把它们念成一句话。

事故原文（2026-08-13 Prod 实测，2026-08-16 复现仍在）：苯查 US 返回
`status=compliant`，唯一证据是一条 OSHA 暴露限值；清单没加载的法域返回肯定的
`not_listed`（"CAS X not found in CN regulatory lists"）——用户读作**可以进口**。

后端（`compliance_engine`）已经把它拆成两个问题：
  `status`     —— 在不在**限制清单**上（restricted / detected / not_restricted / unverified）
  `inventory`  —— 在不在**现有物质名录**上，🔴 极性相反：不在名录上才是风险

🔴 这一层是那两个字段唯一的真实消费面。后端建好模型而渲染层不接，本仓的叫法是
「修了，但没到达真正的消费者」（同 CI-488）。所以下面每一条都断言**吐给用户的字**。
"""
from server import _format_region_results


def _rr(**kw):
    base = {"region": "US", "status": "not_restricted", "flags": [], "details": "", "inventory": {}}
    base.update(kw)
    return base


def _text(*rrs) -> str:
    return "\n".join(_format_region_results(list(rrs)))


def test_a_bare_not_restricted_never_ships_without_naming_what_was_checked():
    """「不在清单上」单说一个词，读起来就是放行。必须带上「查了哪几份、没查哪几份」。"""
    out = _text(_rr(details="CAS 71-43-2 was checked against California Proposition 65 "
                            "and is not on it. Lists we do not hold for US were not checked."))
    assert "California Proposition 65" in out, f"没说查了什么：{out!r}"
    assert "were not checked" in out, f"没说没查什么：{out!r}"


def test_unverified_carries_its_disclaimer_into_the_rendered_text():
    """🔴 `unverified` 被读成「没问题」是这张票的核心事故形状。"""
    out = _text(_rr(region="TW", status="unverified",
                    details="We hold no restriction list for TW, so no check was performed. "
                            "This is not a finding of 'not regulated'."))
    assert "no check was performed" in out
    assert "not a finding of 'not regulated'" in out, f"免责句被丢了：{out!r}"


def test_an_exposure_limit_is_rendered_as_evidence_not_as_a_verdict():
    out = _text(_rr(status="detected", flags=["OSHA PEL (TWA: 1 ppm, STEL: 5 ppm)"],
                    details="Found occupational-exposure or Section 15 evidence for CAS 71-43-2, "
                            "but this is NOT a compliance determination: an exposure limit answers "
                            "'how much exposure is allowed', not 'is this substance permitted'."))
    assert "OSHA PEL" in out
    assert "NOT a compliance determination" in out
    assert "compliant" not in out.lower().replace("compliance", ""), \
        f"渲染文本里不该出现「合规」这个结论词：{out!r}"


def test_absence_from_an_inventory_is_rendered_as_a_warning_not_dropped():
    """🔴 极性相反的那一半：不在 TSCA/IECSC/KECL 名录上通常意味着进口前要申报。
    旧渲染只念 `status` + flags，这一整条信息**根本不会出现在用户看到的文本里**。"""
    # 🔴 note 必须与引擎**当前**产出的措辞一致 —— 手搓一份旧文案然后断言它出现，
    # 只能证明管道通，证明不了契约还在（review 第 9 条）。下面这段抄自
    # `compliance_engine._check_inventories` 的 partial 分支。
    out = _text(_rr(status="not_restricted", inventory={
        "lists_checked": ["US EPA TSCA Inventory"], "on_inventory": False,
        "coverage": "partial",
        "note": "CAS 99999-08-0 was not found in our copy of US EPA TSCA Inventory. "
                "🔴 Our copies of these inventories are PARTIAL (e.g. the TSCA import covers the "
                "ACTIVE subset, IECSC is an extracted snapshot), so this is NOT evidence that the "
                "substance is absent from the official inventory. If it genuinely is absent, import "
                "typically requires new-substance notification — verify against the official "
                "inventory before acting."}))
    assert "NOT listed" in out, f"名录缺席没被渲染出来：{out!r}"
    assert "new-substance notification" in out
    # 对冲句和缺席提示必须一起到达，否则用户读到的是一个肯定的断言
    assert "PARTIAL" in out and "NOT evidence" in out, f"覆盖率对冲被丢了：{out!r}"


def test_being_on_an_inventory_is_not_dressed_up_as_clearance():
    out = _text(_rr(inventory={"lists_checked": ["US EPA TSCA Inventory"], "on_inventory": True,
                               "note": "..."}))
    assert "registration status, not a restriction" in out, \
        f"「在名录上」必须说明它回答的是登记不是管制：{out!r}"


def test_no_inventory_data_says_nothing_rather_than_implying_absence():
    """`on_inventory: None` ＝ 我们没有名录可查。绝不能渲染成「不在名录上」。"""
    out = _text(_rr(inventory={"lists_checked": [], "on_inventory": None,
                               "note": "No existing-substance inventory is available for US."}))
    assert "NOT listed" not in out, f"把「没名录」渲染成了「不在名录上」：{out!r}"


def test_restricted_still_renders_its_flags():
    """回归护栏：新增的两条不能挤掉原本就对的那条路径。"""
    out = _text(_rr(status="restricted", flags=["Listed on California Proposition 65"]))
    assert "restricted" in out and "Proposition 65" in out


def test_every_status_the_engine_can_emit_gets_its_details_rendered():
    """🔴 第一版这里是个白名单 `("not_restricted","unverified","detected")`，于是
    `restricted`（details 里装着限制正文，如 "shall not be used in toys…"）和
    `unsupported`（details 说明为什么这个法域答不了）被静静吞掉 —— 白名单漏掉的
    恰恰是最需要解释的那两个。

    判据枚举**引擎真的会产出的每一个状态**，而不是挑几个来测：白名单式的守卫
    对「名单本身漏了一项」结构性失明，本仓已经在别处栽过这一类。
    """
    for st in ("restricted", "detected", "not_restricted", "unverified", "unsupported"):
        out = _text(_rr(status=st, details=f"explanation-for-{st}"))
        assert f"explanation-for-{st}" in out, f"{st} 的解释没被渲染：{out!r}"
