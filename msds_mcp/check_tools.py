from __future__ import annotations

import asyncio
import contextlib
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from typing import Annotated

from msds_mcp.app import API_URL, LANG, mcp
from msds_mcp.blocks import (_batch_not_analysed, _batch_pair_structured, _batch_truncation_block, _no_hazard_basis_block, _pair_sides, _physical_form_disclosures_block, _precursor_disclosure_block, _precursor_index, _precursor_pair_note, _query_form_disclosure_block, _rejected_products_block, _unresolved_block)
from msds_mcp.calls import (_expose, _graceful_timeout, _intent_params, _log_intent, _reported, _strip_usage, _supplier_params, _with_usage)
from msds_mcp.direct import (_direct_batch, _direct_sds_document, _direct_sds_draft, _direct_validate_sds)
from msds_mcp.disclosures import _form_disclosure_lines
from msds_mcp.document_tools import _session_from_batch
from msds_mcp.params import (Chemical, Intent, Lang, Suppliers, _normalize_catalog_lang, _require_api_key, _text_result)
from msds_mcp.regulatory_lists import _direct_regulatory_lists, _format_regulatory_lists
from msds_mcp.render import (_REFERENCE_SHEET, _doc_link_lookup, _format_sds_documents, _inline_sds, _pair_basis_label, _traceability_label)
from msds_mcp.sds_authoring import _SDS_QUALITY_STRINGS, _format_sds_draft, _format_sds_quality


# 与后端 batch-safety 的分析上限同值：超过它后端只算前 N 个，所以在这里拒收，别先收下再截断。
# 工具描述里的「2-12」是字面量，改这个值要一起改（test_batch_truncation 钉着）。
_MAX_BATCH_CHEMICALS = 12


