"""`get_storage_guidance` 的文本面：没有危害依据时要说出为什么，而不是三行 `N/A`。

**背景**：后端（CI-678，`020ff982` 已 Prod）在无依据时改成**整个不发**展示键
（`storage_class_label` / `cabinet_color` / `recommended_cabinet` / `temperature_requirement`），
并给出 `insufficient_hazard_data` + `insufficient_reason`。而本文件的渲染器写的是
`item.get("storage_class_label", "N/A")` ⇒ **默认值顶上**，用户读到三行 `N/A`、读不到原因。

🔴 **顺带修掉 CI-679 的第八处**：`_insufficient_lines` 的主句原本说
「the SDS record **we hold for this substance** parsed no hazard data」。
**三个消费者（storage / emergency / waste）的「判不了」都有两种成因**：`direct_service` 里
`resolved`（有 CAS）与 `has_canonical`（另查一次）是**独立判断**，`/emergency-response`
只在 `not resolved` 时早返回 ⇒ **有 CAS、无 canonical 行时照样落到这个载荷**。
对那一种，旧主句是**一句关于我们自己数据的肯定假话**。⇒ **直接改主句，三个面一起改**。
⚠️ 本文件最初写的是「只给 storage 换、emergency/waste 不动」，前提是「那两个面只有一种成因」——
**那个前提是错的**（trust 2026-08-28 指出，我回代码核实成立）。
"""
import pathlib

import pytest

import server as srv


def _render(item: dict) -> str:
    """只跑渲染那一段：把一条后端结果渲染成 storage 的文本面。"""
    return "\n".join(srv._storage_item_lines(item))


_INSUFFICIENT = {
    "chemical_name": "Unobtainium", "cas": "7440-00-0",
    "insufficient_hazard_data": True,
    "insufficient_code": "no_sds_on_file",
    "insufficient_reason": "no SDS hazard data on file for this substance",
}


def test_no_na_lines_when_insufficient():
    """🔴 本票的核心：三行 `N/A` 不许出现——它们看起来像「查过了，没有要求」。"""
    out = _render(_INSUFFICIENT)
    for label in ("Storage class:** N/A", "Cabinet color:** N/A",
                  "Recommended cabinet:** N/A", "Temperature:** N/A"):
        assert label not in out, f"仍在渲染 `{label}`：\n{out}"


def test_says_cannot_be_determined_and_why():
    out = _render(_INSUFFICIENT)
    assert "CANNOT BE DETERMINED" in out
    assert "NOT a low-hazard finding" in out          # 与 PPE / emergency / waste 同措辞
    assert _INSUFFICIENT["insufficient_reason"] in out  # 后端给的原因要透传


def test_neutral_clause_does_not_claim_we_hold_a_record():
    """🔴 storage 覆盖「一份 SDS 都没有」那种成因 ⇒ 主句不许声称我们持有一份记录。

    这条是本次改动的**唯一理由**。少了它，接上 helper 之后文本面会变成
    「我们持有的那份 SDS 没解析出危害数据」——而事实是我们一份都没有。
    """
    out = _render(_INSUFFICIENT)
    assert "record we hold" not in out, f"用了默认主句，对第二种成因字面为假：\n{out}"


def test_no_surface_anywhere_claims_we_hold_a_record():
    """🔴 CI-679 第八处：**整个仓**不许再出现「我们持有的那份记录…」这句主句。

    **我一开始写反了**：原来这里钉的是「emergency/waste 必须保留旧主句」，前提是
    「它们的判不了只有一种成因」。trust 2026-08-28 推翻了它，我回代码核实成立——
    `direct_service` 里 `resolved`（有 CAS）与 `has_canonical`（另查一次）是**独立判断**，
    `/emergency-response` 只在 `not resolved` 时早返回 ⇒ **三个面都会走到「有 CAS、无
    canonical 行」那条**，旧主句对它是假话。⇒ 那条测试钉的是**事故契约**，已删。

    🔴 **为什么按整个源码扫而不是逐个调用点**：backend 有一条同形的字面守卫，但它
    **只扫 backend 仓** ⇒ 跨不了仓，mcp 这处才活到今天（CI-679 票面写「一处」、实际七处，
    这是第八处）。按源码扫才能发现**将来新写的**第九处。
    变异方式＝**在仓里任何地方重新写下这句话**，不是改现有调用点。

    🔴 **本条自己也栽过一次**（trust 2026-08-28 抓到）：初版读的是 `srv.__file__`，
    **只扫 `server.py` 一个文件**，而名字承诺的是 "anywhere"。今天 `server.py` 恰好是
    唯一渲染面，所以结论没错——但**渲染逻辑一旦拆出第二个模块，它照样绿**。
    这与它要防的那个缺陷是**同一形状**（backend 的守卫跨不了仓、我这条跨不了文件）：
    **清单的作用域是手写的**。已改成扫整个包。
    """
    repo = pathlib.Path(srv.__file__).parent
    offenders = [
        f.relative_to(repo) for f in repo.rglob("*.py")
        if f.name != pathlib.Path(__file__).name
        and ".venv" not in f.parts and "node_modules" not in f.parts
        and "record we hold" in f.read_text(errors="ignore")
    ]
    assert not offenders, f"这些文件里又出现了那句「我们持有的那份记录…」：{offenders}"


def test_insufficient_block_is_actually_rendered_not_just_worded_right():
    """🔴 **沉默同样是失真**：守卫不能只钉「说得对不对」，还要钉「**有没有说**」。

    CI-679 那次的教训原话：把整组披露丢掉不渲染时，守卫**一开始 0 红**——因为它们
    全在检查措辞。这条专钉「渲染了没有」，与措辞无关。
    """
    for what in ("Storage class", "Scenario-specific response", "Waste classification"):
        out = "\n".join(srv._insufficient_lines({"insufficient_reason": "r"}, what))
        assert f"{what}: CANNOT BE DETERMINED" in out
        assert "NOT permission to proceed" in out


def test_sufficient_data_still_renders_the_normal_fields():
    """反向对照：有数据时一切照旧——否则这次改动可能是「把整段吞掉」而不是「说清楚」。"""
    out = _render({
        "chemical_name": "Acetone", "cas": "67-64-1",
        "storage_class_label": "Flammable liquids", "cabinet_color": "Yellow",
        "recommended_cabinet": "Flammables cabinet", "temperature_requirement": "< 30 °C",
    })
    assert "Flammable liquids" in out and "Yellow" in out
    assert "CANNOT BE DETERMINED" not in out
