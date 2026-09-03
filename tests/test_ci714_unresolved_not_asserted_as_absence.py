"""CI-714: `unresolved: true` 不许被渲染成「库里没有这份数据」。

这**三**条单化学品路（`/api/v2/emergency-response` · `/api/v2/sds-section` ·
`/api/v2/compliance`）回的是一个**布尔**，
不是多化学品那种 `unresolved: [名字…] + unresolved_detail`。渲染器此前把那个布尔翻译成
「Chemical not found in database」——**一句我们无权说的话**：`unresolved` 的成因里只有一种
是「库里没有」，其余是「畸形输入所以我们压根没搜」「有一级没跑成」「名字与 CAS 矛盾所以拒答」。

**实测（2026-09-03，本地 import server 打 Prod）**：`71-43`（一个 CAS 片段，后端明确
「did NOT search by it」）与 `zzqqxk-not-a-chemical`（真的一份记录都没有）拿到的那行
**逐字相同** ⇒ 模型据此对付费用户断言我们没有这份数据。同族 [[CI-770]] / [[CI-413]]，
以及 [[CI-587]]「守卫只护住结构化、散文照样断言未收录」。

🔴 变异（改一处，看它红）：
  1. 把 `get_sds_section` 那支改回 `lines.append("**Note:** Chemical not found in database.")`
     → `test_sds_section_*` 两条都红（一条因为出现了禁语，一条因为载荷里的真话没被渲染）。
  2. 把 `get_emergency_response` 那句换回旧文案 → `test_emergency_*` 红。
  3. 只把禁语改成同义的「no record in our database」而不改语义 → **仍然红**，
     因为断言打在 `_FORBIDDEN` 的**多个拼写**上，不是单一字符串。
  4. 把 `check_regulatory_compliance` 那句改回 → `test_regulatory_compliance_*` 红。
     🔴 第三处是**扩大作用域时扫出来的，票面只点名了两个端点** —— 判据是
     `grep -i "not found in|no record"` 全文件扫，不是照票面那两个名字改完就走。
     ⚠️ 还有一处**没动**：`create_audit_session` 的 `**Not found in database:**` 来自
     `sessions.py` 的**精确名/别名查表**（另一个产出者、另一个问题），没量过就别顺手改。
🔴 反方向（不该红的那半）：`test_resolved_section_still_renders_content` —— 正常解析出来的
调用必须照旧渲染正文。只验「不许说假话」而不验「该说的还在说」，会放过一个把整段输出
砍空的改动（同族：本仓「收紧闸门只验漏放、没验误伤」那条）。
"""
import asyncio

import server

# 断言打在**语义**上：任何形式的「我们库里没有它」都是这条路无权下的结论。
_FORBIDDEN = (
    "not found in database",
    "no record in our database",
    "not in our database",
)

# 后端在这条路上今天只给得出这两个键（2026-09-03 实测 Prod 载荷）。
EMERGENCY_UNRESOLVED = {
    "chemical": "71-43", "scenario": "spill",
    "data_source": "general", "unresolved": True,
    "immediate_actions": ["Alert nearby personnel"],
}

# 🔴 这条路不一样：载荷里**已经带着真话**，旧渲染器把它短路掉了。
SECTION_UNRESOLVED = {
    "chemical": "71-43", "section": 4, "content": None,
    "unresolved": True,
    "no_section_text": True,
    "no_section_text_reason": "unresolved",
    "no_section_text_note": (
        "This chemical's identity could not be resolved to a CAS number — this is NOT a "
        "finding of no hazard, only that we could not identify it."
    ),
}

SECTION_RESOLVED = {
    "chemical": "acetone", "cas": "67-64-1", "section": 4,
    "content": "Rinse mouth. Do NOT induce vomiting.",
    "unresolved": False,
}


def _run(tool, patch_name, payload, *args):
    async def _fake(*_a, **_k):
        return payload

    orig = getattr(server, patch_name)
    setattr(server, patch_name, _fake)
    try:
        res = asyncio.run(tool(*args))
        return res.content[0].text if hasattr(res, "content") else res
    finally:
        setattr(server, patch_name, orig)


def _assert_no_absence_claim(txt: str):
    low = txt.lower()
    for phrase in _FORBIDDEN:
        assert phrase not in low, (
            f"渲染器对一个只说明「没解析出身份」的载荷断言了 {phrase!r} —— "
            f"成因里只有一种是「库里没有」。全文：\n{txt}"
        )


def test_emergency_response_does_not_claim_absence_from_the_database():
    txt = _run(server.get_emergency_response, "_direct_emergency",
               EMERGENCY_UNRESOLVED, "71-43", "spill")
    _assert_no_absence_claim(txt)
    # 该说的还得说：这一次确实只有通用指引，别让「不说假话」退化成「什么都不说」。
    assert "general guidance" in txt.lower()


def test_sds_section_does_not_claim_absence_from_the_database():
    txt = _run(server.get_sds_section, "_direct_sds_section",
               SECTION_UNRESOLVED, "71-43", 4)
    _assert_no_absence_claim(txt)


def test_sds_section_renders_the_truthful_note_the_backend_already_sent():
    """这条路不需要后端改任何东西——真话早就在载荷里了。"""
    txt = _run(server.get_sds_section, "_direct_sds_section",
               SECTION_UNRESOLVED, "71-43", 4)
    assert SECTION_UNRESOLVED["no_section_text_note"] in txt, (
        "载荷里带着后端写好的真话，而模型读到的文本里没有它 —— "
        f"全文：\n{txt}"
    )


COMPLIANCE_UNRESOLVED = {
    "chemical": "71-43", "cas": None, "region_results": [],
    "summary_level": "unknown", "unresolved": True,
}


def test_regulatory_compliance_does_not_claim_absence_from_the_database():
    """第三处，票面没点名 —— 扩大作用域时扫出来的（`direct_compliance` 同样只回布尔）。"""
    txt = _run(server.check_regulatory_compliance, "_direct_compliance",
               COMPLIANCE_UNRESOLVED, ["71-43"])
    _assert_no_absence_claim(txt)


def test_resolved_section_still_renders_content():
    """反方向：正常解析出来的调用照旧给正文（防止把输出砍空）。"""
    txt = _run(server.get_sds_section, "_direct_sds_section",
               SECTION_RESOLVED, "acetone", 4)
    assert SECTION_RESOLVED["content"] in txt
