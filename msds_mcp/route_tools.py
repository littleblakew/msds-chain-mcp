from __future__ import annotations

import re
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from typing import Annotated

from msds_mcp.app import mcp
from msds_mcp.blocks import (_precursor_disclosure_block, _precursor_index, _precursor_pair_note,
                             _query_form_disclosure_block, _rejected_products_block, _unresolved_block)
from msds_mcp.calls import (_expose, _graceful_timeout, _intent_params, _log_intent, _reported,
                            _strip_usage, _with_usage)
from msds_mcp.direct import _direct_route_review, _direct_thermal_hazard
from msds_mcp.disclosures import _form_disclosure_lines, _insufficient_lines
from msds_mcp.params import Chemical, Intent, Lang, _require_api_key, _text_result
from msds_mcp.render import _pair_basis_label

_MAX_STEPS = 10
_MAX_STEP_CHEMICALS = 12

_AUTH_HINT = (
    "Get a free API key at https://msdschain.lagentbot.com (API Keys tab) "
    "and set it via MSDS_API_KEY or gateway authentication."
)

# ---------------------------------------------------------------------------
# Route review
# ---------------------------------------------------------------------------

_ROUTE_STATUS_LABEL = {
    "hazard_found": "HAZARD FOUND: at least one incompatible pair",
    "caution": "CAUTION: at least one pair needs care",
    "incomplete": "INCOMPLETE: some names or pairs could not be assessed",
    "no_known_incompatibility_among_checked": "No known incompatibility among the pairs checked",
    "no_pairs": "No pairs to check",
}

# A pair at this level is reported as clean; every other level is listed.
_CLEAN_LEVEL = "no_known_incompatibility"

_LEVEL_LABEL = {
    "incompatible": "INCOMPATIBLE",
    "caution": "CAUTION",
    "unverified": "UNVERIFIED",
}


class RouteStep(BaseModel):
    label: str | None = Field(
        default=None, max_length=200,
        description='Optional short name, e.g. "Step 1: amide coupling".')
    chemicals: list[str] = Field(
        description='Reagents, solvents, catalysts and workup agents present in this step, '
                    'by name or CAS (1-12 items).')
    carryover: list[str] | None = Field(
        default=None,
        description='Names from earlier steps that are still present in THIS step. Omit when '
                    'unknown (everything present in the previous step is then assumed to remain, '
                    'the worst case). Use [] when the previous step was fully worked up or quenched.')


def _status_label(status: str | None) -> str:
    return _ROUTE_STATUS_LABEL.get(status or "", status or "unknown")


def _pair_line(pair: dict, index: dict) -> list[str]:
    # 后端配对行的名字键是 `chem1` / `chem2`（与 batch-safety 同一形状）。
    a = pair.get("chem1") or pair.get("cas_a") or "?"
    b = pair.get("chem2") or pair.get("cas_b") or "?"
    level = pair.get("level") or "unknown"
    label = _LEVEL_LABEL.get(level, str(level).upper())
    basis = _pair_basis_label(pair.get("traceability"))
    clause, extra = _precursor_pair_note(
        index, (pair.get("chem1"), pair.get("cas_a")), (pair.get("chem2"), pair.get("cas_b")))
    line = f"- **{a}** + **{b}**: {label}: {pair.get('reason') or 'N/A'}  [{basis}]{clause}"
    source = _readable_source(pair)
    if source:
        line += f"\n  Source: {source}"
    return [line, *extra]


_CODE_RE = re.compile(r"[a-z0-9_:.\-]+")


def _readable_source(pair: dict) -> str:
    """The human-readable source line, or "" when there is none.

    Rule-based pairs carry their reference in `source_detail`; `citation` / `source` can be
    internal keys (`lit:...`, `unverified_insufficient_data`) that mean nothing to a reader.
    The verdict level is already on the line, so a bare key adds nothing.
    """
    detail = (pair.get("source_detail") or "").strip()
    if detail:
        # For an unverified pair the detail is the reason sentence itself; don't print it twice.
        return "" if detail == (pair.get("reason") or "").strip() else detail
    for key in ("citation", "source"):
        val = pair.get(key)
        if isinstance(val, str) and val.strip() and not _CODE_RE.fullmatch(val.strip()):
            return val.strip()
    return ""


