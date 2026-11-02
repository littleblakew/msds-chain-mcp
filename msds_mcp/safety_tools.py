from __future__ import annotations

import json as _json
import ui_compat_card
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from typing import Annotated, Literal

from msds_mcp.app import _CARD_LINK_ORIGIN, mcp
from msds_mcp.blocks import (_identity_candidate_lines, _no_hazard_basis_block, _pair_sides, _physical_form_disclosures_block, _precursor_disclosure_block, _precursor_index, _precursor_pair_facets, _precursor_pair_note, _query_form_disclosure_block, _rejected_products_block, _unresolved_block)
from msds_mcp.calls import (_expose, _graceful_timeout, _intent_params, _language_note_of, _log_intent, _quick_chat, _reported, _response_kind, _strip_usage, _supplier_params, _with_usage)
from msds_mcp.direct import (_direct_compat, _direct_compliance, _direct_emergency, _direct_exposure, _direct_ppe, _direct_risk, _direct_storage, _direct_transport)
from msds_mcp.disclosures import (_form_disclosure_lines, _insufficient_lines, _storage_item_lines)
from msds_mcp.params import (Chemical, ChemicalList, Intent, Lang, QuickLang, Suppliers, _text_result)
from msds_mcp.regulatory_lists import _unresolved_reason_note, _with_lookup_url
from msds_mcp.render import (_chemicals_from_response, _doc_link_lookup, _format_sds_documents, _inline_sds, _pair_basis_label, _quick_result, _traceability_label)



