from __future__ import annotations

import httpx
import json as _json
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from typing import Annotated

from msds_mcp.app import API_URL, LANG, TIMEOUT, mcp
from msds_mcp.blocks import (_no_hazard_basis_block, _pair_sides, _precursor_disclosure_block, _precursor_index, _precursor_pair_note, _query_form_disclosure_block, _unresolved_block)
from msds_mcp.calls import (_graceful_timeout, _headers, _intent_params, _log_intent, _quick_chat, _reported, _strip_usage, _supplier_params)
from msds_mcp.direct import (_direct_alternatives, _direct_compat, _direct_online_search, _direct_sds_section, _direct_waste, _format_alternatives)
from msds_mcp.disclosures import _form_disclosure_lines, _insufficient_lines
from msds_mcp.params import (Chemical, ChemicalList, Intent, Lang, QuickLang, Suppliers, _normalize_lang, _require_api_key, _text_result)
from msds_mcp.regulatory_lists import _unresolved_boolean_note
from msds_mcp.render import _chemicals_from_response, _quick_result



# 🔴 CI-1080：这段 docstring 曾写着「or to get the canonical CAS number for a
# chemical name」。这个工具落在 `GET /chemicals?q=` —— listing 面，返回模糊候选
# 是设计内的（下面 `_mismatch` / `substance_no_cas` 那几段都在解释「这一行不进
# 判定」）。把它描述成身份解析器，消费端的模型就会把候选行当成身份结论，而
# 判定面那条路本来是对的 ⇒ 错不在检索，在描述。
# **别把「返回 canonical CAS」这类说法加回来**；要给身份就指向判定工具。
@mcp.tool(annotations=ToolAnnotations(title="Search Chemical Database", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_reported
async def search_chemical_database(query: Annotated[str, Field(
    description='A single chemical name, synonym, or CAS number, e.g. "methanol", '
                '"wood alcohol", "67-56-1". Not a natural-language question — use '
                'ask_chemical_safety for those.',
)], intent: Intent = None) -> str:
    """
    Search the MSDS Chain database for a specific chemical.

    Returns a ranked list of CANDIDATE records for confirmation, each with CAS
    number, chemical name, NFPA ratings (flammability, health, reactivity), GHS
    classification, and whether full MSDS data is available. A candidate row is
    not a decided identity: near-matches are expected here by design, and the
    response says why a query resolved the way it did.

    Use this to browse what the database holds and to pick the right record.
    Do NOT read the top row as "the CAS number for this name". For a decided
    identity, and for any hazard, compatibility, storage or compliance verdict,
    call ask_chemical_safety or the corresponding judgement tool, which apply
    the identity checks this listing surface deliberately does not.

    Args:
        query: Chemical name, synonym, or CAS number, e.g.
               "methanol", "wood alcohol", "67-56-1"
    """
    error_msg = None
    success = True
    try:
        if err := _require_api_key():
            return err
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            res = await client.get(
                f"{API_URL}/chemicals",
                # 🔴 CI-770/CI-413：`with_reason` 让后端**说出空结果的成因**。
                # 不带它时后端对三种完全不同的空返回同一个 `[]`：
                #   · 按策略没查（`71-43` 这种数字片段，CI-693 的闸）
                #   · 没有这个**形态**的条目（`浓硫酸`；母体 7664-93-9 有 5 行 canonical）
                #   · 确实没有
                # 而我们下面那句写死的话在**前两种情况下是假的**，还会把用户推向
                # 「上传 SDS」这个对他无效的动作。
                # ⚠️ 老后端不认这个参数会**忽略**它并照旧返回裸 list，下面的取值两种
                # 形状都认 ⇒ 两仓部署顺序不敏感。
                params={"q": query, "with_reason": 1},
                headers=_headers(),
            )
            if res.status_code == 200:
                data = res.json()
                chemicals = data if isinstance(data, list) else data.get("chemicals", [])
                if not chemicals:
                    # 🔴 后端说得出成因就**原样转述它**，别用我们这句更强的兜底覆盖它。
                    # 这句兜底断言的是「库里没有」——一个我们并没有验证过的库存事实。
                    _unresolved = data.get("unresolved") if isinstance(data, dict) else None
                    if isinstance(_unresolved, dict) and _unresolved.get("reason"):
                        return str(_unresolved["reason"])
                    return f'No chemicals found matching "{query}" in the MSDS Chain database.'
                # 🔴 CI-322: 无 CAS 行由后端**追加在结果尾部**（只在前两层没填满时才
                # 补），而这里只渲染前 5 条 —— 一个名字片段撞上 5~9 条无关的普通物质，
                # 用户真正要找的那条无 CAS 记录连同它的 GHS 和披露就被整段切掉，抬头
                # 却仍写着 "Found 10 result(s)"。模型完全看不出发生过截断。
                # 所以按类别分别取：普通行 5 条的预算不变，无 CAS 行**单独保留名额**
                # —— 它们是唯一带披露义务的一类，被截掉等于披露没发生。
                # 🔴 CI-979: `match_type == "cas_mismatch"` 的行要单独留名额，且排最前。
                # 那是**用户自己打进来的那个 CAS**，我们库里确实有这条记录，只是查询的
                # 名字半边没能与它相互印证（后端 CI-210 的 display fallthrough）。后端把它
                # **追加在结果末尾**（`rank=6`），并在 `chemical_resolver` 里写明这是个
                # 呈现问题而非排序结论："a consuming UI should surface this row as a
                # MISMATCH WARNING rather than as the weakest suggestion. The resolver
                # cannot enforce that presentation."
                # ⇒ 不留名额时它排在候选列表最后，`[:5]` 会把它整条切掉：屏幕上只剩一批
                # 名字相近的**别的**物质，而用户逐字打进来的那个号一次都不出现。
                # 🔴 排在最前 ≠ 升格成身份：它仍然不进判定（下面 `included_in_assessment`
                # 显式 False），文案也必须说出「没能印证」这件事。
                def _is_cas_mismatch(c: dict) -> bool:
                    return (c.get("match_type") or "") == "cas_mismatch"

                _mismatch = [c for c in chemicals if _is_cas_mismatch(c)]
                _no_cas = [c for c in chemicals
                           if not _is_cas_mismatch(c)
                           and (c.get("record_kind") or "") == "substance_no_cas"]
                _ordinary = [c for c in chemicals
                             if not _is_cas_mismatch(c)
                             and (c.get("record_kind") or "") != "substance_no_cas"]
                shown = _mismatch[:2] + _ordinary[:5] + _no_cas[:3]
                omitted = len(chemicals) - len(shown)
                lines = [f"Found {len(chemicals)} result(s) for '{query}':\n"]
                struct_results = []
                for c in shown:
                    name = c.get("name") or c.get("chemical_name", "Unknown")
                    cas = c.get("cas_number", "—")
                    flam = c.get("flammability", "—")
                    tox = c.get("toxicity", "—")
                    # CI-277: a formulated product (mixture) has no CAS of its own and
                    # its GHS classification describes the whole formulation. Label it
                    # so the caller never reads it as a substance record.
                    kind = c.get("record_kind") or "substance"
                    if _is_cas_mismatch(c):
                        # 🔴 CI-979 —— 措辞要同时容纳**两种**成因，因为后端这一条行
                        # 对二者是同形的：①名字半边指向了别的已知物质（真矛盾）
                        # ②名字半边什么也没印证（沉默，例如那个俗名不在别名表里）。
                        # 说死成「你的名字和 CAS 互相矛盾」在②上是假话。
                        # ⇒ 只陈述我们确实知道的那件事：这个 CAS 我们有、名字没印证上、
                        # 这一行不参与任何判定，并给出**两个方向各自可执行的下一步**。
                        lines.append(
                            f"• **{name}** (CAS: {cas}) — ⚠️ this is the CAS number "
                            f"you typed, and we DO hold a record for it. It is listed "
                            f"separately because the name half of your query could not "
                            f"be corroborated against it, so this row is NOT an "
                            f"identified chemical: no hazard, compatibility or storage "
                            f"verdict is computed from it. Two possibilities, and we "
                            f"cannot tell them apart: the name is a synonym we do not "
                            f"hold, or the CAS belongs to a different substance than "
                            f"the name. Next step — if the CAS is the one on your "
                            f"container, re-run the search with `{cas}` alone; "
                            f"otherwise check the name against your source document."
                        )
                    elif kind == "substance_no_cas":
                        # CI-322 B2: a legitimately CAS-less substance (a newly
                        # synthesised building block that has never been assigned
                        # one). We DO hold its supplier SDS and its full GHS, so
                        # withholding is the more dangerous option — an empty
                        # hazard field reads downstream as "no hazard". Quote what
                        # we have AND say plainly it is not part of any verdict.
                        ghs = c.get("ghs") or {}
                        h_codes = ", ".join(ghs.get("hazard_statements") or []) or "—"
                        catalog = c.get("catalog_number") or "—"
                        lines.append(
                            f"• **{name}** — no CAS number (supplier catalog "
                            f"{catalog}, {c.get('supplier') or 'unknown supplier'})\n"
                            f"  GHS from the supplier SDS, quoted verbatim: "
                            f"{ghs.get('signal_word') or '—'} / {h_codes}\n"
                            f"  🔴 This record has NO CAS number, so it is NOT "
                            f"included in compatibility, storage or hazard "
                            f"assessment — those need a CAS-level identity. Report "
                            f"both halves to the user; do not present it as assessed."
                        )
                    elif kind == "product":
                        lines.append(
                            f"• **{name}** — formulated product (mixture, no single CAS)\n"
                            f"  Its hazard classification applies to the whole "
                            f"formulation and cannot be extrapolated to any single "
                            f"ingredient. Not accepted as an input to compatibility "
                            f"or batch safety checks."
                        )
                    else:
                        lines.append(
                            f"• **{name}** (CAS: {cas})\n"
                            f"  Flammability: {flam}  |  Toxicity: {tox}"
                        )
                    struct_results.append({
                        "name": name,
                        "cas_number": c.get("cas_number"),
                        "record_kind": kind,
                        "flammability": c.get("flammability"),
                        "toxicity": c.get("toxicity"),
                        # CI-322: machine-readable half of the disclosure. Present
                        # on every row (True for ordinary substances) so a caller
                        # reading this field never has to infer exclusion from a
                        # missing key — absence and False must not look the same.
                        # 🔴 CI-979：`cas_mismatch` 行**必须**是 False。后端这条
                        # 通道不返回 `included_in_assessment`，于是默认值
                        # `kind == "substance"` 让它一直是 True —— 机器可读的那一面
                        # 在说「这条已纳入判定」，与文本面和 `is_identity_grade_candidate`
                        # 都相反。默认值那半照旧，不动其它行。
                        "included_in_assessment": (
                            False if _is_cas_mismatch(c) else c.get(
                                "included_in_assessment", kind == "substance"
                            )
                        ),
                        # CI-979：把后端的 `match_type` 原样带出来，让调用方不必从
                        # 文案里反推「这一行为什么长得不一样」。
                        "match_type": c.get("match_type"),
                        **({"catalog_number": c.get("catalog_number"),
                            "ghs": c.get("ghs"),
                            "disclosure": c.get("disclosure")}
                           if kind == "substance_no_cas" else {}),
                    })
                if omitted > 0:
                    # 截断必须说出来。静默的上限读起来和「这就是全部」一模一样。
                    lines.append(
                        f"\n_({omitted} further result(s) not shown — refine the query "
                        f"or search by CAS / supplier catalog number.)_"
                    )
                return CallToolResult(
                    content=[TextContent(type="text", text="\n".join(lines))],
                    structured_content={
                        "query": query,
                        "result_count": len(chemicals),
                        "results": struct_results,
                    },
                )
            return f"Chemical search failed (HTTP {res.status_code}). Try a different name or CAS number."
    finally:
        _log_intent("search_chemical_database", [query],
                        _intent_params({"query": query}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Search MSDS Online (PubChem)", read_only_hint=True, destructive_hint=False, open_world_hint=True), structured_output=False)
@_graceful_timeout
@_reported
async def search_msds_online(
    chemical_name: Annotated[str, Field(
        description='Chemical name to look up on PubChem, e.g. "acetonitrile". '
                    'Supply this or cas_number (or both).',
    )] = "",
    cas_number: Annotated[str, Field(
        description='CAS number, e.g. "75-05-8". Used in preference to chemical_name '
                    'when both are given, because it identifies the substance exactly.',
    )] = "", intent: Intent = None,
) -> "CallToolResult | str":
    """
    Look up GHS hazard data for a chemical NOT in the MSDS Chain database, via PubChem.

    Use this ONLY as a fallback when search_chemical_database returns no result. The
    data is PubChem's AGGREGATED GHS classification, clearly labelled source="pubchem"
    — it is NOT a signed supplier SDS. Present it to the user as PubChem-sourced and
    unverified; prefer uploading a real SDS (upload_msds_pdf) when accuracy matters.

    Args:
        chemical_name: Chemical name, e.g. "acetonitrile"
        cas_number:    CAS number, e.g. "75-05-8" (used first if provided)
    """
    error_msg = None
    success = True
    try:
        if err := _require_api_key():
            return err
        data = await _direct_online_search(chemical_name, cas_number)
        status = data.get("status")
        if status != "found":
            return data.get("message") or (
                f"'{chemical_name or cas_number}' not found on PubChem. Upload an SDS or skip."
            )
        ghs = data.get("ghs") or {}
        cas = data.get("cas_number") or "—"
        name = data.get("chemical_name") or chemical_name or cas
        lines = [f"**{name}** (CAS: {cas}) — PubChem aggregated GHS (NOT a signed SDS):"]
        if ghs.get("signal_word"):
            lines.append(f"Signal word: {ghs['signal_word']}")
        hcodes = ghs.get("h_codes") or []
        if hcodes:
            lines.append("Hazard codes: " + ", ".join(hcodes[:15]))
        if ghs.get("pictograms"):
            lines.append("Pictograms: " + ", ".join(ghs["pictograms"]))
        lines.append("\n⚠ Source: PubChem aggregated GHS — not a verified supplier SDS. "
                     "Upload the actual SDS for an authoritative safety check.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content={
                # `query` 是**调用方问的那个词**，`chemical_name` 是 PubChem 解析出的名字，
                # 两者可以不同 ⇒ 都给，别用一个盖掉另一个（此前只有 query）。
                "query": chemical_name or cas_number,
                "chemical_name": data.get("chemical_name") or "",
                "cas_number": data.get("cas_number") or "",
                "status": data.get("status"),          # found / not_found，机器可判
                "completeness": data.get("completeness"),
                "source": "pubchem",
                "ghs": ghs,
            },
        )
    finally:
        _log_intent("search_msds_online", [chemical_name or cas_number],
                    _intent_params({"chemical_name": chemical_name, "cas_number": cas_number}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get SDS Section", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_sds_section(
    chemical: Chemical,
    section: Annotated[int, Field(
        ge=1, le=16,
        description="GHS-SDS section number, 1-16. Common ones: 2 hazards, 4 first aid, "
                    "5 fire-fighting, 6 accidental release, 7 handling & storage, "
                    "8 exposure controls & PPE, 9 physical properties, "
                    "13 disposal, 14 transport.",
    )],
    lang: Lang = None, intent: Intent = None,
) -> str:
    """
    Retrieve a specific SDS (Safety Data Sheet) section for a chemical.

    The 16 standard GHS-SDS sections are:
      1. Identification
      2. Hazard(s) identification
      3. Composition / ingredients
      4. First-aid measures
      5. Fire-fighting measures
      6. Accidental release measures
      7. Handling and storage
      8. Exposure controls / PPE
      9. Physical and chemical properties
      10. Stability and reactivity
      11. Toxicological information
      12. Ecological information
      13. Disposal considerations
      14. Transport information
      15. Regulatory information
      16. Other information

    Use this when you need detailed data from a specific section rather than
    a general safety overview.

    Args:
        chemical: Chemical name or CAS number
        section:  SDS section number (1-16)
    """
    error_msg = None
    success = True
    try:
        if section < 1 or section > 16:
            return "Section number must be between 1 and 16."

        section_names = {
            1: "Identification", 2: "Hazard(s) identification",
            3: "Composition/ingredients", 4: "First-aid measures",
            5: "Fire-fighting measures", 6: "Accidental release measures",
            7: "Handling and storage", 8: "Exposure controls/PPE",
            9: "Physical and chemical properties", 10: "Stability and reactivity",
            11: "Toxicological information", 12: "Ecological information",
            13: "Disposal considerations", 14: "Transport information",
            15: "Regulatory information", 16: "Other information",
        }
        sec_name = section_names[section]
        data = await _direct_sds_section(chemical, section, lang)
        if data.get("error"):
            return _text_result(f"SDS section error: {data['error']}")
        chem_display = data.get("chemical", chemical)
        cas = data.get("cas", "N/A")
        content = data.get("content")
        lines = [
            f"**SDS Section {section}: {sec_name}**",
            f"Chemical: {chem_display} (CAS: {cas})\n",
        ]
        if data.get("unresolved"):
            # 🔴 CI-714：这一支此前写死「Chemical not found in database.」，而**载荷里已经带着
            # 真话**：`no_section_text_note` 说的是「身份没能解析到 CAS —— 这不是「无危害」的
            # 结论」。旧写法把它整个短路掉了，正是下面 CI-408 那段注释在防的事，只是那段注释
            # 管不到这条 early branch。实测：`71-43`（没搜过）与乱码串输出逐字相同。
            # 🔴 只有当那条 note 确实是**为这个成因**写的才用它（review 抓到）：
            # `no_section_text_note` 由 `no_section_text_reason` 决定，三种取值里另外
            # 两种（`no_sections_parsed` / `section_not_present`）说的是「我们持有这份
            # SDS 但…」——对一个身份都没解析出来的输入，那是**更强**的一句假话。
            note = data.get("no_section_text_note")
            if data.get("no_section_text_reason") != "unresolved":
                note = None
            lines.append(note or _unresolved_boolean_note(lang))
        elif content:
            # CI-1102: the text below may come from a different SDS than the cited
            # record. Say so BEFORE the text — a note placed after it is read as
            # an afterthought, and the Source line below would otherwise attach
            # this text to the canonical supplier.
            if data.get("section_source_differs") and data.get("section_source_note"):
                lines.append(f"⚠️ {data['section_source_note']}\n")
            lines.append(content)
        else:
            # CI-408: 这条工具是 structured_output=False —— LLM 读的是这段文本，
            # 不是 structuredContent。后端把「为什么没有正文」建模成了
            # no_section_text_note（三种原因：整份没解析出分段 / 这一节不在里面 /
            # 没有 canonical 记录），如果这里仍然回那句无区分度的固定文案，
            # 后端那个修复对模型来说等于不存在。
            #
            # 🔴 为什么必须说清「这是数据缺口，不是危害结论」：本项目栽过——
            # 「空」在下游被读成「无危害」，40% HF 的储存建议因此掉进普通柜。
            lines.append(
                data.get("no_section_text_note")
                or "No data available for this section in the canonical SDS."
            )
        # CI-308: this section's text may come from a different underlying SDS
        # record than get_sds_document's — surface the supplier/revision here too
        # so a mismatch between the two tools is visible without reading the raw
        # section text. Omit the line entirely (never "unknown supplier") when the
        # backend doesn't provide it, since that would misleadingly imply a
        # deliberate "no source" answer rather than a field the backend omitted.
        if not data.get("unresolved") and data.get("supplier"):
            region_suffix = f" · {data['region']}" if data.get("region") else ""
            label = ("Cited record (not the source of the text above)"
                     if content and data.get("section_source_differs") else "Source")
            lines.append(f"\n- **{label}:** {data['supplier']}{region_suffix}")
            if data.get("revision_date"):
                lines.append(f"- **Revision date:** {data['revision_date']}")
        # CI-838 / CI-1102: when the text was borrowed from a corpus record, that
        # record's provenance is the only citation the text has — without this
        # line the model gets safety text with no source at all (CI-838 path:
        # `supplier` is empty). No id on purpose: CI-871.
        csi = data.get("corpus_source_info") or {}
        if not data.get("unresolved") and content and csi:
            who = " · ".join(str(v) for v in (csi.get("supplier"), csi.get("source")) if v)
            if who:
                gap = "" if data.get("supplier") else "\n"
                lines.append(f"{gap}- **Text source:** {who}")
            if csi.get("revision_date"):
                lines.append(f"- **Text revision date:** {csi['revision_date']}")
        # 🔴 CI-347：同一个 CAS 可以是两种形态（无水氟化氢 vs 氢氟酸水溶液），
        # 而**储存/泄漏处置/急救都不同**。后端把「这份数据是哪种形态、另一种我们没有」
        # 建模成了 `physical_form_disclosure`；这条工具是 structured_output=False，
        # **LLM 读的是这段文本**，不放进来后端那个修复对模型就等于不存在
        # ——CI-408 已经栽过一次同样的形态，别再来第三次。
        #
        # `None` ＝ 未判定（不是「只有一种形态」）⇒ 那时**什么都不说**，
        # 别编一句「本品为某某形态」出来。
        # CI-572：改走 `_form_disclosure_lines`（此前是与它逐字相同的手写一份）。
        # 格式留两份 ⇒ 改一处漏另一处，七条工具的披露会长成两个样子。
        lines.extend(_form_disclosure_lines(data))
        lines.append(f"\n*Data source: {data.get('data_source', 'unknown')}*")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("get_sds_section", [chemical],
                    _intent_params({"chemical": chemical, "section": section}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get Chemical Alternatives", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_chemical_alternatives(
    chemical: Chemical,
    use_case: Annotated[str, Field(
        description='Optional context for how the chemical is used, which changes what '
                    'counts as a viable substitute, e.g. "degreasing solvent", '
                    '"extraction solvent for organic synthesis", "cleaning agent for labware".',
    )] = "",
    lang: Lang = None,
) -> str:
    """
    Suggest safer alternatives for a chemical, considering its intended use.

    Returns 2-4 alternative chemicals with: name, CAS number, why it's safer
    (lower toxicity, higher flash point, non-CMR, etc.), any trade-offs
    (cost, availability, performance), and relevant regulatory context
    (e.g., REACH SVHC substitution requirement).

    Use this when a chemical is flagged as high-risk, restricted, or when the
    user is exploring greener chemistry options.

    Args:
        chemical: Chemical name or CAS number to find alternatives for
        use_case: Optional context about how the chemical is being used, e.g.
                  "degreasing solvent", "extraction solvent for organic synthesis",
                  "cleaning agent for labware"
    """
    error_msg = None
    success = True
    try:
        # CI-137：此前拼一句英文 prompt 交给 `_quick_chat`（RAI→intent→summary 三轮 LLM），
        # 延迟是确定性端点的若干倍；而后端 `agent/tools/chemical_substitution.py` 早就是
        # 确定性实现（curated 替代表 + `resolve_cas` + GHS 风险比较，全文件零 LLM 引用）。
        # 同 [[CI-523]] 一族：**信息在，只是这条通道没去拿。**
        # 🔴 保留凭证检查：`_quick_chat` 会 `_require_api_key()`，`_direct_*` 不会 ⇒
        # 直接换端点会把这个工具顺带变成匿名可调（CI-523 踩过）。
        if err := _require_api_key():
            success, error_msg = False, "no_credential"
            return _text_result(
                f"Authentication required: {err}\n\n"
                "Get a free API key at https://msdschain.lagentbot.com (API Keys tab) "
                "and set it via MSDS_API_KEY or gateway authentication."
            )

        # 🔴 **窄回退，不是硬切换**：确定性路径快 30 倍（9.7s → 0.3s），但 curated 表
        # 给不了两样 quick-chat 本来给得了的东西：
        #   ① 非英文答复 —— 表里的 `rationale` / `trade_offs` / `note` 是英文常量；
        #   ② 按 `use_case` 裁剪 —— handler 收下这个参数但从不读它。
        # 硬切换会让 zh 调用方从「中文」退回「英文」、让写了 use_case 的人拿到与上下文
        # 无关的通用建议 —— 那是拿能力换速度，而且**回退是静默的**。
        # ⚠️ 我**量不出**受影响的人有多少：`input_params` 只有 CI-344（08-15）之后的数据，
        # 这个工具在那之后没有调用记录 ⇒ 「没人用 zh」是猜的，不是测的。所以按原则走保守。
        # ⏭ 退出条件：curated 表本地化（[[CI-361]] 的地盘）+ handler 真的读 `use_case`
        # 之后，删掉这个回退、全部走直连。
        # 🔴 CI-1095 review 抓到：这一行判的是**要不要回退到 quick-chat**，所以它必须用
        # **quick-chat 族**的归一化。用 catalog 族会让 `ja`/`de`/`id` 判成「非英文」而走进
        # LLM 那条路，可 `_quick_chat` 进门就把语言夹回 `en` ⇒ 同样的英文答案、慢 30 倍
        # （9.7s vs 0.3s），**而且没有任何东西会红**（本文件的既有用例只覆盖 `zh`）。
        wants_more_than_curated = bool(use_case) or _normalize_lang(lang or LANG) != "en"
        if wants_more_than_curated:
            ctx = f" It is being used as: {use_case}." if use_case else ""
            message = (
                f"Suggest 2-4 safer alternatives to {chemical}.{ctx} "
                "For each alternative, provide: chemical name, CAS number, "
                "why it's safer (specific hazard reduction), any trade-offs "
                "(performance, cost, availability), and whether the original is "
                "restricted under any regulation (REACH SVHC, TSCA, etc.). "
                "Focus on drop-in replacements that serve the same function."
            )
            data = await _quick_chat(message, lang=lang)
            if data.get("_timed_out"):
                success = False
                error_msg = "timeout"
            return _quick_result(data)

        data = await _direct_alternatives(chemical)
        if data.get("error"):
            # 后端 handler 对空入参返回 `{"error": …}` —— 那是失败，不是「没有替代品」。
            success, error_msg = False, str(data["error"])[:120]
            return _text_result(f"Could not look up alternatives: {data['error']}")
        return CallToolResult(
            content=[TextContent(type="text",
                                 text=_format_alternatives(data, chemical))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("get_chemical_alternatives", [chemical],
                        _json.dumps({"chemical": chemical, "use_case": use_case}),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Validate Protocol Chemicals", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_reported
async def validate_protocol_chemicals(
    protocol_text: Annotated[str, Field(
        description="Any text containing chemical names — a lab protocol, a reagent list, or "
                    "code such as an Opentrons Python protocol. Chemical names are extracted "
                    "from it automatically. Maximum ~4000 characters.",
    )],
    lang: QuickLang = None,
) -> str:
    """
    Extract and validate chemical names from a protocol or experiment description.

    Parses free-text or code (e.g., Opentrons Python protocol, lab notebook entry,
    SOP paragraph) to identify all mentioned chemicals, then checks each against
    the MSDS Chain database.

    Returns a structured list with: chemical name as mentioned, canonical name,
    CAS number (if found), and whether full safety data is available.

    Use this as the FIRST step before calling batch_safety_check or
    check_chemical_compatibility — it saves the user from manually listing chemicals.

    A protocol routinely names more chemicals than one turn checks. The result carries an
    `unchecked` list of the ones this turn did NOT look up. When it is non-empty you MUST
    report those chemicals as "not checked" before any conclusion, and you MUST NOT say
    or imply that we lack data or a record for them — nothing was looked up. Tell the
    user to re-send them separately or in smaller batches.

    Args:
        protocol_text: Any text containing chemical names — can be a Python script,
                       a natural language protocol description, or a reagent list.
                       Maximum ~4000 characters.
    """
    error_msg = None
    success = True
    data: dict = {}
    try:
        # CI-578: 截断结果放进新变量 —— 就地覆盖 `protocol_text` 会让 `finally` 里记的
        # `protocol_text_length` 对所有超长输入恒等于 4015，分不出 4.5k 和 200k。
        sent_text = protocol_text
        if len(protocol_text) > 4000:
            sent_text = protocol_text[:4000] + "\n[...truncated]"

        message = (
            "Extract ALL chemical names, reagents, and solvents from the following "
            "text. For each one, look it up in our database and report:\n"
            "- Name as mentioned in the text\n"
            "- Canonical name (if different)\n"
            "- CAS number (if found)\n"
            "- Whether we have safety data for it (yes/no)\n\n"
            "If a name is ambiguous, note the ambiguity.\n\n"
            f"Text to analyze:\n```\n{sent_text}\n```"
        )
        data = await _quick_chat(message, lang=lang)
        if data.get("_timed_out"):
            success = False
            error_msg = "timeout"
        return _quick_result(data)
    finally:
        _log_intent("validate_protocol_chemicals", _chemicals_from_response(data),
                        _json.dumps({"protocol_text_length": len(protocol_text)}),
                    success=success, error_message=error_msg)


def _mixing_order_prompt(chemical_a: str, chemical_b: str, context: str = "") -> str:
    """`check_mixing_order` 发给 `/quick-chat` 的那一串。

    🔴 **这串是量出来的，不是写出来的**（交错采样比过候选措辞；量与口径在私有票
    `docs/pm/tickets/CI-613.md`）。改它之前先去读那张票——**别凭读着顺不顺手改**。

    两个改动点，各自单独测过、缺一不可：
    ① 开头换成内容分类器提示词里逐字白名单的形状（"Is it safe to mix …"）。
       **只去掉反向条款而不改开头，拒答照旧** ⇒ 票里原本设想的那条修法就此证伪。
    ② 反向那条从祈使式索取（"the DANGEROUS order to avoid and what happens if done
       wrong"）改成判断句（"whether … is unsafe"）。内容一条没丢 ⇒ 不需要在 MCP 层
       确定性渲染反向顺序。
    🔴 **别再去改分类器那侧的提示词**：两条修法都已证伪并回滚。
    🔴 采样规模只够说「没看到回归」，不够说「稳」⇒ 拒答兜底照样留着。

    做成独立函数是为了让这串**可被引用而不是被抄写**：golden
    `safety_guard.yaml::safe-order-001` 钉的就是它，跨仓一致性由**私有 workspace 侧**的
    跨仓一致性检查看住（抄写过的探针盯不住措辞漂移）。
    🔴 那个脚本**不在本仓**——此前这里写的相对路径在公开仓里指向不存在的文件，别在这儿找。
    """
    ctx = f" Context: {context}." if context else ""
    return (
        f"Is it safe to mix {chemical_a} and {chemical_b}, and in which order "
        f"should they be added?{ctx} "
        "Specify: (1) the RECOMMENDED addition order and why, "
        "(2) required precautions (cooling, addition rate, stirring, inert atmosphere), "
        "(3) whether adding them in the reverse sequence is unsafe. "
        "If order doesn't matter for this pair, say so explicitly."
    )


_MIXING_ORDER_UNAVAILABLE = (
    "We could not determine a safe addition order for this pair, and no rule-based "
    "compatibility verdict was available either. Do not infer that the order is "
    "unimportant — consult the SDS (Section 7, Handling and Storage) or upload it, "
    "and ask again with `ask_chemical_safety`."
)


async def _mixing_order_grounded_fallback(
    chemical_a: str, chemical_b: str, lang: str | None,
) -> dict:
    """CI-613：quick-chat 被 RAI 判 `rejected` 时的**有依据兜底**。

    形状与 `_quick_chat` 的返回一致（answer / tool_results / documents），
    好让调用处不必分叉。

    🔴 三条约束，动这段之前先读完：
    ① **绝不自由生成**。只调 `/api/v2/compatibility/check`（规则引擎，不过 RAI 闸门，
       `direct_api.py` 里没有 `check_rai`），把它的判定原样渲染。
    ② **只传结构化的两个化学品名，绝不转发 `context`**（用户自由文本）或原始 message。
       ⇒ 这条兜底**不能**被当成绕开审核的通道：它能输出的东西，任何人直接调
       `check_chemical_compatibility` 本来就能拿到，没有新增能力面。
    ③ **不许把「没查到不相容」渲染成绿灯**。相容性回答的是「能不能共存」，
       而本工具问的是「按什么顺序加」——**顺序依据我们一条都没有**（同族 [[CI-611]]）。
       所以文案必须显式说「加料顺序未判定」，不能让读者读成「随便什么顺序都行」。
    """
    try:
        data = await _direct_compat([chemical_a, chemical_b], lang=lang)
    except Exception:
        # 兜底自己也失败：宁可说不知道，也不要编一个顺序出来。
        return {"answer": _MIXING_ORDER_UNAVAILABLE, "tool_results": [], "documents": []}

    lines = [
        "**Addition order: NOT determined.**",
        "",
        "The narrative safety engine declined this phrasing, so the answer below comes "
        "straight from the rule-based compatibility registry. It tells you whether these "
        "two may be combined at all — it does **not** establish a safe addition sequence.",
        "",
    ]
    pairs = data.get("pairs", [])
    # CI-1099：这条路也渲染判定 ⇒ 限制同样要挂在判定行上。CI-869 记过的那条在这里再成立
    # 一次：这条路只在 RAI 拒答时才走，而触发拒答的恰恰是真危险对。
    order_precursor_index = _precursor_index(data)
    for pair in pairs:
        level = (pair.get("level") or "unknown").lower()
        tag = {"compatible": "OK", "caution": "CAUTION",
               "incompatible": "DANGER"}.get(level, level.upper())
        restrict_clause, restrict_lines = _precursor_pair_note(
            order_precursor_index, *_pair_sides(pair))
        lines.append(
            f"- **{pair.get('chem1', chemical_a)}** + **{pair.get('chem2', chemical_b)}**: "
            f"[{tag}] {pair.get('level', 'unknown')}{restrict_clause}\n"
            f"  Reason: {pair.get('reason', 'N/A')}\n"
            f"  Basis (rule): {pair.get('source', 'unknown')}"
            + ("\n" + "\n".join(restrict_lines) if restrict_lines else "")
        )
        if level == "incompatible":
            # 🔴 CI-1078：这里原来写「There is **no safe addition order** … do not combine
            # them in either direction.」——与 `_order_scope_note` 同一个外推，而这条路
            # **同时和自己的表头矛盾**（表头逐字写着 "Addition order: NOT determined."）。
            # 登记表的单位是「能不能共存」，没有顺序维度 ⇒ 说不出「不存在安全的顺序」。
            # 🔴 这条路比另一条更该修，不是更不该：它只在 RAI 拒答时才走，
            # 而触发拒答的恰恰是**真危险对**（CI-613），也就是最可能来自真实工艺的提问。
            lines.append("  ⚠️ INCOMPATIBLE for coexistence — do not combine them outside a "
                         "documented, engineered procedure. **No addition order has been "
                         "determined — in either direction**: the registry has no order "
                         "dimension. An incompatibility verdict is also not a finding that no "
                         "controlled process can exist (sulfuric acid + hydrogen peroxide, "
                         "SPM / piranha, is incompatible by verdict and is still deliberately "
                         "prepared under engineered controls). Take the order and the controls "
                         "from your validated process document or SDS Section 7.")
        else:
            # 🔴 这一句是本函数存在的安全理由，别删：`no_known_incompatibility`
            # 是「登记表里没查到冲突」，不是「顺序无关紧要」。硫酸+水正是这一档，
            # 而它的全部危险都在顺序上。
            lines.append("  ⚠️ No known incompatibility is **not** an addition-order clearance. "
                         "The order for this pair was not evaluated — treat the sequence as "
                         "unverified and consult the SDS before combining.")
    if not pairs:
        lines.append(_MIXING_ORDER_UNAVAILABLE)
    if data.get("unresolved"):
        lines.extend(_unresolved_block(data))
    # 🔴 CI-869 review 抓到：`_direct_compat` 有**两个**调用点，本函数是第二个。
    # 它自己拼 `lines`，所以 `check_chemical_compatibility` 那边挂上的三块披露在这条路上
    # 全部丢失——而这条路正是危险配对最常走的（CI-613：RAI 误伤残留非零，
    # `check_mixing_order` 一旦被拒答就落到这里）。失败形态：用户拿到一份完整的规则引擎
    # 结论，答的是母体记录，零披露 —— 正是 CI-842 要修的那个 Prod 事故本身。
    # ⚠️ `precursor` / `no_hazard_basis` 在这条路上**此前也一直缺**（CI-553 / CI-666 的
    # 同一个漏，只是没人从这个入口走过）⇒ 一并补上，别只补自己这一票的那块。
    disclosures = (_query_form_disclosure_block(data)
                   + _precursor_disclosure_block(data)
                   + _no_hazard_basis_block(data))
    if disclosures:
        # 排在最前：这三块说的都是「下面这些字是关于什么的 / 凭什么」，读到结论之后才看到就晚了。
        lines = disclosures + lines
    return {"answer": "\n".join(lines), "tool_results": [], "documents": data.get("documents", [])}


# 🔴 CI-611：这条工具问的是「按什么顺序加」，而它拿到的判定来自**相容性引擎**——
# 那个引擎的单位是「**能不能共存**」。引擎自己的注释就写着「此处说的是共同存放；
# 混合使用前仍需按各自的反应性单独核实」（`reactivity_matrix.py`）。
#
# ⇒ 硫酸 + 水拿到 `no_known_incompatibility` 是**共存问题的正确答案**，
#   却会被读成**顺序问题的绿灯**——而这一对的全部危险恰恰在顺序上（水入酸 ⇒ 暴沸飞溅）。
#
# 🔴 **不把共存判定改红**：它作为共存答案是对的，改红是新的误伤
# （[[feedback-safety-fix-made-it-worse]]：只验「会不会漏放」而不验「会不会误伤」是半道验证）。
# 这里只做**加法**——显式声明「加料顺序未判定」，并说清依据为什么不存在。
#
# 🔴 **「未判定」是结构性的，不是这次查不到**：全仓零顺序维度（规则引擎里没有
# `order_sensitive`/`addition_order` 任何形态），SDS 第 7 节只以原文存在于
# `msds_sections.content`、从未结构化 ⇒ 规则引擎**永远**给不出顺序判定。
# 散文里那句「酸入水」来自模型，不是来自依据。
_ORDER_NOT_DETERMINED = {
    "verdict": "not_determined",
    "reason": "The rule engine has no addition-order dimension; SDS Section 7 is stored "
              "as raw text only. Any ordering advice in the prose above is model-generated, "
              "not derived from a structured source.",
    "not_a_clearance": "A compatibility verdict answers whether two chemicals may COEXIST. "
                       "It is not a clearance for the ORDER of addition — sulfuric acid + water "
                       "is the canonical case where coexistence is fine and the order is the "
                       "entire hazard.",
}


def _order_scope_note(data: dict) -> str:
    """CI-611：把「共存 ≠ 顺序」这句话放进**用户真正读到的那条通道**（文本）。

    🔴 多数 MCP 客户端只读 text —— 只塞进 structuredContent 等于没修
    （[[fix-never-reaches-the-real-consumer]]）。

    🔴 **CI-1078：不相容分支此前说「there is no safe addition order / 两个方向都别混」，
    那是本函数原本要防的范畴错误的反向，已撤。** 引擎的判定单位是**能不能共存**，
    它没有顺序维度 ⇒ 从「不能共存」推出「不存在安全的加料顺序」**和从「能共存」推出
    「顺序放行」是同一个错**，只是方向相反。而这一侧的代价更贵：半导体 fab 每天**故意**
    配的 SPM（硫酸+双氧水）/ SC-1 / SC-2 三对恰恰都被判 incompatible，一句绝对禁令
    对他们**无法执行** ⇒ 整条答案被丢掉，连该读的危害信息一起丢。
    ⇒ 两个分支现在都**不对顺序下判定**（与 `structuredContent.addition_order.verdict
    = not_determined` 一致，此前文本与结构化层自相矛盾），差别只在**共存那一维说了什么**。

    🔴 **别把这次改动读成「放宽」**：不相容分支仍然明确「不要在没有成文受控工艺的情况下
    混合」，而且是两个分支里唯一带 INCOMPATIBLE 的那句。守卫 + 变异见
    `tests/rendering/test_coexistence_not_order_clearance.py`。
    """
    incompatible = False
    for tr in data.get("tool_results", []):
        res = tr.get("result") if isinstance(tr, dict) else None
        if not isinstance(res, dict):
            continue
        for pair in res.get("matrix", []) or []:
            if str(pair.get("level") or pair.get("verdict") or "").lower() == "incompatible":
                incompatible = True
    if incompatible:
        return ("\n\n---\n⚠️ **This pair is INCOMPATIBLE (coexistence verdict) — do not combine "
                "them outside a documented, engineered procedure.** "
                "That verdict answers whether they may COEXIST; the rule engine has no "
                "addition-order dimension, so **no addition order has been determined here — "
                "in either direction**. An incompatibility verdict is also not a finding that "
                "no controlled process can exist: sulfuric acid + hydrogen peroxide (SPM / "
                "piranha) is incompatible by verdict and is still deliberately prepared under "
                "engineered controls. Any ordering advice *or prohibition* in the prose above "
                "is model-generated, not derived from a structured source — take the order and "
                "the controls from your own validated process document or SDS Section 7.")
    return ("\n\n---\n⚠️ **Addition order: not determined by any structured source.** "
            "A compatibility verdict (e.g. \"no known incompatibility\") answers whether these "
            "may COEXIST — it is *not* a clearance for the order of addition. Sulfuric acid + "
            "water is the canonical case: coexistence is unremarkable, the order is the whole "
            "hazard. Treat any sequence above as unverified guidance and check SDS Section 7.")


@mcp.tool(annotations=ToolAnnotations(title="Check Mixing Order", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_reported
async def check_mixing_order(
    chemical_a: Annotated[str, Field(
        description='First chemical name or CAS number. Order of chemical_a/chemical_b is '
                    'irrelevant — the tool returns which one to add to which.',
    )],
    chemical_b: Annotated[str, Field(
        description="Second chemical name or CAS number.",
    )],
    context: Annotated[str, Field(
        description='Optional context about the procedure, e.g. "diluting for titration" '
                    'or "quenching a reaction".',
    )] = "",
    lang: QuickLang = None,
) -> str:
    """
    Determine the safe order for mixing/adding two chemicals.

    Returns the recommended addition sequence, the dangerous sequence to avoid,
    reasoning (exothermic potential, gas evolution, splashing risk), and any
    required precautions (cooling, dilution rate, inert atmosphere).

    Classic examples: "acid into water, never water into acid";
    "add oxidizer to substrate slowly, not the reverse".

    Use this when reviewing liquid transfer steps in an Opentrons protocol or
    any manual procedure involving sequential addition of reagents.

    Args:
        chemical_a: First chemical name or CAS number
        chemical_b: Second chemical name or CAS number
        context:    Optional context about the procedure, e.g.
                    "diluting for titration" or "quenching a reaction"
    """
    error_msg = None
    success = True
    rai_rejected = False
    try:
        message = _mixing_order_prompt(chemical_a, chemical_b, context)
        data = await _quick_chat(message, lang=lang)
        if data.get("_timed_out"):
            success = False
            error_msg = "timeout"
        # 🔴 CI-613：拒答不是「没有答案」，是**这条通道**没有答案。
        # 改措辞把误判率压下来了，但**压不到 0**（任何写法都还有残余，
        # 详见票）。残余那部分若原样返回，调用方拿到的是一句礼貌的「I can't assist」
        # 加 0 个工具 —— 而这个工具最该服务的正是漂白剂+酸这类真危险对。
        if data.get("intent") == "rejected":
            rai_rejected = True
            data = await _mixing_order_grounded_fallback(chemical_a, chemical_b, lang)
        else:
            # CI-611：正常路径也要说清「共存 ≠ 顺序」。兜底那一支自己已经说了。
            data = {**data, "answer": data.get("answer", "") + _order_scope_note(data)}
        res = _quick_result(data)
        res.structured_content = {**(res.structured_content or {}),
                                  "addition_order": _ORDER_NOT_DETERMINED}
        return res
    finally:
        # 🔴 `rai_rejected` 是这条线唯一能在 Prod 上量残余误判率的地方：兜底之后
        # 用户拿到的是一份正常答案，`success` 仍是 True，**从外面看不出发生过拒答**。
        # 不记这一笔，就只能靠人拿危险对去手动采样才能发现措辞退化（CI-613 全程如此）。
        _log_intent("check_mixing_order", [chemical_a, chemical_b],
                        _json.dumps({"chemical_a": chemical_a, "chemical_b": chemical_b,
                                     "context": context, "rai_rejected_fallback": rai_rejected}),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get Waste Disposal Guidance", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_waste_disposal(chemicals: ChemicalList, suppliers: Suppliers = None,
                             intent: Intent = None) -> str:
    """
    Get waste classification and disposal guidance for chemicals.

    Returns waste category (halogenated/non-halogenated/acidic/alkaline/
    heavy metal/oxidizing/reactive), disposal method, container requirements,
    and incompatible waste streams that must NOT be mixed.

    Based on SDS Section 13 (Disposal Considerations) data.

    Use this after an experiment to determine proper waste segregation and
    disposal procedures for the chemicals used.

    Args:
        chemicals: List of chemical names or CAS numbers, e.g.
                   ["dichloromethane", "acetone", "sulfuric acid"]
    """
    error_msg = None
    success = True
    try:
        data = await _direct_waste(chemicals, suppliers=suppliers)
        lines = ["**Waste Disposal Guidance**\n"]
        for item in data.get("results", []):
            lines.append(f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})")
            lines.extend(_form_disclosure_lines(item))  # CI-572
            # 🔴 CI-360：无依据时 `waste_classification` 是后端的兜底桶
            # （`general_chemical_waste`），把它当分类结论渲染，会和同一段里的
            # `Data source: none` 直接打架——一句说「这是普通化学废物」，一句说
            # 「我们没有依据」。诚实的那句必须赢。
            if item.get("insufficient_hazard_data"):
                lines.extend(_insufficient_lines(item, "Waste classification"))
            else:
                lines.append(f"- **Waste classification:** {item.get('waste_classification', 'N/A')}")
            sds_13 = item.get("sds_section_13")
            if sds_13:
                lines.append(f"- **SDS Section 13 (Disposal Considerations):** {sds_13[:600]}")
            lines.append(f"*Data source: {item.get('data_source', 'unknown')}*\n")
        if data.get("unresolved"):
            lines.extend(_unresolved_block(data))
        if not data.get("results"):
            lines.append("No waste-disposal data found for the given chemicals.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("get_waste_disposal", chemicals,
                        _intent_params({"chemicals": chemicals, **_supplier_params(chemicals, suppliers)}, intent),
                    success=success, error_message=error_msg)