def _pairs_block(pairs: list[dict], index: dict) -> list[str]:
    """Non-clean pairs listed in full; clean pairs only counted."""
    lines: list[str] = []
    clean = 0
    for p in pairs or []:
        if p.get("level") == _CLEAN_LEVEL:
            clean += 1
            continue
        lines.extend(_pair_line(p, index))
    if clean:
        lines.append(f"- {clean} pair(s) with no known incompatibility in our data.")
    return lines


def _handling_block(handling: dict | None) -> list[str]:
    """Per-chemical PPE and waste, rendered with the same honesty rules as the single tools.

    A missing PPE level means the record had no hazard data, never "low hazard"; a waste item
    without a basis shows the insufficient-data lines, never the fallback bucket as a finding.
    """
    if not handling:
        return []
    out = ["## Handling by chemical"]
    ppe = handling.get("ppe") or {}
    if ppe.get("failed"):
        out.append("- PPE lookup failed for this call; use `get_ppe_recommendation`.")
    for item in ppe.get("results") or []:
        name = f"**{item.get('chemical_name', '?')}**"
        if item.get("insufficient_hazard_data") or item.get("minimum_ppe_level") is None:
            out.append(f"- {name} PPE: CANNOT BE DETERMINED (no hazard data on this record; "
                       "this is not a low-hazard finding).")
        else:
            items = "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
                              for k, v in (item.get("ppe") or {}).items())
            out.append(f"- {name} PPE level {item['minimum_ppe_level']}" + (f" ({items})" if items else ""))
        out.extend(f"  {line}" for line in _form_disclosure_lines(item))
        if item.get("hazard_classification_conflict_note"):
            out.append(f"  {item['hazard_classification_conflict_note']}")
    waste = handling.get("waste") or {}
    if waste.get("failed"):
        out.append("- Waste lookup failed for this call; use `get_waste_disposal`.")
    for item in waste.get("results") or []:
        name = f"**{item.get('chemical_name', '?')}**"
        if item.get("insufficient_hazard_data"):
            out.append(f"- {name} waste:")
            out.extend(f"  {line}" for line in _insufficient_lines(item, "Waste classification"))
        else:
            out.append(f"- {name} waste: {item.get('waste_classification', 'N/A')}")
    out.append("")
    return out


def _format_route_review(data: dict, steps: list[dict]) -> str:
    index = _precursor_index(data)
    out = ["# Synthesis Route Review"]
    out.append(f"**Overall: {_status_label(data.get('overall_status'))}** "
               f"({data.get('pairs_checked', 0)} pair(s) checked across {len(steps)} step(s))")
    out.append("")
    out.extend(_unresolved_block(data, trailing_newline=True))
    out.extend(_rejected_products_block(data))
    out.extend(_query_form_disclosure_block(data))
    out.extend(_precursor_disclosure_block(data))

    for st in data.get("steps") or []:
        title = f"## Step {st.get('step', '?')}"
        if st.get("label"):
            title += f": {st['label']}"
        out.append(f"{title} ({_status_label(st.get('status'))})")
        names = ", ".join(
            f"{c.get('name')}" + (f" (CAS {c['cas']})" if c.get("cas") else "")
            for c in st.get("chemicals") or [] if isinstance(c, dict))
        if names:
            out.append(f"Chemicals: {names}")
        out.extend(_pairs_block(st.get("pairs") or [], index))
        out.append("")

    for tr in data.get("transitions") or []:
        out.append(f"## Carryover, step {tr.get('from_step', '?')} to step {tr.get('to_step', '?')} "
                   f"({_status_label(tr.get('status'))})")
        carried = ", ".join(tr.get("carryover") or [])
        if tr.get("carryover_assumed"):
            out.append("Carryover was not stated, so everything present in the previous step is "
                       "assumed to remain (worst case)"
                       + (f": {carried}" if carried else "") + ".")
        elif carried:
            out.append(f"Carried over as stated: {carried}.")
        else:
            out.append("Nothing carried over (stated as fully worked up).")
        out.extend(_pairs_block(tr.get("pairs") or [], index))
        out.append("")

    out.extend(_handling_block(data.get("handling")))

    if data.get("assumptions"):
        out.append("## Assumptions")
        out.extend(f"- {a}" for a in data["assumptions"])
        out.append("")
    if data.get("scope_note"):
        out.append(f"> {data['scope_note']}")
    out.append("> Reaction heat, runaway risk and process conditions are not assessed here; "
               "use `assess_thermal_hazard` for thermal screening.")
    return "\n".join(out)