@mcp.tool(
    annotations=ToolAnnotations(title="Check Chemical Compatibility", read_only_hint=True, destructive_hint=False, open_world_hint=False),
    structured_output=False,
    # CI-1098：链到内联卡片模板。不渲染组件的宿主忽略它，文本答复不变。
    meta=ui_compat_card.tool_meta(),
)
@_graceful_timeout
@_reported
async def check_chemical_compatibility(chemicals: ChemicalList, lang: Lang = None,
                                       suppliers: Suppliers = None,
                                       intent: Intent = None) -> CallToolResult:
    """
    Check pairwise compatibility between a list of chemicals.

    Returns, for every pair, a verdict (compatible / caution / incompatible)
    with the reason and the rule it came from.

    Scope: this answers whether two chemicals may be brought together, and
    nothing more. To find out which cabinet each one belongs in, at what
    temperature, what it must be kept away from, or whether it forms peroxides
    on standing, call get_storage_guidance as well. A set of pairwise verdicts
    does not by itself lay out where the chemicals go.

    Use this before an experiment to verify it is safe to use the listed
    chemicals together in the same lab setting.

    Args:
        chemicals: List of chemical names or CAS numbers, e.g.
                   ["acetone", "methanol", "ethanol"] or ["67-64-1", "67-56-1"]
    """
    error_msg = None
    success = True
    try:
        if len(chemicals) < 2:
            return _text_result("Please provide at least 2 chemicals to check compatibility.")

        data = await _direct_compat(chemicals, lang=lang, suppliers=suppliers)
        lines = [f"**Compatibility Check** ({len(chemicals)} chemicals)\n"]

        if data.get("unresolved"):
            lines.extend(_unresolved_block(data, trailing_newline=True))
        lines.extend(_rejected_products_block(data))
        # 🔴 CI-842 排在 precursor / no_hazard_basis **之前**：那两条都在说「这条答案的
        # 内容」，本条在说「这条答案说的是不是你问的那个东西」——身份先于内容。
        lines.extend(_query_form_disclosure_block(data))
        lines.extend(_physical_form_disclosures_block(data))
        lines.extend(_precursor_disclosure_block(data))
        lines.extend(_no_hazard_basis_block(data))

        # CI-89-inline: per-chemical link lookup so each pair row carries its own
        # SDS links (survives client-model summarization better than a trailing block).
        doc_lut = _doc_link_lookup(data.get("documents", []))
        # CI-1099：限制条件挂到每一对的判定句上，不只躺在上面那个披露块里。
        precursor_index = _precursor_index(data)
        struct_pairs = []
        counts = {"compatible": 0, "caution": 0, "incompatible": 0}
        for pair in data.get("pairs", []):
            level = pair.get("level", "unknown").upper()
            emoji = {"COMPATIBLE": "OK", "CAUTION": "CAUTION", "INCOMPATIBLE": "DANGER"}.get(level, level)
            # 出处前缀由 traceability 决定（_pair_basis_label），缺省按规则层
            traceability = pair.get("traceability", "rule_based")
            basis_label = _pair_basis_label(traceability)
            restrict_clause, restrict_lines = _precursor_pair_note(
                precursor_index, *_pair_sides(pair))
            pair_line = (
                f"- **{pair.get('chem1', '?')}** + **{pair.get('chem2', '?')}**: "
                f"[{emoji}] {pair.get('level', 'unknown')}{restrict_clause}\n"
                f"  Reason: {pair.get('reason', 'N/A')}\n"
                f"  {basis_label}: {pair.get('source', 'unknown')}"
            )
            if restrict_lines:
                pair_line += "\n" + "\n".join(restrict_lines)
            l1 = _inline_sds(doc_lut, pair.get("chem1"))
            l2 = _inline_sds(doc_lut, pair.get("chem2"))
            if l1:
                pair_line += f"\n  **{pair.get('chem1', '?')}**{l1}"
            if l2:
                pair_line += f"\n  **{pair.get('chem2', '?')}**{l2}"
            lines.append(pair_line)
            lvl = (pair.get("level") or "unknown").lower()
            if lvl in counts:
                counts[lvl] += 1
            struct_pair = _expose(
                pair,
                rename={"chem1": "chemical_a", "chem2": "chemical_b"},
                override={"traceability": traceability},   # 我们归一化过，用自己的
            )
            # CI-1099：只读 structuredContent 的客户端同样要在**这一对**上看到限制，
            # 而不是自己去顶层数组里按名字对回来（没有客户端会这么做）。
            pair_facets = _precursor_pair_facets(
                precursor_index, *_pair_sides(pair))
            if pair_facets:
                struct_pair["precursor_restrictions"] = pair_facets
            struct_pairs.append(struct_pair)

        if not data.get("pairs"):
            lines.append("No compatibility pairs to check (need at least 2 resolved chemicals).")

        # CI-89: append SDS document links when backend provides them
        documents = data.get("documents", [])
        if documents:
            lines.append(_format_sds_documents(documents))

        # 顶层也走透传：后端的 `unresolved` / `unresolved_detail`（为什么没解析出来的
        # 机器可读 `code`，文本里早由 `_unresolved_block` 渲染，structuredContent 此前读不到）
        # / `rejected_products` 及**将来新增的任何键**都自动带上；后面那几个是我们自己
        # 算出来或重塑过的，覆盖同名。
        structured = {
            **_expose(data),
            "chemicals": chemicals,
            "pairs": struct_pairs,
            "summary": {"total_pairs": len(struct_pairs), **counts},
            "documents": documents,
        }
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=structured,
            # CI-1098：只给组件、模型看不到的那条通道。内容都是上面文本里已有的。
            meta={ui_compat_card.CARD_META_KEY: ui_compat_card.card_payload(structured, _CARD_LINK_ORIGIN)},
        ), data)
    finally:
        _log_intent("check_chemical_compatibility", chemicals,
                        _intent_params({"chemicals": chemicals, **_supplier_params(chemicals, suppliers)}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get Chemical Risk Warnings", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_chemical_risk_warnings(chemicals: ChemicalList, lang: Lang = None,
                                     suppliers: Suppliers = None,
                                     intent: Intent = None) -> str:
    """
    Get hazard and risk warnings for one or more chemicals.

    Returns GHS hazard classification, signal words (Danger/Warning), H-codes,
    flash point, toxicity, and recommended PPE.

    DRILL-DOWN tool: use this only when the user explicitly wants the raw structured
    hazard fields. For a broad "what are the hazards / is X dangerous / what PPE"
    question, prefer `ask_chemical_safety` — it returns one sourced answer instead of
    forcing you to chain several tools.

    Args:
        chemicals: List of chemical names or CAS numbers, e.g.
                   ["acetone", "67-56-1"]
    """
    error_msg = None
    success = True
    try:
        data = await _direct_risk(chemicals, lang=lang, suppliers=suppliers)
        lines = [f"**Risk Warnings** ({len(chemicals)} chemicals)\n"]

        if data.get("unresolved"):
            lines.extend(_unresolved_block(data, trailing_newline=True))
        lines.extend(_rejected_products_block(data))
        # 🔴 CI-842 排在 precursor / no_hazard_basis **之前**：那两条都在说「这条答案的
        # 内容」，本条在说「这条答案说的是不是你问的那个东西」——身份先于内容。
        lines.extend(_query_form_disclosure_block(data))
        lines.extend(_physical_form_disclosures_block(data))
        lines.extend(_precursor_disclosure_block(data))
        lines.extend(_no_hazard_basis_block(data))

        # CI-89: build a set of chemicals that have SDS-backed documents
        documents = data.get("documents", [])
        sds_backed_chemicals = {
            (doc.get("chemical_name") or doc.get("chemical") or "").lower()
            for doc in documents
        }
        doc_lut = _doc_link_lookup(documents)  # CI-89-inline

        for w in data.get("warnings", []):
            level = w.get("level", "unknown").upper()
            # CI-89: label each warning by its traceability
            traceability = w.get("traceability")
            trace_label = _traceability_label(
                traceability, w.get("chemical"), sds_backed_chemicals)
            # CI-89-inline: SDS link on the warning line itself
            inline = _inline_sds(doc_lut, w.get("chemical"), w.get("cas"))
            lines.append(
                f"### {w.get('chemical', 'Unknown')} — {level} RISK {trace_label}{inline}\n"
                f"- **Description:** {w.get('description', 'N/A')}\n"
                f"- **Mitigation:** {w.get('mitigation', 'N/A')}"
            )
            if w.get("reference"):
                lines.append(f"- **Reference:** {w['reference']}")

        if not data.get("warnings") and not data.get("no_hazard_basis"):
            # 🔴 CI-666：只有在**确实没有可说的**时候才说这句。有 `no_hazard_basis`
            # 时上面已经逐条说清了「匹配到的记录没有危害数据」，再补一句
            # "No risk warnings found" 会把它重新压回「查过了、没有」。
            lines.append("No risk warnings found for the given chemicals.")

        # CI-89: append SDS document links
        if documents:
            lines.append(_format_sds_documents(documents))

        structured = {
            **_expose(data),
            "chemicals": chemicals,
            # 逐条也透传：此前手抄字段，把 `additional_hazards`（带 supplier + revision_date）丢了
            "warnings": [_expose(w) for w in data.get("warnings", [])],
            "documents": documents,
        }
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=structured,
        ), data)
    finally:
        _log_intent("get_chemical_risk_warnings", chemicals,
                        _intent_params({"chemicals": chemicals, **_supplier_params(chemicals, suppliers)}, intent),
                    success=success, error_message=error_msg)


