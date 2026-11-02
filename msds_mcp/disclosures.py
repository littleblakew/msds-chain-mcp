from __future__ import annotations







# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _form_disclosure_lines(item: dict) -> list[str]:
    """CI-572：把后端的 `physical_form_disclosure` 渲染进**文本**面。

    同一个 CAS 可以承载两种处置方式完全不同的形态（无水氟化氢 vs 氢氟酸水溶液），
    而我们对每个 CAS 只有一份 SDS ⇒ 不说清是哪一种，用户会默认「就是我手上这种」。
    后端此前只在 `/sds-section`、`/sds-document-url` 产出这个键，CI-572 把它接进了
    另外 6 个安全端点；**但后端产出 ≠ 用户看见**：这些工具都是
    `structured_output=False`，多数客户端只把 text 喂给模型，只透 structuredContent
    等于没修（CI-553/CI-360 各栽过一次，get_sds_section 那处的注释写的是同一件事）。

    `None` ＝ 无话可说（未判定且该 CAS 不在双形态名单上，或我们一份 SDS 都没有）
    ⇒ 什么都不说，绝不编一句「本品为某某形态」。
    """
    if not isinstance(item, dict):
        return []
    # 🔴 CI-615：稀释制剂披露走**同一个渲染出口**，理由与本函数存在的理由逐字相同
    # ——后端产出 ≠ 用户看见。第一版只改了后端载荷，而 `get_sds_document` 的
    # `structured_content` 是手写白名单、文本面只渲染形态那一条 ⇒ 走 MCP 打那条原始复现
    # （水 → TMSP 0.03%）**一个字都看不到**。放在形态披露**之前**：它说的是「这份 SDS
    # 描述的根本不是纯物质」，比「是哪一种形态」更靠前。
    out: list[str] = []
    # 🔴 CI-842：**排在最前面，因为它质疑的是「这条答案说的是不是你问的那个东西」**。
    # 下面两条都已经假定记录是对的、只在描述它（是制剂 / 是哪个形态）；本条说的是
    # 我们据以作答的记录**名字里没有你说的那个形态词** —— 一次静默的替换
    # （Prod 实测 `glacial ethanol` → 64-17-5 照母体作答、零披露）。先读到「答的可能
    # 不是这个东西」，比先读到「这个东西是水溶液形态」更靠前。
    #
    # 🔴 **不与 `physical_form_disclosure` 合并、也不二选一**（后端 `query_form_match`
    # 的注释是同一条，这里不重复论证）：一个由**记录**驱动、一个由**查询与命中名之差**
    # 驱动，两者可以同时出现且不矛盾。合并会丢掉其中一半。
    #
    # 🔴 措辞是后端渲染的（5 语言单源在 i18n catalog），**别在这里重写**——理由与本函数
    # 下面那两条逐字相同，第二份不带版本的副本会各漂各的。
    if qform := item.get("query_form_disclosure"):
        out.append(f"- ⚠️ {qform}")
    # 🔴 CI-237-a：用户在名字里写了浓度（`50% 乙醇`），而作答那份 SDS 标的是别的浓度
    # 或没标。后端在六个单物质端点的条目上（急救是顶层）给出已渲染好的一句话；
    # 它与 `documents[].concentration_mismatch` 不同，不挂在「有没有 PDF」上，
    # 所以储存这类根本不带 documents 的工具也拿得到。原样念，措辞归后端 catalog。
    if conc := item.get("concentration_disclosure"):
        out.append(f"- ⚠️ {conc}")
    if prep := item.get("preparation_disclosure"):
        out.append(f"- ⚠️ {prep}")
    note = item.get("physical_form_disclosure")
    # 🔴 **不给整句再套一层 `**`**：这句话在 zh/ja/de/id 四种语言里**自带**加粗标记，
    # 而且加粗的正是否定词（zh「我们**没有**这个 CAS…」/ de「**keine**」/ id「**tidak**」）。
    # 再套一层会拼出 `**A**B**C**`，markdown 客户端于是把 A、C 加粗、把中间那个否定词
    # 渲染成普通文字 —— **强调恰好反了**，最该显眼的那半句反而最不显眼。
    # （英文那条不带 `**`，所以这个 bug 只咬另外四种语言，本仓的默认 lang 恰恰是 zh。）
    if note:
        out.append(f"- ⚠️ {note}")
    out.extend(_content_source_lines(item))
    return out


_SUPPLIER_SDS_SOURCES = frozenset({"canonical", "system_library", "session"})