@mcp.tool(annotations=ToolAnnotations(title="Review Synthesis Route", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def review_synthesis_route(
    steps: Annotated[list[RouteStep], Field(
        description='The route as an ordered list of steps (1-10), each with the chemicals '
                    'present in it (1-12) and optionally which earlier chemicals carry over.')],
    include_handling: Annotated[bool, Field(
        description='Also return PPE and waste disposal for each resolved chemical. Slower '
                    '(one SDS lookup per chemical); leave false when only compatibility is needed.')] = False,
    lang: Lang = None, intent: Intent = None,
) -> str:
    """
    Screen a multi-step synthesis route for chemical incompatibilities.

    Convert the route the user describes into `steps`: list every reagent, solvent, catalyst and
    workup agent in each step. When the user says something was removed, quenched, washed out or
    evaporated, set `carryover` on the next step to what is still present; use [] for a fully
    worked-up step. If `carryover` is omitted, everything from the previous step is assumed to
    remain, which is the worst case and is stated in the result.

    Covers: pairwise incompatibility of chemicals within each step; pairwise incompatibility
    between what is carried over from the previous step and the chemicals of the next step;
    controlled-precursor flags on the named chemicals; names we could not resolve. With
    `include_handling`, also PPE and waste disposal per chemical (not aggregated per step).

    Does NOT cover: reaction heat, runaway or thermal stability (call `assess_thermal_hazard`
    for that), reaction yield or selectivity, equipment, quantities, or concentrations. The
    overall status never says a route is safe: the best result is "no known incompatibility among
    the pairs checked", which only describes the pairs we could evaluate.

    Limits: up to 10 steps, up to 12 chemicals per step, up to 24 distinct chemicals and 66
    pairs per route. Billed as a paid safety conclusion, like `batch_safety_check`.

    Args:
        steps: Ordered route steps, each {label?, chemicals[], carryover?}
    """
    payload = [RouteStep.model_validate(s).model_dump(exclude_none=True) for s in steps]
    flat = [c for s in payload for c in s.get("chemicals", [])]
    error_msg = None
    success = True
    try:
        if err := _require_api_key():
            success, error_msg = False, "no_credential"
            return _text_result(f"Authentication required: {err}\n\n{_AUTH_HINT}")
        if not payload:
            success, error_msg = False, "invalid_input"
            return "Please provide at least one step."
        if len(payload) > _MAX_STEPS:
            success, error_msg = False, "invalid_input"
            return f"A route may have at most {_MAX_STEPS} steps. Please split it."
        for i, s in enumerate(payload, 1):
            names = [c for c in s.get("chemicals", []) if c and c.strip()]
            if not names or len(names) > _MAX_STEP_CHEMICALS:
                success, error_msg = False, "invalid_input"
                return (f"Step {i} must list between 1 and {_MAX_STEP_CHEMICALS} chemicals "
                        "(blank names are not accepted).")

        data = await _direct_route_review(payload, lang=lang, include_handling=include_handling)
        structured = _expose(data)
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text=_format_route_review(data, payload))],
            structured_content=structured,
        ), data)
    finally:
        _log_intent("review_synthesis_route", flat,
                    _intent_params({"steps": len(payload), "chemicals": flat}, intent),
                    success=success, error_message=error_msg)


# ---------------------------------------------------------------------------
# Thermal hazard
# ---------------------------------------------------------------------------

_A6_LABEL = {
    "no_listed_group_found": "No listed explosophore group found",
    "exempt_by_oxygen_balance": "Exempt by oxygen balance",
    "exempt_by_dsc_energy": "Exempt by measured decomposition energy",
    "explosive_property_testing_indicated": "Explosive-properties testing is indicated",
    "invalid_structure": "The structure could not be parsed",
    "no_structure": "No structure available",
    "chemical_not_resolved": "The chemical could not be resolved",
}

_PROCESS_LABEL = {
    "dsc_onset_c": "DSC onset (C)",
    "decomposition_energy_j_g": "Decomposition energy (J/g)",
    "q_ref_w_kg": "Heat release at reference temperature (W/kg)",
    "t_ref_c": "Reference temperature (C)",
    "ea_kj_mol": "Activation energy (kJ/mol)",
    "td24_c": "TD24 (C)",
    "process_temp_c": "Process temperature (C)",
    "reaction_enthalpy_kj_kg": "Reaction enthalpy (kJ/kg)",
    "adiabatic_rise_k": "Adiabatic temperature rise (K)",
    "cp_kj_kg_k": "Specific heat (kJ/kg/K)",
    "accumulation_fraction": "Accumulation fraction",
    "mtt_c": "MTT (C)",
}