def _format_region_results(region_results: list[dict]) -> list[str]:
    """把每个法域的结果渲染成人读的行。

    🔴 CI-493：**状态词单独一个是读不出意思的**。`not_restricted` 不说清「查了哪几份」
    会被读成放行；`unverified` 不说清会被读成「没问题」；而「不在现有物质名录上」
    与「不在限制清单上」方向相反 —— 前者是风险，后者是好消息，混在一起念就是那条
    「错误绿灯」。后端把这些话放在 `details` / `inventory.note` 里，这一层必须转述：
    修在引擎里而渲染层不接，等于没到达真正的消费者（同 CI-488 那条教训）。
    """
    lines: list[str] = []
    for rr in region_results:
        st = rr.get("status", "unknown")
        lines.append(f"- **{rr.get('region', '?')}:** {st}")
        for flag in rr.get("flags", []):
            lines.append(f"  - {flag}")
        # 🔴 白名单在这里是错的形状：漏掉的恰恰是**最需要解释的那些**。
        # `restricted` 的 details 装着限制正文（"shall not be used in toys…"），
        # `unsupported` 的 details 说明为什么这个法域答不了 —— 白名单把两者都吞了。
        # 有 details 就转述，不做取舍。
        if rr.get("details"):
            lines.append(f"  - _{rr['details']}_")
        inv = rr.get("inventory") or {}
        if inv.get("on_inventory") is True:
            checked = ", ".join(inv.get("lists_checked") or []) or "n/a"
            lines.append(f"  - 📋 Inventory: listed ({checked}) — registration status, not a restriction")
        elif inv.get("on_inventory") is False:
            lines.append(f"  - ⚠️ Inventory: **NOT listed** — {inv.get('note', '')}")
        elif inv.get("lookup"):
            # CI-1235: no inventory copy, only the official query page. `on_inventory`
            # stays None (nothing was checked), so this line must never read as a
            # finding — the backend `note` already says "not determined" and carries
            # the URL; the URL is appended only if a future note stops carrying it.
            lines.append(f"  - 🔗 Inventory: not determined — {_with_lookup_url(inv.get('note'), inv['lookup'])}")
    return lines