def _content_source_lines(item: dict) -> list[str]:
    """CI-938：作答那一行的出处（后端 `content_source`），六个单物质工具共用这一个出口。

    后端此前只从 canonical 作答；现在 canonical 不覆盖的 CAS 也会从语料或 PubChem 的
    GHS 分类汇总作答。文本面不说是哪一种，模型会照 server instructions 把它引成
    「某家供应商的 SDS」⇒ PubChem 那一种必须明说「不是供应商 SDS」。

    键缺席或为 `None` ＝ 没有作答行 ⇒ 什么都不说。没有供应商名时也不说
    （同 `get_sds_section`：绝不写 "unknown supplier"）。
    """
    cs = item.get("content_source") if isinstance(item, dict) else None
    if not isinstance(cs, dict):
        return []
    source = cs.get("source")
    if source == "pubchem_ghs":
        return ["- ⚠️ **Answering record:** PubChem aggregated GHS classification, "
                "not a supplier SDS. Do not cite it as one."]
    # 白名单是「已知出自供应商 SDS」的那几种；认不出的 source 往少声称那侧落，
    # 否则一种新的非供应商来源会在下面「没有供应商名就不说」那条里静默。
    if source not in _SUPPLIER_SDS_SOURCES:
        return [f"- ⚠️ **Answering record:** source `{source or 'unspecified'}`, "
                "not identified as a supplier SDS. Do not cite it as one."]
    if not cs.get("supplier"):
        return []
    rev = f" · revision {cs['revision_date']}" if cs.get("revision_date") else ""
    return [f"- **Answering SDS:** {cs['supplier']}{rev}"]


def _insufficient_lines(item: dict, what: str) -> list[str]:
    """CI-360: 把后端的「判不了」渲染成人看得懂的文字，而不是一行 `Data source: none`。

    背景：后端（CI-243）在「记录在库、但没有危害数据」时会返回
    `insufficient_hazard_data: true` + `insufficient_reason`，但这一面**只渲染
    `data_source`**。多数 MCP 客户端只读 text（`structuredContent` 不进模型上下文），
    于是用户看到的是「一段空荡荡的输出 + Data source: none」——`none` 不在任何既有
    枚举里，模型多半读成「来源未知但建议有效」，**比修之前的 hcode_mapping 更含混**。

    🔴 与 PPE 那段（本文件更早的 `CANNOT BE DETERMINED` 分支）保持同一措辞：
    说清「判不了」、说清「这不是低危结论」、明确禁止模型用常识补空。
    """
    reason = item.get("insufficient_reason")
    # 🔴 **主句不许声称我们持有一份记录**（CI-679 同族的一处）。
    # 旧句是「the SDS record **we hold for this substance** parsed no hazard data」，
    # 而三个消费者（storage / emergency / waste）的「判不了」**都有两种成因**：
    # `direct_service` 里 `resolved`（有 CAS）与 `has_canonical`（另查一次）是**独立判断**，
    # `/emergency-response` 只在 `not resolved` 时早返回 ⇒ **CAS 解析得出但没有 canonical 行时
    # 照样落到这个载荷**。对那一种，旧句是**一句关于我们自己数据的肯定假话**，
    # 且失效方向很坏：用户因此更不会自己去找 SDS。
    # ⚠️ 我一度以为 emergency/waste 只有一种成因、想加个开关只给 storage 用 —— 那是错的，
    # **加开关反而会留下「哪些调用方传了」这份要人记得维护的名单**。
    # 📌 backend 有字面守卫钉这两句话（`test_ci679_no_record_wording.py`），但它**只扫 backend 仓**
    # ⇒ 跨不了仓，这处才活到今天。本仓自己的守卫在 `tests/rendering/test_storage_insufficient_disclosure.py`。
    lines = [
        f"- **{what}: CANNOT BE DETERMINED** — we have no SDS hazard data on file "
        "for this substance that we can answer from. This is NOT a "
        "low-hazard finding and NOT permission to proceed. Do not infer it from "
        "general knowledge; upload this substance's SDS or try another supplier's "
        "record.",
    ]
    if reason:
        lines.append(f"  - Why: {reason}")
    return lines