_REVIEW_LINE = ("Review by a process safety specialist is required before acting on any of this; "
                "these are screening flags, not a thermal stability assessment.")


def _format_thermal(data: dict, chemical: str | None) -> str:
    name = data.get("chemical") or chemical
    title = "# Thermal Hazard Screening" + (f": {name}" if name else "")
    if data.get("cas"):
        title += f" (CAS {data['cas']})"
    out = [title]

    flags = data.get("red_flags") or []
    if flags:
        out.append("**Red flags:**")
        out.extend(f"- {f}" for f in flags)
    else:
        out.append("**Red flags:** none raised from the inputs given (this is not a safety conclusion).")
    out.append("")

    detail = data.get("unresolved_detail")
    if isinstance(detail, dict):
        reason = (detail.get("reason_en") or detail.get("reason") or "").strip()
        if reason:
            out.append(f"**Unresolved:** {reason}")
            out.append("")

    struct = data.get("structure") or {}
    sc = data.get("screening") or {}
    out.append("## Structural screening")
    if data.get("screened") is False:
        out.append("**Screening was NOT performed.**"
                   + (f" {sc['message']}" if sc.get("message") else ""))
        out.append(f"Outcome: {_A6_LABEL.get(sc.get('a6_outcome'), sc.get('a6_outcome') or 'unknown')}. "
                   "This says nothing about the substance either way.")
    else:
        out.append(f"Outcome: {_A6_LABEL.get(sc.get('a6_outcome'), sc.get('a6_outcome') or 'unknown')}")
        if sc.get("message"):
            out.append(sc["message"])
        if struct.get("smiles"):
            out.append(f"Structure used: `{struct['smiles']}` (source: {struct.get('source') or 'unknown'})")
        for g in sc.get("groups") or []:
            out.append(f"- Group: {g.get('family') or g.get('key')}"
                       + (" (contains oxygen)" if g.get("contains_oxygen") else ""))
        ob = sc.get("oxygen_balance") or {}
        if ob.get("value_pct") is not None:
            out.append(f"Oxygen balance: {ob['value_pct']}%"
                       + (f" ({ob['formula_scope']})" if ob.get("formula_scope") else ""))
        if sc.get("criterion"):
            out.append(f"Criterion: {sc['criterion']}")
        if sc.get("red_flag"):
            out.append("Screening red flag: raised.")
    if sc.get("not_a_safety_conclusion"):
        out.append("This screening outcome is not a safety conclusion.")
    if sc.get("reference"):
        out.append(f"Reference: {sc['reference']}")
    out.append("")

    y = data.get("yoshida")
    if y:
        out.append("## Yoshida correlations")
        if y.get("computable"):
            out.append(f"- Explosive propagation: {y.get('explosive_propagation')}"
                       + (" (flag)" if y.get("ep_flag") else ""))
            out.append(f"- Shock sensitivity: {y.get('shock_sensitivity')}"
                       + (" (flag)" if y.get("ss_flag") else ""))
        else:
            out.append("Not computable from the inputs given.")
        if y.get("caveat"):
            out.append(f"Caveat: {y['caveat']}")
        if y.get("reference"):
            out.append(f"Reference: {y['reference']}")
        out.append("")

    proc = data.get("process") or {}
    out.append("## Process values (as supplied by you)")
    values = proc.get("values") or {}
    if values:
        for key, v in values.items():
            v = v if isinstance(v, dict) else {"value": v}
            line = f"- {_PROCESS_LABEL.get(key, key)}: {v.get('value')}"
            if v.get("source"):
                line += f" (source: {v['source']})"
            if v.get("bound"):
                line += f" [{v['bound']}]"
            out.append(line)
    else:
        out.append("No process values were supplied.")
    if proc.get("missing"):
        out.append("Missing inputs: " + ", ".join(_PROCESS_LABEL.get(k, k) for k in proc["missing"]))
    for a in proc.get("assumptions") or []:
        out.append(f"- Assumption: {a}")
    if proc.get("stoessel_class") is not None:
        line = f"**Stoessel criticality class: {proc['stoessel_class']}**"
        if proc.get("stoessel_meaning"):
            line += f": {proc['stoessel_meaning']}"
        out.append(line)
    else:
        out.append("Stoessel class: not determined (it needs measured values you supply; nothing is estimated).")
    if proc.get("reference"):
        out.append(f"Reference: {proc['reference']}")
    out.append("")

    if data.get("scope_note"):
        out.append(f"> {data['scope_note']}")
    out.append(f"> {_REVIEW_LINE}")
    return "\n".join(out)