@mcp.tool(annotations=ToolAnnotations(title="Check Regulatory Compliance", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def check_regulatory_compliance(
    chemicals: ChemicalList,
    regions: Annotated[list[str] | None, Field(
        description='Region codes to check. Valid codes: "EU", "US", "CN", "JP", "KR", '
                    '"CA", "AU", "SG", "TW". Omit to check EU + US (the default pair) — the '
                    'response says explicitly which regions were used.',
    )] = None, lang: Lang = None, intent: Intent = None,
) -> str:
    """
    Check multi-region regulatory status for chemicals. Answers TWO separate questions
    per region — do not collapse them (CI-493):

    1. `status` — is it on a RESTRICTION list (EU SVHC / REACH Annex XVII, US Prop 65,
       China Catalogue of Hazardous Chemicals, JP CSCL, SG EPMA, Korea Restricted /
       Prohibited Substances, Taiwan MOENV Listed Toxic and Concerned Chemical Substances,
       Taiwan OSHA Priority Management and Controlled Chemicals)?
         `restricted`     on at least one restriction list. A REACH Annex XVII flag names
                          the entry numbers, e.g. "REACH Annex XVII (Entry 5; Entry 72)";
                          each entry restricts specific uses, concentrations or articles,
                          so pass the entry on rather than calling the substance banned.
         `detected`       only indirect evidence: a CMR hazard code, an occupational
                          exposure limit, an SDS Section 15 mention, or (EU) a hit on
                          CLP Annex VI, Seveso III or the Water Framework Directive
                          priority list. Those hits are still named in `flags`, each
                          marked "not a restriction" — relay that marking, do not
                          upgrade it. (EU) A diisocyanate matched by name to REACH
                          Annex XVII Entry 74 is also `detected`: relay its training
                          condition, do not upgrade it to `restricted`. (US) A metal
                          compound matched by name to a Prop 65 category entry such
                          as "Lead and lead compounds" is also `detected`: relay that
                          it is a match by name, do not upgrade it to `restricted`.
                          🔴 NOT a clearance and NOT a violation.
         `not_restricted` we DID check that region's restriction lists and it is not on
                          them. `details` names which lists were checked. Lists we do not
                          hold were not checked, so this is not a clearance either.
         `unverified`     no check was performed — we hold no restriction list for that
                          region (CA, AU), or the source could not be read.
                          KR has one restriction list (Restricted / Prohibited
                          Substances); hazardous designated substances such as
                          benzene are NOT on it, so KR `not_restricted` is not a
                          clearance.
                          TW has four restriction lists (MOENV Listed Toxic and
                          Concerned Chemical Substances, OSHA Priority Management and
                          Controlled Chemicals), so TW `not_restricted` means "not on
                          those four lists", nothing more. The Taiwan OSHA Permissible
                          Exposure Standards are not a restriction list.
                          🔴 Never report this as "not regulated".
    2. `inventory` — is it on that region's EXISTING-SUBSTANCE inventory (TSCA / IECSC /
       KECL / DSL / AIIC / REACH registered)? Here the polarity is REVERSED:
         `on_inventory: true`  registered there — about registration, not restriction
         `on_inventory: false` 🔴 the direction that needs attention: a substance absent
                               from the inventory typically needs new-substance
                               notification before import
         `on_inventory: null`  we hold no inventory for that region. For TW (TCSI) a
                               `lookup` link to the official Taiwan OSHA CSNN query page
                               is returned instead: pass the link on and say we do NOT
                               determine existing / new substance status there

    Use this when preparing export documentation, compliance audits, or when working with
    chemicals that may be restricted in certain jurisdictions.
    🔴 No result from this tool is ever, on its own, an import/export clearance. In
    particular an exposure limit (OSHA PEL) answers "how much exposure is allowed", not
    "is this substance permitted" — report it as evidence, never as compliance.

    Args:
        chemicals: List of chemical names or CAS numbers
        regions:   Optional list of region codes to check, e.g. ["EU", "US", "CN"]
                   Defaults to EU + US if not specified.
                   Valid codes: EU, US, CN, JP, KR, CA, AU, TW
    """
    error_msg = None
    success = True
    try:
        effective_regions = regions or ["EU", "US"]
        lines = ["**Regulatory Compliance**\n"]
        # CI-61: a stateless tool can't ask which jurisdiction, so when the caller
        # names none we default to EU+US but DISCLOSE it — never let a silent default
        # read as "checked everywhere". (The conversational agent path asks instead.)
        if not regions:
            lines.append(
                "> ℹ️ No regions specified — checked **EU, US** by default. "
                "Pass `regions` to check others (available: EU, US, CN, JP, KR, CA, AU, TW).\n"
            )
        results = []
        _usage_cost = 0.0
        _usage_bal = None
        _usage_reason = ""
        _saw_usage = False
        for chemical in chemicals:
            data = await _direct_compliance(chemical, effective_regions, lang)
            _u = data.pop("_usage", None)  # strip internal key from stored per-chemical result
            if _u:
                _saw_usage = True
                _usage_cost += _u.get("cost", 0) or 0
                _usage_bal = _u.get("balance")
                _usage_reason = _u.get("reason", "")
            results.append(data)
            if data.get("unresolved"):
                # 🔴 CI-714 第三处（扩大作用域时扫出来的，票面只点名了另外两个端点）：
                # `/api/v2/compliance` 的未解析分支同样只回一个布尔
                # （`direct_compliance`: `{chemical, cas: None, region_results: [],
                # summary_level: "unknown", unresolved: True}`），没有任何原因。
                # 把它渲染成 "Not found in database" 是同一句我们无权说的话。
                lines.append(f"### {chemical}\n- **Status:** {_unresolved_reason_note(data)}")
                lines.extend(_identity_candidate_lines(data.get("unresolved_detail")))
                lines.append("")
                continue
            lines.append(f"### {data.get('chemical', chemical)} (CAS: {data.get('cas', 'N/A')})")
            lines.append(f"- **Overall compliance level:** {data.get('summary_level', 'unknown')}")
            lines.extend(_format_region_results(data.get("region_results", [])))
            lines.append("")
        # 🔴 CI-904：判据是「**这轮有没有被计量**」，不是「余额拿到没有」。
        # 原来写 `if _usage_bal is not None`——余额 header 缺失时（现在是 `None`）
        # 会把**整条用量行吞掉**，而那次调用其实是扣了钱的。
        _usage = ({"cost": _usage_cost, "balance": _usage_bal, "reason": _usage_reason}
                  if _saw_usage else None)
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content={
                "chemicals": chemicals,
                "regions": effective_regions,
                "regions_defaulted": not regions,
                "results": results,
                # CI-1133：本工具的 structuredContent 是**手拼白名单**（CI-342 记过：
                # 后端新增的键不会自动带上，也不会报错）⇒ 顶层那句 `language_note`
                # 要显式从 `results` 里推出来，`_relay_language_note` 才够得着文本面。
                **_language_note_of(results),
            },
        ), {"_usage": _usage})
    finally:
        _log_intent("check_regulatory_compliance", chemicals,
                        _intent_params({"chemicals": chemicals, "regions": regions}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Ask Chemical Safety Question", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_reported
async def ask_chemical_safety(
    question: Annotated[str, Field(
    description='A chemical safety question in natural language, e.g. "What are the main '
                'hazards and PPE for TMAH?", "How should I store acetone and methanol in '
                'the same cabinet?", "A worker got hydrofluoric acid on their skin — first aid?"',
    )],
    lang: QuickLang = None,
) -> str:
    """
    PREFERRED first tool for any general chemical-safety question — hazards, PPE,
    first aid, spill/exposure response, storage, disposal, "is X safe", "what do I
    need to handle Y", GHS interpretation, MSDS lookup.

    Returns ONE answer grounded in a specific supplier SDS, with the source
    (supplier + revision date) cited and any general knowledge clearly separated
    from what the SDS actually says.

    Use this FIRST for broad questions instead of chaining search_chemical_database
    + get_chemical_risk_warnings + get_ppe_recommendation — those are slower, produce
    a fragmented answer, and lose the single sourced citation. Reach for the granular
    tools only when the user explicitly wants just that one structured field.

    When presenting the answer, cite the returned source and do not add hazard,
    medical, or regulatory claims that are not in the tool output.

    A question may name more chemicals than one turn checks. When the result carries a
    non-empty `unchecked` list, you MUST say so before any conclusion and MUST NOT state
    or imply whether data exists for those — this turn did not look them up. Tell the
    user to ask about them separately.

    Args:
        question: Any chemical safety question, e.g.
                  "What are the main hazards and PPE for TMAH?"
                  "How should I store acetone and methanol in the same cabinet?"
                  "A worker got hydrofluoric acid on their skin — first aid?"
    """
    error_msg = None
    success = True
    data: dict = {}
    try:
        data = await _quick_chat(question, lang=lang)
        if data.get("_timed_out"):
            success = False
            error_msg = "timeout"
        return _quick_result(data)
    finally:
        _log_intent("ask_chemical_safety", _chemicals_from_response(data),
                        _json.dumps({"question": question}),
                    success=success, error_message=error_msg,
                    response_kind=_response_kind(data))


@mcp.tool(annotations=ToolAnnotations(title="Get PPE Recommendation", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_ppe_recommendation(chemicals: ChemicalList, lang: Lang = None,
                                 suppliers: Suppliers = None,
                                 intent: Intent = None) -> str:
    """
    Get PPE (Personal Protective Equipment) recommendations for chemicals.

    Returns specific glove types, eye protection, respiratory protection, and
    body protection requirements based on MSDS Section 8 data and GHS hazard codes.

    DRILL-DOWN tool: use this only when the user explicitly wants a standalone PPE
    list. A broad "what are the hazards and what PPE" question is answered in one
    sourced call by `ask_chemical_safety` — prefer that over chaining tools.

    Args:
        chemicals: List of chemical names or CAS numbers, e.g.
                   ["acetone", "hydrochloric acid"] or ["67-64-1"]
    """
    error_msg = None
    success = True
    try:
        data = await _direct_ppe(chemicals, lang, suppliers=suppliers)
        lines = ["**PPE Recommendations**\n"]

        # CI-89: build set of SDS-backed chemicals from documents list
        documents = data.get("documents", [])
        sds_backed_chemicals = {
            (doc.get("chemical_name") or doc.get("chemical") or "").lower()
            for doc in documents
        }
        doc_lut = _doc_link_lookup(documents)  # CI-89-inline

        for item in data.get("results", []):
            # CI-89: label each result by its traceability
            traceability = item.get("traceability")
            trace_label = _traceability_label(
                traceability, item.get("chemical_name"), sds_backed_chemicals)
            header = f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})"
            if trace_label:
                header += f"  {trace_label}"
            header += _inline_sds(doc_lut, item.get("chemical_name"), item.get("cas"))  # CI-89-inline
            lines.append(header)
            lines.extend(_form_disclosure_lines(item))  # CI-572
            lines.append(f"- Signal word: **{item.get('signal_word') or 'N/A'}**")
            # CI-243: the backend now returns null when the SDS parsed no hazards at
            # all. `.get(k, 'N/A')` does NOT catch that — the key exists, its value is
            # None — so this rendered the literal word "None" as if it were a level.
            # Absence of a measurement must read as absence, and must say so loudly
            # enough that a model relaying this does not fill the gap itself.
            if item.get("insufficient_hazard_data") or item.get("minimum_ppe_level") is None:
                lines.append(
                    "- Minimum PPE level: **CANNOT BE DETERMINED** — this SDS record "
                    "contains no hazard data (no H-codes, no signal word). This is NOT "
                    "a low-hazard finding. Do not infer protective equipment from "
                    "general knowledge; upload this substance's SDS or try another "
                    "supplier's record."
                )
            else:
                lines.append(f"- Minimum PPE level: **{item['minimum_ppe_level']}**")
            ppe = item.get("ppe", {})
            for category, recs in ppe.items():
                if isinstance(recs, list):
                    lines.append(f"- **{category.title()}:** {', '.join(str(r) for r in recs)}")
                else:
                    lines.append(f"- **{category.title()}:** {recs}")
            lines.append("")
        if data.get("unresolved"):
            lines.extend(_unresolved_block(data))
        if not data.get("results"):
            lines.append("No PPE data found for the given chemicals.")

        # CI-89: append SDS document links
        if documents:
            lines.append(_format_sds_documents(documents))

        # Build structuredContent: strip internal _usage key but include documents
        sc = _strip_usage(data)
        if not isinstance(sc, dict):
            sc = {}
        sc["documents"] = documents
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=sc,
        )
    finally:
        _log_intent("get_ppe_recommendation", chemicals,
                        _intent_params({"chemicals": chemicals, **_supplier_params(chemicals, suppliers)}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get Storage Guidance", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_storage_guidance(chemicals: ChemicalList, lang: Lang = None,
                               suppliers: Suppliers = None,
                               intent: Intent = None) -> str:
    """
    Get storage and isolation guidance for chemicals.

    Returns storage class (flammable/oxidizer/corrosive/toxic/general),
    recommended cabinet type and color code, temperature requirements,
    incompatible materials for isolation, and specific storage instructions
    derived from SDS Section 7.

    Args:
        chemicals: List of chemical names or CAS numbers, e.g.
                   ["acetone", "sulfuric acid"] or ["67-64-1"]
    """
    error_msg = None
    success = True
    try:
        data = await _direct_storage(chemicals, lang=lang, suppliers=suppliers)
        lines = ["**Storage Guidance**\n"]
        for item in data.get("results", []):
            lines.extend(_storage_item_lines(item))
        if data.get("unresolved"):
            lines.extend(_unresolved_block(data))
        if not data.get("results"):
            lines.append("No storage data found for the given chemicals.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("get_storage_guidance", chemicals,
                        _intent_params({"chemicals": chemicals, **_supplier_params(chemicals, suppliers)}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get Emergency Response", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_emergency_response(
    chemical: Chemical,
    scenario: Annotated[Literal["spill", "fire", "exposure"], Field(
        description='Type of emergency: "spill" (leak/release), "fire", or "exposure" '
                    '(skin/eye/inhalation first aid). These three are the only accepted '
                    'values — the backend rejects anything else, so map the incident to '
                    'the closest one (e.g. skin contact / splash / inhalation → "exposure"). '
                    '🔴 PERSON FIRST: if the material reached a person, use "exposure" EVEN IF '
                    'the incident is also a spill and the user said "spilled" — e.g. "I spilled '
                    'it on my hand" is "exposure", not "spill". Only "exposure" returns the '
                    'substance-specific first-aid protocol (e.g. the calcium gluconate protocol '
                    'for hydrofluoric acid); picking "spill" for a contact incident silently '
                    'returns cleanup guidance instead of the antidote. '
                    '🔴 CI-1020 — A BURN ON A PERSON IS "exposure", NOT "fire": '
                    '"I got burned by HF", "it is burning my hand", "chemical burn on my arm" '
                    'are all "exposure". Use "fire" only when something is actually combusting.',
    )] = "spill",
    lang: Lang = None, intent: Intent = None,
) -> str:
    """
    Get emergency response guidance for a chemical incident.

    Returns immediate actions, SDS-specific instructions from Section 4/5/6,
    and H-code-based guidance for three scenario types.

    Args:
        chemical: Chemical name or CAS number, e.g. "hydrochloric acid"
        scenario: Type of emergency — "spill" (leak/release), "fire", or
                  "exposure" (skin/eye/inhalation first aid). Defaults to "spill".
                  🔴 Person first: material that reached a person is "exposure" even
                  when the user says "spilled" — only "exposure" returns the
                  substance-specific antidote protocol (CI-579).
    """
    error_msg = None
    success = True
    try:
        data = await _direct_emergency(chemical, scenario, lang=lang)
        if data.get("error"):
            return _text_result(f"Emergency response error: {data['error']}")
        chem_display = data.get("chemical", chemical)
        cas = data.get("cas", "N/A")
        lines = [f"**Emergency Response: {scenario.title()} — {chem_display} ({cas})**\n"]
        # 🔴 CI-572：急救是形态差异最致命的场景（无水 HF vs 氢氟酸水溶液），
        # 这一行必须在任何处置动作**之前**出现。
        lines.extend(_form_disclosure_lines(data))
        if data.get("signal_word"):
            lines.append(f"Signal word: **{data['signal_word']}**\n")
        # 🔴 CI-568：披露必须渲进**文本**。后端算出 `provenance_note` 已久，但两个面的
        # 返回字典都没抄它（后端侧已修），而即便抄了，只读 TextContent 的客户端仍然
        # 看不到——这条渲染函数是逐键取值的。CI-553 刚在管制前体披露上栽过同一形状。
        # 位置在最上方：它是「下面这些字是谁说的」，读到步骤之后才看到披露就晚了。
        note = data.get("provenance_note")
        if note:
            lines.append(f"*Provenance: {note}*\n")
        # 🔴 CI-567：物质级规程条目单独成段并排在最前。失败形状：
        # 它们混在 immediate_actions 里、没有任何标记，而通用 [Hxxx] 行数量多篇幅大 ⇒
        # 模型拿通用行的数字把物质级那条改写掉（HF 的「立刻涂钙剂」被降级成
        # 「告知医护人员以便他们提供」＝延迟解毒）。标题里写死「不要用通用指引替换」。
        # 🔴 按 `[protocol]` **前缀**认，不靠后端多返回一个字段：第一版是单独字段，
        # 那等于把同一段安全关键原文复制第二份进载荷，实测把载荷推过裁剪预算、
        # 反而把「立即冲洗 15 分钟」那句挤掉了。标记随文本走就没有这个问题。
        priority = [a for a in (data.get("immediate_actions") or [])
                    if isinstance(a, str) and a.startswith("[protocol]")]
        if priority:
            lines.append("**Substance-specific protocol — follow these first, verbatim** "
                         "(generic hazard-code guidance below does NOT override these; "
                         "do not substitute its durations or defer these actions to clinicians):")
            lines.extend(f"  - {a}" for a in priority)
            if data.get("protocol_citation"):
                lines.append(f"  *Source: {data['protocol_citation']}*")
            lines.append("")
        immediate = data.get("immediate_actions", [])
        if immediate:
            # priority 那几条已经单独渲染过，别再重复一遍（CI-553 折叠重复披露的同一教训）。
            rest = [a for a in immediate if a not in priority]
            if rest:
                lines.append("**Immediate Actions:**")
                lines.extend(f"  - {a}" for a in rest)
                lines.append("")
        sds = data.get("sds_instructions", [])
        if sds:
            lines.append("**SDS-Specific Instructions:**")
            lines.extend(f"  - {i}" for i in sds)
            lines.append("")
        hcode = data.get("hcode_actions", [])
        if hcode:
            lines.append("**Hazard Code Guidance:**")
            lines.extend(f"  - {a}" for a in hcode)
            lines.append("")
        # CI-370: GHS 官方为该危害类别指派的处置语（P 句），每条自带 P 码。
        # 🔴 必须渲进**文本**：这一层把 exposure 场景的可答率抬高了一大截，
        # 但多数 MCP 客户端只把 text 喂给模型（见 CI-360 的同一教训）——只放在
        # structuredContent 里，等于后端声称有依据而用户看到零条指引。
        # 🔴 标题写明出处：这是**这一类危害**的标准处置语，不是这份 SDS 的正文。
        # 两者混在一段渲染会让模型把通用语当成「这份 SDS 这么说」，侵蚀「可追溯」这个主张。
        precaution = data.get("precaution_actions", [])
        if precaution:
            lines.append("**GHS Standard Precautions** "
                         "(official GHS statements for this hazard class — "
                         "not text from this specific SDS):")
            lines.extend(f"  - {a}" for a in precaution)
            lines.append("")
        # CI-360: 「判不了」要说出来，别让用户从一行 `Data source: none` 里猜。
        # 🔴 只在**这个场景**判不了；上面 immediate_actions 里与化学品无关的通用动作
        # （呼叫急救、参照 SDS 第 6 节）照常渲染 —— 该说的不说也是失真。
        if data.get("insufficient_hazard_data"):
            lines.extend(_insufficient_lines(data, "Scenario-specific response"))
            lines.append("")
        lines.append(f"*Data source: {data.get('data_source', 'unknown')}*")
        if data.get("unresolved"):
            # 🔴 CI-714：此前这里写死「Chemical not found in database」——一句**我们无权说的话**。
            # `unresolved: true` 只说明身份没解析出来，成因可以是「我们压根没搜」（畸形 CAS）、
            # 「有一级没跑成」、「名字与 CAS 互相矛盾所以拒答」，其中只有一种是「库里没有」。
            # 实测：`71-43`（我们**没有**搜索过）与一个乱码串拿到的这行
            # **逐字相同** ⇒ 模型据此对付费用户断言我们没有这份数据。同族 [[CI-770]]/[[CI-413]]。
            # 措辞照抄后端 `unresolved_detail.reason_en` 已经在用的那半句（"This is not a
            # statement that…"），不新造披露口径。
            # ✅ CI-714 后端那半已上 Prod：载荷现在可能带 `unresolved_detail`
            # （裸 dict，不是列表），于是这里能说出**逐案**的原因而不只是通用那句。
            # 缺席时仍回退到通用句 —— 见 `_unresolved_reason_note` 的 docstring。
            lines.append(f"\n**Note:** {_unresolved_reason_note(data, lang)} "
                         "Showing general guidance only.")
            lines.extend(_identity_candidate_lines(data.get("unresolved_detail")))
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("get_emergency_response", [chemical],
                        _intent_params({"chemical": chemical, "scenario": scenario}, intent),
                    success=success, error_message=error_msg)


# CI-1076: when no curated or regulatory value exists, the backend answers with the
# supplier's own SDS text instead of an empty list. These are QUOTES, not values we
# extracted: the renderer must say whose words they are and must not turn a number in
# the quote into one of our limits (data measured §8 parsing as unreliable, which is
# why the backend ships text, not fields).
def _supplier_quote_lines(ev: dict | None) -> list[str]:
    """Render a backend `section_excerpt` / `evidence` dict as an attributed quote."""
    if not ev or not ev.get("quote"):
        return []
    who = ev.get("supplier") or "unknown supplier"
    rev = f", revision {ev['revision_date']}" if ev.get("revision_date") else ""
    lines = [f"> **Supplier SDS Section {ev.get('section', '?')}** ({who}{rev}), quoted as written:"]
    lines.extend(f"> {ln}" if ln.strip() else ">" for ln in str(ev["quote"]).splitlines())
    if ev.get("excerpt_truncated"):
        lines.append("⚠️ The quote was cut short; read the rest in the original SDS.")
    if ev.get("section_possibly_truncated"):
        lines.append("⚠️ Our stored copy of this section may be missing its end; check the original SDS.")
    return lines


def _exposure_item_lines(item: dict) -> list[str]:
    """Body of one `get_exposure_limits` result (CI-1076 statuses included)."""
    lines: list[str] = []
    limits = item.get("limits") or []
    for lim in limits:
        source = lim.get("source") or lim.get("authority") or "?"
        ltype = lim.get("type", "?")
        value = lim.get("value", "—")
        unit = lim.get("unit", "")
        # CI-578: 别叫 `region` —— 它会盖掉调用方传的过滤条件，而 `finally`
        # 里的 `_log_intent` 在循环之后跑，记进日志的就成了最后一条限值的
        # region。守卫见 tests/call_logging/test_logged_params_not_reassigned.py。
        lim_region = lim.get("region", "")
        region_suffix = f" ({lim_region})" if lim_region else ""
        # 同一 CAS 在法规值表里可有多行（粉尘总量 / 可吸入、各附表行），entry_name 是唯一区分。
        entry = f" [{lim['entry_name']}]" if lim.get("entry_name") else ""
        extra = "; ".join(str(lim[k]) for k in ("notation", "source_version", "remarks") if lim.get(k))
        if lim.get("value_source") == "regulatory_table":
            # 混合答案（curated_literal+regulatory_table）里，模型要能分清哪行出自法规值表。
            extra = f"regulatory table; {extra}" if extra else "regulatory table"
        extra = f" ({extra})" if extra else ""
        lines.append(f"- **{source}**{region_suffix}{entry}: {ltype} = {value} {unit}".rstrip() + extra)
    if limits:
        if item.get("source_note"):
            lines.append(f"*{item['source_note']}*")
        return lines
    status = item.get("oel_status")
    excerpt = item.get("section_excerpt")
    if excerpt and excerpt.get("quote"):
        if status == "supplier_states_no_oel":
            lines.append("- No curated or regulatory limit on file. **The supplier's SDS states "
                         "that no exposure limit applies**: that is the supplier's statement, "
                         "quoted below, not a check made by us.")
        else:
            lines.append(f"- No curated or regulatory limit on file. Section "
                         f"{excerpt.get('section') or '8.1'} of the supplier's SDS is quoted "
                         "below. **No values were extracted from it**: any figure "
                         "in the quote is the supplier's, and which component and jurisdiction "
                         "it applies to is as stated in the original text.")
        for key in ("note", "region_note"):
            if item.get(key):
                lines.append(f"*{item[key]}*")
        lines.extend(_supplier_quote_lines(excerpt))
        return lines
    lines.append("- No OEL data found for this chemical.")
    return lines


_AMBIGUITY_REASONS = {
    "multiple_un_numbers": "the section names more than one UN number",
    "regulated_and_not_regulated_signals": "the section both classifies it and says it is not regulated",
    "not_regulated_for_some_modes_only": "it is stated as not regulated for some transport modes only",
    "section_possibly_truncated": "our stored copy of the section may be cut off",
}


def _transport_item_lines(item: dict) -> list[str]:
    """Body of one `get_transport_classification` result (CI-1076 statuses included).

    🔴 `ambiguous` lists EVERY candidate and picks none: choosing one would state a UN
    number the supplier's own text does not support.
    """
    status = item.get("transport_status")
    ev_lines = _supplier_quote_lines(item.get("evidence"))
    note = [f"*{item['note']}*"] if item.get("note") else []
    if status == "ambiguous":
        cands = item.get("un_candidates") or []
        lines = ["- **UN Number: not determined.** The supplier's SDS Section 14 does not "
                 "support a single UN number, so none was chosen."]
        if cands:
            lines.append(f"- **Candidates in the text:** {', '.join(cands)}")
        reason = item.get("ambiguity_reason")
        if reason:
            lines.append(f"- Reason: {_AMBIGUITY_REASONS.get(reason, reason)}")
        return lines + note + ev_lines
    if status == "supplier_states_not_regulated":
        return (["- **The supplier's SDS states this is not regulated as dangerous goods.** "
                 "That is the supplier's statement, not a classification made by us."]
                + note + ev_lines)
    if status == "none":
        return ["- No transport classification on file: no curated entry, and the supplier's "
                "SDS Section 14 supports no statement."]

    if status is None and not item.get("un_number") and not item.get("transport_modes"):
        # 旧后端（无 transport_status）查不到时整排字段都是 None：别印四行「not stated」装作有分类。
        return ["- No transport classification on file for this chemical."]

    def field(key: str) -> str:
        v = item.get(key)
        return "not stated" if v in (None, "") else str(v)

    lines = ["- Read from the supplier's SDS Section 14 (quoted below), not a "
             "classification made by us."] if status == "un_number_found" else []
    lines += [
        f"- **UN Number:** {field('un_number')}",
        f"- **Proper Shipping Name:** {field('proper_shipping_name')}",
        f"- **Hazard Class:** {field('hazard_class')}",
        f"- **Packing Group:** {field('packing_group')}",
    ]
    modes = item.get("transport_modes") or {}
    if modes:
        lines.append("- **Transport Modes:**")
        lines.extend(f"  - {mode.upper()}: {details}" for mode, details in modes.items())
    if item.get("source_note"):
        lines.append(f"*{item['source_note']}*")
    return lines + note + ev_lines


@mcp.tool(annotations=ToolAnnotations(title="Get Exposure Limits", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_exposure_limits(
    chemicals: ChemicalList,
    region: Annotated[str | None, Field(
        description='Optional filter for which standards to return: "US" (OSHA PEL), '
                    '"EU" (IOELV), "JP", "CN" (GBZ 2.1), or "INT" (ACGIH TLV). '
                    'Omit to return every standard on file.',
    )] = None, intent: Intent = None,
) -> str:
    """Get occupational exposure limits (OEL/TLV/PEL/MAC) for chemicals.

    Returns TWA, STEL, and Ceiling values from multiple standards:
    - OSHA PEL (US)
    - ACGIH TLV (International)
    - EU SCOEL IOELV
    - Japan 産衛研
    - China GBZ

    When no curated or regulatory value is on file, the answer may instead QUOTE
    Section 8.1 of the supplier's SDS (supplier and revision named). Those quotes are
    the supplier's words, not values we extracted: present them as quotes and do not
    restate a number from them as a limit we hold.

    Args:
        chemicals: List of chemical names or CAS numbers
        region: Optional filter — "US", "EU", "JP", "CN", or "INT"
    """
    error_msg = None
    success = True
    try:
        data = await _direct_exposure(chemicals, region)
        lines = ["**Occupational Exposure Limits**\n"]
        for item in data.get("results", []):
            lines.append(f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})")
            lines.extend(_form_disclosure_lines(item))  # CI-572
            if item.get("region_filter"):
                lines.append(f"Region filter: **{item['region_filter']}**")
            lines.extend(_exposure_item_lines(item))
            lines.append(f"*Data source: {item.get('data_source', 'unknown')}*\n")
        if data.get("unresolved"):
            lines.extend(_unresolved_block(data))
        if not data.get("results"):
            lines.append("No exposure-limit data found for the given chemicals.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("get_exposure_limits", chemicals,
                        _intent_params({"chemicals": chemicals, "region": region}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get Transport Classification", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_transport_classification(chemicals: ChemicalList,
                                       suppliers: Suppliers = None,
                                       intent: Intent = None) -> str:
    """Get UN transport classification for chemicals (dangerous goods shipping).
    Returns UN number, proper shipping name, hazard class, packing group,
    and transport mode details (ADR road, IATA air, IMDG sea).
    Without a curated entry, the answer may come from the supplier's SDS Section 14,
    quoted with supplier and revision. If that text names several UN numbers the
    answer is "not determined" and lists every candidate: do not pick one.
    Args:
        chemicals: List of chemical names or CAS numbers
    """
    error_msg = None
    success = True
    try:
        data = await _direct_transport(chemicals, suppliers=suppliers)
        lines = ["**UN Transport Classification**\n"]
        for item in data.get("results", []):
            lines.append(f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})")
            lines.extend(_form_disclosure_lines(item))  # CI-572
            lines.extend(_transport_item_lines(item))
            lines.append(f"*Data source: {item.get('data_source', 'unknown')}*\n")
        if data.get("unresolved"):
            lines.extend(_unresolved_block(data))
        if not data.get("results"):
            lines.append("No transport-classification data found for the given chemicals.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("get_transport_classification", chemicals,
                        _intent_params({"chemicals": chemicals, **_supplier_params(chemicals, suppliers)}, intent),
                    success=success, error_message=error_msg)
