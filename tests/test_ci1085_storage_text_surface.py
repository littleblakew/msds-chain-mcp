"""CI-1085：`get_storage_guidance` 的文本面 —— 过氧化物警告要到达，告诫别被埋进分号串。

**背景**：这些工具都是 `structured_output=False`，多数客户端只把 `content` 的 text
喂给模型 ⇒ **后端产出 ≠ 用户看见**。`_form_disclosure_lines` 头部记的是同一条，
那里已经栽过两次（CI-553 / CI-360），本票是第三个字段：`peroxide_former`
**在本票之前一次都没有被渲染过**。

两种失败形状，成因不同，别混：
- 过氧化物警告**根本没有文本面** —— 不是模型丢的，是我们没给它文本。
- 酸碱未定那句在文本里，但被 `"; ".join` 压成一串里的一项，于是被概括掉，
  而模型替它补了一个更像样的柜型。

🔴 **本文件守的是「有没有进 text」，不是「后端发了没有」。**

🔬 **变异（逐条实跑过，下面记的是跑出来的结果不是预测）**：
- M1 把 `_peroxide_former_lines(item)` 那行从 `_storage_item_lines` 里删掉
  → **3 红**（我预测 2，实测 3）：上面两条过氧化物的，**外加**
  `..._coverage_note_still_shown_when_not_classified` —— 因为那条免责也从这个出口出，
  删掉调用就把「有分类」和「没分类」两支一起端了。📌 记实测不记预测：
  下一个人看到「多红一条」才不会以为自己改坏了别的东西。
- M2 把 coverage_note 改成无条件渲染（分类存在时也渲染）
  → **1 红**：`..._coverage_note_suppressed_when_classified`。
- M3 把 `storage_requirements` 改回 `"- **Storage requirements:** " + "; ".join(...)`
  → **1 红**：`..._each_storage_requirement_on_its_own_line`。
- 空跑侧 M4 让 `_storage_item_lines` 直接 `return []`
  → **5 红**（全部），其中 `test_renderer_is_not_a_noop` 报得出原因（渲染结果是空的）。
  这一侧不能省：上面四条都是「某段文本在不在」，渲染器整体失灵时它们**全都以同一种
  方式失败**，而那与「这一条本来就不该出现」在断言消息里同形。
"""
import server as srv


def _render(item: dict) -> str:
    return "\n".join(srv._storage_item_lines(item))


# 取自一次真实后端返回（异丙醇 67-63-0）的形状，只留本票关心的键。
_IPA = {
    "chemical_name": "Isopropyl alcohol",
    "cas": "67-63-0",
    "storage_class_label": "Flammable Liquids",
    "cabinet_color": "Red",
    "recommended_cabinet": "Flammable storage cabinet (FM/UL approved)",
    "temperature_requirement": "Flash point 12.0°C, store below 15°C in flammable cabinet",
    "storage_requirements": [
        "Keep away from heat, sparks, open flames, and hot surfaces",
        "Ground and bond containers during transfer",
        "Store in a well-ventilated area",
    ],
    "peroxide_former": {
        "name": "Isopropanol (isopropyl alcohol)",
        "hazard_class": "B",
        "class_label": "Class B, forms peroxides on concentration/prolonged storage",
        "guidance": [
            "STOP, if you can see crystals or solid deposits (in the liquid, around the "
            "cap, or on the threads), do NOT move, twist, or open the container",
            "Test for peroxides before use once opened, and at least every 6 months",
        ],
        "citation": "Established peroxide-forming chemical classification (OSHA lab guidance)",
    },
    "peroxide_former_coverage_note": (
        "PEROXIDE-FORMER COVERAGE: no peroxide-forming classification is available for "
        "this substance in our curated list."
    ),
}

# 同一次调用里的双氧水：酸碱未定，那句告诫是 storage_requirements 的头一项。
_H2O2 = {
    "chemical_name": "Hydrogen peroxide",
    "cas": "7722-84-1",
    "storage_class_label": "Corrosives (acid vs base not determined)",
    "cabinet_color": "White",
    "recommended_cabinet": "Corrosives cabinet, segregate from BOTH acids and bases",
    "temperature_requirement": "Room temperature, dry area",
    "storage_requirements": [
        "We could NOT determine from this SDS whether this is an acid or a base, do not "
        "place it in an acid or a base cabinet on the strength of this answer",
        "Confirm from Section 9 (pH) of the original SDS or the container label",
        "Separate from oxidizers and organic materials",
        "Store below eye level",
    ],
    "peroxide_former_coverage_note": (
        "PEROXIDE-FORMER COVERAGE: no peroxide-forming classification is available for "
        "this substance in our curated list."
    ),
}


def test_renderer_is_not_a_noop():
    """防空跑：渲染器整体失灵时，下面四条会以和「这段本来就不该出现」同样的方式失败。"""
    out = _render(_IPA)
    assert len(out) > 200, f"渲染结果只有 {len(out)} 字符，渲染器多半没取到东西"
    # 阳性对照：这几段无论本票怎么改都该在
    assert "Isopropyl alcohol" in out
    assert "67-63-0" in out
    assert "Flammable storage cabinet" in out


def test_peroxide_class_reaches_text():
    """分类本身要进 text —— 它此前只活在 structured_content 里。"""
    out = _render(_IPA)
    assert "Class B" in out, "过氧化物分类没有进文本面"


def test_peroxide_stop_guidance_reaches_text():
    """🔴 真正救人的是这一条：看见结晶就别动容器。危害发生在开盖那一刻。"""
    out = _render(_IPA)
    assert "do NOT move, twist, or open the container" in out, (
        "过氧化物结晶的「别开盖」告诫没有进文本面"
    )


def test_coverage_note_suppressed_when_classified():
    """有分类时不许再说「我们没有这个物质的分类」—— 两句逐字打架。"""
    out = _render(_IPA)
    assert "no peroxide-forming classification is available" not in out, (
        "同时渲染了分类和「没有分类」，读者合理的反应是不信那条分类"
    )


def test_coverage_note_still_shown_when_not_classified():
    """🔴 反方向：没有分类时那句仍要在，别为了消矛盾把它一并删掉。

    它说的是「curated list 只收了一小部分，别把没命中读成不是过氧化物生成物」，
    那是这一格唯一的免责。
    """
    out = _render(_H2O2)
    assert "no peroxide-forming classification is available" in out


def test_each_storage_requirement_on_its_own_line():
    """告诫不许被压进一个分号串 —— 那样它是一个句子里的一个从句。"""
    out = _render(_H2O2)
    lines = [ln.strip() for ln in out.splitlines()]
    hit = [ln for ln in lines if ln.startswith("- We could NOT determine")]
    assert hit, (
        "「无法判断是酸是碱」那句不是独立一行 —— 它又被拼回长句里了。\n"
        f"实际渲染：\n{out}"
    )
    # 同一列表的其它项也必须各自成行，否则只是把第一项特殊处理了
    assert any(ln.startswith("- Store below eye level") for ln in lines)