def _num(desc: str, **kw) -> object:
    return Annotated[float | None, Field(description=desc, **kw)]


@mcp.tool(annotations=ToolAnnotations(title="Assess Thermal Hazard", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def assess_thermal_hazard(
    chemical: Annotated[str | None, Field(description='Chemical name or CAS number.')] = None,
    smiles: Annotated[str | None, Field(description='SMILES of the structure; used instead of a database structure when given.')] = None,
    dsc_onset_c: _num('Measured DSC onset temperature, C.') = None,
    decomposition_energy_j_g: _num('Measured decomposition energy, J/g.') = None,
    q_ref_w_kg: _num('Measured heat release rate at the reference temperature, W/kg.') = None,
    t_ref_c: _num('Reference temperature for q_ref_w_kg, C.') = None,
    ea_kj_mol: _num('Activation energy, kJ/mol (at least 20).', ge=20) = None,
    td24_c: _num('Temperature at which time to maximum rate is 24 h, C.') = None,
    process_temp_c: _num('Planned process temperature, C.') = None,
    reaction_enthalpy_kj_kg: _num('Desired-reaction enthalpy, kJ/kg.') = None,
    adiabatic_rise_k: _num('Adiabatic temperature rise of the desired reaction, K.') = None,
    cp_kj_kg_k: _num('Specific heat of the reaction mass, kJ/kg/K.') = None,
    accumulation_fraction: _num('Fraction of reagent that can accumulate, 0 to 1.', ge=0, le=1) = None,
    mtt_c: _num('Maximum temperature for technical reasons (for example solvent boiling point), C.') = None,
    lang: Lang = None, intent: Intent = None,
) -> str:
    """
    Screen a substance or process for thermal hazard flags.

    Does three things with what you give it: (1) a structural screening for energetic functional
    groups plus oxygen balance, from the name, CAS or SMILES; (2) Yoshida correlations when the
    inputs allow; (3) a Stoessel criticality class when the measured process values you supply
    are sufficient. All inputs are optional.

    Screening only raises flags. Finding no listed group is NOT a safety conclusion. The Stoessel
    class is computed only from measured values you supply; nothing is estimated or looked up, and
    missing inputs are listed. Pass the user's own DSC/calorimetry numbers, never guessed ones.

    Does NOT cover: incompatibility between chemicals (use `review_synthesis_route` or
    `check_chemical_compatibility`), dust explosion, or a substitute for calorimetry and review
    by a process safety specialist.

    Free lookup, no credits.

    Args:
        chemical: Name or CAS (optional)
        smiles: Structure (optional)
        Remaining numeric parameters are measured or planned process values, all optional.
    """
    inputs = {k: v for k, v in {
        "chemical": chemical, "smiles": smiles, "dsc_onset_c": dsc_onset_c,
        "decomposition_energy_j_g": decomposition_energy_j_g, "q_ref_w_kg": q_ref_w_kg,
        "t_ref_c": t_ref_c, "ea_kj_mol": ea_kj_mol, "td24_c": td24_c,
        "process_temp_c": process_temp_c, "reaction_enthalpy_kj_kg": reaction_enthalpy_kj_kg,
        "adiabatic_rise_k": adiabatic_rise_k, "cp_kj_kg_k": cp_kj_kg_k,
        "accumulation_fraction": accumulation_fraction, "mtt_c": mtt_c,
    }.items() if v is not None and v != ""}
    error_msg = None
    success = True
    try:
        if err := _require_api_key():
            success, error_msg = False, "no_credential"
            return _text_result(f"Authentication required: {err}\n\n{_AUTH_HINT}")
        data = await _direct_thermal_hazard(inputs, lang=lang)
        return CallToolResult(
            content=[TextContent(type="text", text=_format_thermal(data, chemical))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("assess_thermal_hazard", [chemical] if chemical else [],
                    _intent_params({"provided": sorted(inputs)}, intent),
                    success=success, error_message=error_msg)