def _peroxide_former_lines(item: dict) -> list[str]:
    """把过氧化物生成物的警告渲染进**文本**面。

    🔴 **在 CI-1085 之前，`peroxide_former` 在本仓一次都没有被渲染过** —— 它只存在于
    `structured_content` 里，而这些工具都是 `structured_output=False`、多数客户端只把
    text 喂给模型 ⇒ 对那些用户**它等于不存在**。这与 `_form_disclosure_lines` 头部记的是
    同一条（「后端产出 ≠ 用户看见」），那里已经栽过两次，这是第三个字段。
    ⇒ 加新展示字段时，判据是「**它有没有进 text**」，不是「后端发了没有」。

    危害是**开容器那一刻**：过氧化物结晶受摩擦或撞击可能起爆，所以那条「看见结晶就别动」
    必须排在储存要求之前 —— 读者按顺序读，而他可能已经站在柜子前面了。

    `peroxide_former_coverage_note` **只在没有分类时**渲染：后端命中分类时把它置为 `null`
    （[[CI-706]]），这里仍按「有分类就不读它」写，不依赖后端那一侧——两者同时说出来
    就成了「这是 Class B」紧跟着「我们没有这个物质的分类」，两句逐字打架。
    """
    if not isinstance(item, dict):
        return []
    pf = item.get("peroxide_former")
    if not isinstance(pf, dict) or not pf:
        note = item.get("peroxide_former_coverage_note")
        return [f"- {note}"] if note else []

    label = pf.get("class_label") or pf.get("hazard_class") or "peroxide former"
    out = [f"- **Peroxide former:** {label}"]
    for g in pf.get("guidance") or []:
        out.append(f"  - {g}")
    if pf.get("citation"):
        out.append(f"  - Basis: {pf['citation']}")
    return out


def _storage_item_lines(item: dict) -> list[str]:
    """渲染一条 storage 结果。抽成函数是为了能直接测文本面——这一段的失效方式
    （渲染出一串 `N/A`）在集成测试里看起来完全正常。

    🔴 **没有危害依据时不许渲染那四行 `N/A`**：后端（CI-678）在无依据时**整个不发**
    展示键，而 `item.get(k, "N/A")` 的默认值会顶上去 —— 用户读到的是四行 `N/A`，
    那看起来像「查过了，没有存储要求」，而事实是「我们判不了」。**没有结论被读成没有危害**，
    与 [[CI-570]] / [[CI-277]] 同形。
    """
    lines: list[str] = []
    lines.append(f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})")
    lines.extend(_form_disclosure_lines(item))  # CI-572
    # 🔴 CI-586：柜型是按**另一份** SDS 的更严分类给的时候，那句「是哪一家、
    # 哪一版说的」必须进文本面 —— 否则模型看到的是一个没有出处的加严，
    # 而它引用的 supplier 恰恰是**没有**这个分类的那一份（可追溯性反噬）。
    if item.get("hazard_classification_conflict_note"):
        lines.append(f"- {item['hazard_classification_conflict_note']}")
    if item.get("insufficient_hazard_data"):
        lines.extend(_insufficient_lines(item, "Storage class"))
    else:
        lines.append(f"- **Storage class:** {item.get('storage_class_label', 'N/A')}")
        lines.append(f"- **Cabinet color:** {item.get('cabinet_color', 'N/A')}")
        lines.append(f"- **Recommended cabinet:** {item.get('recommended_cabinet', 'N/A')}")
        lines.append(f"- **Temperature:** {item.get('temperature_requirement', 'N/A')}")
    lines.extend(_peroxide_former_lines(item))
    reqs = item.get("storage_requirements", [])
    if reqs:
        # 🔴 CI-1085：**一条一行，不要再用 `"; ".join` 拼成一个长句**。
        # 这个列表里混着两种东西：几条按危害类别推的通用要求（「与碱、氧化剂分开」），
        # 和偶尔出现的一条**「我判不出来，别据此做某事」**（酸碱未定时后端发的那句）。
        # 拼成一行之后，后者是七项里的一项，而下游模型是在**转述**这一行而不是照抄它
        # ⇒ 它被概括掉的时候，读者看到的是一份读起来很完整、只是少了那句告诫的答案。
        # 观察到的形状：酸碱未定那句没有到达用户，而模型替它补了一个更像样的柜型。
        # 一条一行不保证模型一定转述，但它不再是**一个句子里的一个从句**。
        lines.append("- **Storage requirements:**")
        lines.extend(f"  - {r}" for r in reqs)
    # CI-370：GHS 官方存储段处置语（通风/密闭/阴凉/避光/上锁），每条带 P 码。
    # 与上面的 storage_requirements 分开渲染：那是按危害类别推的柜型/温度要求，
    # 这是官方指派语 —— 出处不同，别混成一段（同 get_emergency_response）。
    conditions = item.get("precaution_conditions", [])
    if conditions:
        lines.append("- **GHS Standard Precautions (storage):** "
                     + "; ".join(str(c) for c in conditions))
    incompatible = item.get("incompatible_materials", [])
    if incompatible:
        lines.append("- **Incompatible materials:** " + ", ".join(str(m) for m in incompatible))
    nfpa = item.get("nfpa_ratings", {})
    if nfpa:
        lines.append("- **NFPA ratings:** " + ", ".join(f"{k.title()} {v}" for k, v in nfpa.items()))
    lines.append("")
    return lines