@mcp.tool(annotations=ToolAnnotations(title="Batch Safety Check", read_only_hint=False, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def batch_safety_check(
    chemicals: Annotated[list[str], Field(
    description='List of chemical names or CAS numbers to check together, e.g. '
                '["acetone", "sulfuric acid", "sodium hydroxide", "methanol"]. '
                'Accepts 2-12 items; this runs pairwise compatibility and per-chemical risk '
                'warnings in one call (no PPE), so cost and latency grow with the list length.',
    )],
    lang: Lang = None, suppliers: Suppliers = None, intent: Intent = None,
) -> str:
    """
    Run a comprehensive safety check on a list of chemicals in one call.

    Returns a combined report with:
    - Pairwise compatibility matrix (compatible/caution/incompatible)
    - Key risk warnings per chemical, with the source SDS for each

    It does NOT return PPE or storage grouping — for those call
    `get_ppe_recommendation` / `get_storage_guidance`. (This list previously
    advertised both; the description is what you read when choosing a tool, so
    naming an output that never arrives invites answering from nothing.)

    Good first call when reviewing the chemical list of an experiment protocol: it
    covers the pairwise interactions in one round-trip.

    Side effect: when called with a signed-in account, the result is also saved
    as an audit session in that account and its session_id is returned, so
    `get_audit_report` can fetch it later. Anonymous calls save nothing.

    Args:
        chemicals: List of chemical names or CAS numbers (2-12 items), e.g.
                   ["acetone", "sulfuric acid", "sodium hydroxide", "methanol"]
    """
    error_msg = None
    success = True
    try:
        if len(chemicals) < 2:
            return "Please provide at least 2 chemicals for a batch safety check."
        if len(chemicals) > _MAX_BATCH_CHEMICALS:
            return (
                f"Maximum {_MAX_BATCH_CHEMICALS} chemicals per batch check; received "
                f"{len(chemicals)} and checked none of them. Please split into smaller groups. "
                "Each call only checks pairs within its own group, so pairs split across "
                "groups stay unchecked until you overlap the groups or run "
                "check_chemical_compatibility on those pairs."
            )

        # CI-1135：批量分析与会话容器**并发**跑 —— 两者互不依赖，串起来会把这个工具的
        # 延迟预算翻倍（各自最多吃 `TIMEOUT_MULTI`）。会话是副产物：它失败只丢副产物。
        #
        # 🔴 **不能用裸 `asyncio.gather`**（`/code-review` 抓到，而这正是我那条不变式的
        # 反面）：`gather` 默认 `return_exceptions=False` ⇒ 主路抛异常时异常立刻传播，
        # **兄弟 task 不会被取消**，它会继续打 `/sessions` 三个端点（各自 `TIMEOUT_MULTI`，
        # 合计约 135s）—— **恰好在后端已经撑不住的时候继续给它加压**，而且建出来的会话
        # 没有任何人看得到（工具已经带着错误返回了）。
        # ⚠️ 我原来只写了「会话失败不影响答案」，漏掉的是「**答案失败而会话还在跑**」。
        # 🔴 也不能改成 `return_exceptions=True`：那会让主路的错误**等到副产物跑完才返回**
        # （最坏 +135s），把一个快速失败变成一次超长挂起。
        _batch_task = asyncio.create_task(
            _direct_batch(chemicals, lang=lang, suppliers=suppliers))
        _session_task = asyncio.create_task(_session_from_batch(chemicals))
        try:
            data = await _batch_task
        except BaseException:
            # 🔴 `BaseException` 而不是 `Exception`：本工具外层套着 `_graceful_timeout`，
            # 取消/超时走的是 `CancelledError`（BaseException 的子类）—— 只接 `Exception`
            # 会让那条路径把副产物 task 漏在后台。
            _session_task.cancel()
            with contextlib.suppress(BaseException):
                await _session_task          # 让取消落地，别留 pending task 警告
            raise
        session_id = await _session_task
        sections = []

        sections.append("# Batch Safety Report")
        chem_list = ", ".join(chemicals)
        sections.append(f"**Chemicals ({len(chemicals)}):** {chem_list}\n")
        # 🔴 紧跟抬头：抬头刚说「Chemicals (N)」，截断这件事必须挨着它说，
        # 埋在报告中段等于没说。入口上限与后端同值时这块不会触发，留着防后端把上限调低。
        sections.extend(_batch_truncation_block(data, chemicals))

        if data.get("unresolved"):
            sections.extend(_unresolved_block(data, trailing_newline=True))
        sections.extend(_rejected_products_block(data))
        # 🔴 CI-842：同上两个工具——身份先于内容。
        sections.extend(_query_form_disclosure_block(data))
        sections.extend(_physical_form_disclosures_block(data))
        sections.extend(_precursor_disclosure_block(data))
        sections.extend(_no_hazard_basis_block(data))

        # CI-89: extract documents and build SDS-backed chemical set
        documents = data.get("documents", [])
        sds_backed_chemicals = {
            (doc.get("chemical_name") or doc.get("chemical") or "").lower()
            for doc in documents
        }
        doc_lut = _doc_link_lookup(documents)  # CI-89-inline

        # Compatibility
        sections.append("## 1. Compatibility Matrix")
        compat = data.get("compatibility", {})
        summary = compat.get("summary", {})
        if summary:
            sections.append(
                f"Total pairs: {summary.get('total', 0)} | "
                f"Compatible: {summary.get('compatible', 0)} | "
                f"Caution: {summary.get('caution', 0)} | "
                f"Incompatible: {summary.get('incompatible', 0)}\n"
            )
        # CI-1099：批量面的判定行同样要带限制（`_precursor_index` 读的是**顶层**披露，
        # 后端在截断闸门之前算它 ⇒ 被披露的物质可能没进这轮分析，所以只在**有判定行**的
        # 那一对上挂，`_batch_truncation_block` 继续管「谁没进来」那半）。
        batch_precursor_index = _precursor_index(data)
        for pair in compat.get("pairs", []):
            level = pair.get("level", "unknown").upper()
            # 出处前缀由 traceability 决定（_pair_basis_label），缺省按规则层
            traceability = pair.get("traceability", "rule_based")
            basis_label = _pair_basis_label(traceability)
            restrict_clause, restrict_lines = _precursor_pair_note(
                batch_precursor_index, *_pair_sides(pair))
            line = (
                f"- **{pair.get('chem1', '?')}** + **{pair.get('chem2', '?')}**: "
                f"{level} — {pair.get('reason', 'N/A')}  [{basis_label}]{restrict_clause}"
            )
            if restrict_lines:
                line += "\n" + "\n".join(restrict_lines)
            l1 = _inline_sds(doc_lut, pair.get("chem1"))
            l2 = _inline_sds(doc_lut, pair.get("chem2"))
            if l1:
                line += f"\n  **{pair.get('chem1', '?')}**{l1}"
            if l2:
                line += f"\n  **{pair.get('chem2', '?')}**{l2}"
            sections.append(line)

        # Risk warnings
        sections.append("\n## 2. Risk Warnings")
        for w in data.get("risk_warnings", []):
            # CI-89: label each warning by traceability
            traceability = w.get("traceability")
            trace_label = _traceability_label(
                traceability, w.get("chemical"), sds_backed_chemicals)
            inline = _inline_sds(doc_lut, w.get("chemical"), w.get("cas"))  # CI-89-inline
            sections.append(
                f"### {w.get('chemical', 'Unknown')} — {w.get('level', 'unknown').upper()} RISK "
                f"{trace_label}{inline}\n"
                f"- {w.get('description', 'N/A')}\n"
                f"- Mitigation: {w.get('mitigation', 'N/A')}"
            )

        if not data.get("risk_warnings"):
            # 🔴 CI-666：同上——`no_hazard_basis` 已经逐条说明时别再盖一句
            # "No risk data available."，那句读起来就是「查过了、没有」。
            if not data.get("no_hazard_basis"):
                sections.append("No risk data available.")

        # CI-89: append SDS document links
        if documents:
            sections.append(_format_sds_documents(documents))

        sections.append(
            "\n---\n*Need a filed record of this? Call `get_audit_report()` with no "
            "arguments — it builds a signed PDF from what you have analysed here, "
            "nothing to restate.*"
        )

        structured = {
            **_expose(data),
            "chemicals": chemicals,
            "compatibility": {
                "summary": compat.get("summary", {}),
                # 透传：此前 11 个字段只透出 5 个，丢的是 cas_a/cas_b/citation/source/
                # source_detail/verdict —— 全是可追溯性字段
                # 🔴 CI-1099 review 抓到：文本面挂了、结构化面漏了。而下面 CI-570 那条
                # 注释正写着**这个工具**有只读 structuredContent 的消费者（claude.ai
                # 连接器）⇒ 漏这一处等于 CI-1096 的失效形态从这个端点原样复发。
                # 「两个面各挂一次」是本票的固有形状，别只改自己正在看的那一面。
                "pairs": [
                    _batch_pair_structured(p, batch_precursor_index)
                    for p in compat.get("pairs", [])
                ],
            },
            # 同 get_chemical_risk_warnings：逐条透传，别再手抄字段
            "risk_warnings": [_expose(w) for w in data.get("risk_warnings", [])],
            "documents": documents,
            # 🔴 CI-570：上面那行 `"chemicals": chemicals` 回的是**调用方提交的全部**，
            # 而 pairs 只覆盖前 12 个 ⇒ 光改文本面的话，只拿 structuredContent 的客户端
            # （claude.ai 连接器）读到的仍是一份声称覆盖 20 个的报告。
            "not_analysed": _batch_not_analysed(data, chemicals),
        }
        # CI-1135：会话是**已经建好的**，所以这里陈述事实、不提要求。
        # 🔴 前两次失败都是因为这句话是「请你再调一个工具」——那一步会被 agent 省掉。
        # 匿名调用没有会话（没有归属），那时一个字都不加。
        if session_id:
            structured["session_id"] = session_id
            sections.append(
                f"\n---\n📋 This analysis was saved as audit session `{session_id}`. "
                f"A signed PDF report of it can be fetched with "
                f"`get_audit_report(session_id=\"{session_id}\")`."
            )
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(sections))],
            structured_content=structured,
        ), data)
    finally:
        _log_intent("batch_safety_check", chemicals,
                        _intent_params({"chemicals": chemicals, **_supplier_params(chemicals, suppliers)}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Check Regulatory Lists", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def check_regulatory_lists(chemical: Chemical, lang: Lang = None, intent: Intent = None) -> str:
    """
    Check which international regulatory lists a chemical appears on.

    Searches regulatory lists across 9 jurisdictions plus 3 international conventions:
    - US: EPA TSCA Inventory, OSHA PEL, California Prop 65
    - EU: SVHC Candidate List, REACH Annex XVII, REACH Annex XIV, REACH registered
      substances, CLP Annex VI, Seveso III, Water Framework Directive priority substances
    - APAC: China Catalogue of Hazardous Chemicals, China IECSC, Japan CSCL,
      Korea KECL, Korea Restricted / Prohibited Substances, Australia AIIC, Singapore EPMA,
      Taiwan MOENV Listed Toxic Chemical Substances, Taiwan MOENV Concerned Chemical
      Substances, Taiwan OSHA Priority Management Chemicals, Taiwan OSHA Controlled
      Chemicals, Taiwan OSHA Permissible Exposure Standards
    - Americas: Canada DSL
    - Conventions: Rotterdam PIC, Stockholm POPs, Montreal Protocol
    - Dual-use / export control: CWC Schedules 1/2/3, Australia Group precursors
    It also reports presence in the EPA CompTox Dashboard, which is an identifier
    resource rather than a regulatory list.

    Coverage limits, so the answer is not over-read:
    - Taiwan: the four restriction lists above plus the OSHA Permissible Exposure
      Standards, which record membership only (no limit values, not a restriction).
      There is no Taiwan inventory (TCSI), so absence says nothing about whether a
      substance is registered there. The result carries a link to the official
      Taiwan OSHA CSNN query page instead: pass it on, and say we do not determine
      existing / new substance status there.
    - Korea: the Restricted / Prohibited Substances list holds restriction and
      prohibition designations only. Hazardous designated substances (e.g. benzene)
      are NOT on it, so absence from it is not a clearance.
    - There is NO IARC coverage. Do not infer it from this tool.
    - The lists are a curated snapshot, not a live regulatory feed. A chemical missing
      from a list means "not found in our copy of that list", never "not regulated".

    Returns a summary of all matching lists, helping you understand
    a chemical's global regulatory footprint at a glance.

    Args:
        chemical: Chemical name or CAS number
    """
    error_msg = None
    success = True
    try:
        # CI-523: this used to hand an English sentence to `_quick_chat`
        # (RAI → intent → summary, three LLM round-trips) for what is a table
        # lookup. Two things came out of that, both observed on Prod:
        #   ① the RAI classifier could reject it outright — "which lists is benzene
        #      on" was answered with "MSDS Chain is designed for chemical safety
        #      inquiries only";
        #   ② the answer was a summariser's retelling, so the two explicit
        #      disclosures the backend had already built — CI-507's "could not
        #      check ≠ not on any list" and CI-375's unresolved wording — were
        #      paraphrased away or dropped. Repeating the identical call gave
        #      answers whose length varied by more than an order of magnitude.
        # Now it calls the deterministic endpoint and renders it. Same decision as
        # the Direct Service Layer switch; this tool was not migrated with the rest.
        # 🔴 Keep the credential requirement the old path had. `_quick_chat` calls
        # `_require_api_key()`; the `_direct_*` helpers do not, so switching endpoints
        # would silently turn this into an anonymous, unattributed lookup — and per
        # the CI-506 note in `get_regulatory_coverage` the anonymous tenant path does
        # not even return the same list set. An access change is not a rendering fix.
        if err := _require_api_key():
            success, error_msg = False, "no_credential"
            return _text_result(
                f"Authentication required: {err}\n\n"
                "Get a free API key at https://msdschain.lagentbot.com (API Keys tab) "
                "and set it via MSDS_API_KEY or gateway authentication."
            )

        data = await _direct_regulatory_lists(chemical, lang=lang)
        if data.get("lists_unavailable"):
            success = False
            error_msg = "lists_unavailable"
        # Free lookup tool (LOOKUP_TOOLS) — same shape as the other direct lookup
        # tools: no credits line, `_usage` stripped out of structuredContent.
        return CallToolResult(
            content=[TextContent(type="text",
                                 text=_format_regulatory_lists(data, chemical, lang))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("check_regulatory_lists", [chemical],
                        _intent_params({"chemical": chemical}, intent),
                    success=success, error_message=error_msg)


class SdsIngredient(BaseModel):
    chemical: str = Field(description='Ingredient name or CAS number, e.g. "acetone" or "67-64-1".')
    concentration: str | None = Field(
        default=None,
        description='Optional, as the user wrote it (e.g. "60%", "1-5 % w/w"). Echoed next '
                    'to the drafted line; never parsed or checked.')


@mcp.tool(annotations=ToolAnnotations(title="Draft SDS Sections", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def draft_sds_sections(
    ingredients: Annotated[list[SdsIngredient], Field(
        description="The formulation's ingredients, 1-30 items.")],
    region: Annotated[str | None, Field(
        description='Jurisdiction for Section 15, e.g. "EU", "US", "TW", "KR". Required '
                    'whenever Section 15 is drafted (the default). Ask the user; there is '
                    'no neutral default.')] = None,
    sections: Annotated[list[int] | None, Field(
        description="Which sections to draft: a subset of [8, 15]. Omit for both.")] = None,
    lang: Lang = None,
    intent: Intent = None,
) -> CallToolResult:
    """
    Draft Section 8 (exposure controls / PPE) and Section 15 (regulatory information)
    of an SDS for the user's OWN mixture, from its ingredient list. No LLM: every line
    carries its source.

    - Section 8 is assembled per ingredient from a curated supplier SDS for which we
      hold the original PDF; each block cites supplier + revision date.
    - Section 15 comes from the government regulatory lists we hold for `region`,
      NOT from supplier SDSs.

    How to present (do not violate):
    - Show the `notice` first, verbatim: this is a DRAFT, not a compliant SDS; the SDS
      issuer must verify every line.
    - If Section 15 `lists_checked` is null, the lists could NOT be read: say nothing
      was checked. Never report that as "not on any list".
    - List every `not_drafted`, `unresolved` and `lookup_failed` ingredient to the
      user; that is what they must supply (upload_msds_pdf, or a CAS number).
    - Show `truncation_note` / `exposure_limits_note` next to the Section 8 block they
      belong to, not only in a summary: users copy one block at a time.
    - Section 2 (mixture classification) is not drafted by this tool.
    """
    error_msg = None
    success = True
    # Plain dicts too: the SDK hands us models, direct Python callers (tests, scripts)
    # hand us dicts — and the `finally` below must not be the thing that crashes.
    names = [(i.get("chemical") if isinstance(i, dict) else i.chemical) or ""
             for i in ingredients]
    try:
        # Same credential rule as check_regulatory_lists: the anonymous tenant path
        # does not return the same list set (CI-506), so §15 would silently differ.
        if err := _require_api_key():
            success, error_msg = False, "no_credential"
            return _text_result(
                f"Authentication required: {err}\n\n"
                "Get a free API key at https://msdschain.lagentbot.com (API Keys tab) "
                "and set it via MSDS_API_KEY or gateway authentication."
            )
        data = await _direct_sds_draft(
            [SdsIngredient.model_validate(i).model_dump() for i in ingredients],
            sections, region, lang=lang)
        # Free: the backend registers `draft_sds_sections` as `_free()` in its MCP
        # billing table, so no credits line (same shape as the lookup tools). If that
        # ever gets a price, this tool must start rendering usage like the value tools.
        return CallToolResult(
            content=[TextContent(type="text", text=_format_sds_draft(data, lang))],
            structured_content=_strip_usage(data),
        )
    finally:
        # Hand-written payload (redaction decision): ingredient names and the
        # requested scope, never the concentrations.
        _log_intent("draft_sds_sections", names,
                    _intent_params({"ingredients": names,
                                    "sections": sections, "region": region}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Validate SDS", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def validate_sds(
    session_id: Annotated[str, Field(
        description="The session the SDS was uploaded into: the `session_id` that "
                    "`upload_msds_pdf` returned.")],
    msds_id: Annotated[int | None, Field(
        description="The uploaded SDS to check: the `msds_id` printed next to it by "
                    "`upload_msds_pdf`. OMIT IT to check every SDS uploaded in the session.",
        ge=1, le=2**31 - 1)] = None,
    lang: Lang = None,
    intent: Intent = None,
) -> CallToolResult:
    """
    Check an SDS the user uploaded for quality problems: contradictions between its
    own sections (e.g. Section 2 hazards vs Section 3 composition vs Section 14
    transport), gaps, age, and US OSHA HazCom format rules. No LLM: rule-based, and
    every finding quotes the lines of the SDS it is based on.

    Use it after `upload_msds_pdf`, when the user wants to know whether a supplier's
    SDS (or their own) is consistent and up to date. It checks only SDSs uploaded by
    this user, not records from our library. Jurisdiction: US OSHA HazCom only.

    How to present (do not violate):
    - Show the `notice` first, verbatim. "No issues" means these checks found
      nothing; it is NOT a statement that the SDS is compliant. Never say "compliant".
    - Give every issue with its section(s) and its quoted original text, so the user
      can find it in their document.
    - Say which checks were skipped (`checks_skipped`): those could not read their
      input from this SDS, so they did not look.
    - It does not rewrite or correct the SDS; it only points at the problems.
    """
    error_msg = None
    success = True
    try:
        # Same rule as upload_msds_pdf: it only reads the caller's own uploads, so
        # there is nothing to answer without knowing who the caller is.
        if err := _require_api_key():
            success, error_msg = False, "no_credential"
            return _text_result(
                f"Authentication required: {err}\n\n"
                "Get a free API key at https://msdschain.lagentbot.com (API Keys tab) "
                "and set it via MSDS_API_KEY or gateway authentication."
            )
        data = await _direct_validate_sds(session_id, msds_id, lang=lang)
        if data is None:
            success, error_msg = False, "not_found"
            lg = _normalize_catalog_lang(lang or LANG)
            return _text_result(_SDS_QUALITY_STRINGS.get(lg, _SDS_QUALITY_STRINGS["en"])["not_found"])
        # Free: the backend registers `validate_sds` / `validate_sds_session` as
        # `_free()` in its MCP billing table, so no credits line. If either ever gets a
        # price, this tool must start rendering usage like the value tools.
        return CallToolResult(
            content=[TextContent(type="text", text=_format_sds_quality(data, lang))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("validate_sds", [],
                    _intent_params({"session_id": session_id, "msds_id": msds_id}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get SDS Document", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_sds_document(chemical: Chemical, intent: Intent = None) -> CallToolResult:
    """
    Return a signed download URL for the original SDS/MSDS PDF of a chemical.

    The URL is valid for approximately 5 minutes and can be opened in a browser
    or downloaded with `curl -O`. The response also includes the document's
    source (supplier, region, revision date) so the provenance is clear.

    If no original PDF is on file, or the chemical is not in the database at
    all, the tool says so and suggests uploading an SDS PDF via `upload_msds_pdf`.

    Args:
        chemical: Chemical name or CAS number, e.g. "acetone" or "67-64-1"
    """
    error_msg = None
    success = True
    try:
        if err := _require_api_key():
            return _text_result(
                f"Authentication required: {err}\n\n"
                "Get a free API key at https://msdschain.lagentbot.com (API Keys tab) "
                "and set it via MSDS_API_KEY or gateway authentication."
            )

        data = await _direct_sds_document(chemical)
        available = data.get("available", False)

        if available:
            relative = data.get("pdf_url", "")
            full_url = f"{API_URL}{relative}" if relative.startswith("/") else relative
            supplier = data.get("supplier", "unknown supplier")
            region = data.get("region", "")
            revision_date = data.get("revision_date") or "unknown"
            cas = data.get("cas", "N/A")
            chem_name = data.get("chemical_name", chemical)

            # CI-277: product/mixture SDS records carry no CAS of their own.
            kind = data.get("record_kind") or "substance"
            document_kind = data.get("document_kind")
            region_suffix = f" · {region}" if region else ""
            doc_label = ("Reference document (not a supplier SDS)"
                         if document_kind == _REFERENCE_SHEET else "SDS Document")
            heading = (
                f"**{doc_label}: {chem_name}** (formulated product — no single CAS)"
                if kind == "product"
                else f"**{doc_label}: {chem_name}** (CAS: {cas})"
            )
            lines = [
                heading,
                f"- **Source:** {supplier}{region_suffix}",
                f"- **Revision date:** {revision_date}",
                f"- **Signed URL** (valid ~5 min):",
                f"  {full_url}",
                "",
                "Open in a browser or `curl -O` to download the PDF.",
            ]
            # 🔴 CI-572 review 抓到的第七条面：后端在 `/sds-document-url` 上**早就**
            # 产出这两个键，而这里只把它们塞进 structuredContent、文本里一个字没有
            # ——多数客户端只把 text 喂给模型 ⇒ 用户拿到 HF 的 PDF 链接和出处，
            # 却读不到「这份是水溶液、无水的我们没有」。与本票其余六条同一条理由。
            lines.extend(_form_disclosure_lines(data))
            if document_kind == _REFERENCE_SHEET:
                lines.insert(1, (
                    "- ⚠️ This is a **public reference fact sheet**, not the supplier's SDS. "
                    "It is not a substitute for the manufacturer's SDS in a hazard-communication "
                    "file; ask the supplier for theirs, or upload it with `upload_msds_pdf`."
                ))
            if kind == "product":
                lines.insert(1, (
                    "- ⚠️ This is a **formulated product**. Its GHS classification "
                    "applies to the mixture as a whole and must not be extrapolated "
                    "to any individual ingredient."
                ))
            return CallToolResult(
                content=[TextContent(type="text", text="\n".join(lines))],
                structured_content={
                    "available": True,
                    "record_kind": kind,
                    "document_kind": document_kind,
                    "chemical_name": chem_name,
                    "cas": cas,
                    "supplier": supplier,
                    "revision_date": revision_date,
                    "region": region,
                    "record_id": data.get("record_id"),
                    # CI-308: sha256 of the raw PDF bytes — the only exact key for
                    # "is this the same physical file as get_sds_section returned",
                    # since record_id comes from a different table on each path.
                    "pdf_hash": data.get("pdf_hash"),
                    # CI-347 的形态披露（文本面见上方 `_form_disclosure_lines`；
                    # CI-572 之前这里**只有** structuredContent，文本里没有）
                    "physical_form": data.get("physical_form"),
                    "physical_form_disclosure": data.get("physical_form_disclosure"),
                    # 🔴 CI-615：这是一份**手写白名单**——后端新增的键不会自己出现在这里。
                    # 那票的原始复现就是打这个工具（水 → TMSP，成分段 0.03%），
                    # 而第一版只改了后端 ⇒ 这里漏抄 = 用户仍然什么都看不到。
                    # `null` ＝ 这份 SDS 没声明百分比，**不表示「就是纯的」**。
                    "preparation_percent": data.get("preparation_percent"),
                    # CI-1408：`preparation_percent` 是区间的**上界**——只给它，机器消费者
                    # 会把 `35–38%` 说成「含 38%」。下界与念给人看的区间文本一起抄。
                    "preparation_percent_low": data.get("preparation_percent_low"),
                    "preparation_percent_text": data.get("preparation_percent_text"),
                    "preparation_disclosure": data.get("preparation_disclosure"),
                    "pdf_url": full_url,
                    "expires_in_seconds": 300,
                },
            )
        else:
            message = data.get("message", "No SDS document available for this chemical.")
            chem_name = data.get("chemical_name", chemical)
            cas = data.get("cas", "")

            # Decide which follow-up to suggest based on the backend message.
            if "parsed" in message.lower() or "get_sds_section" in message.lower():
                hint = (
                    "\n\nThe database holds parsed text for this chemical — "
                    "use `get_sds_section(chemical, section_number)` to query a "
                    "specific SDS section (1-16)."
                )
            else:
                hint = (
                    "\n\nIf you have the SDS PDF, upload it with "
                    "`upload_msds_pdf(pdf_source)` to add it to the database."
                )

            # CI-277: the backend may report that we hold no SDS for this substance
            # but DO hold product SDSs that contain it. That is NOT a hit — the
            # message says so, and the concentration + no-extrapolation disclaimer
            # travel with it so a model cannot present the product's (milder) whole-
            # formulation hazards as the pure substance's.
            component_of = data.get("component_of_products")
            if component_of:
                hint = (
                    "\n\nThis is context, not a match: we do not have an SDS for the "
                    "substance itself. Do not use the product's hazard data for it."
                )

            display = f"{chem_name} (CAS: {cas})" if cas else chem_name
            structured = {
                "available": False,
                "chemical_name": chem_name,
                "cas": cas,
                "message": message,
            }
            if component_of:
                structured["component_of_products"] = component_of
            return CallToolResult(
                content=[TextContent(type="text", text=f"**{display}**: {message}{hint}")],
                structured_content=structured,
            )
    finally:
        _log_intent("get_sds_document", [chemical],
                        _intent_params({"chemical": chemical}, intent),
                    success=success, error_message=error_msg)
