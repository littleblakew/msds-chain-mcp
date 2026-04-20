"""
MSDS Chain MCP Server

Exposes MSDS Chain's chemical safety tools as MCP tools so AI agents
(Claude Code, Cursor, Cline, etc.) can call them directly.

Usage:
    MSDS_API_KEY=sk-msds-xxx python server.py

Claude Code integration (~/.claude/settings.json):
    {
      "mcpServers": {
        "msds-chain": {
          "command": "python",
          "args": ["/path/to/mcp-server/server.py"],
          "env": { "MSDS_API_KEY": "sk-msds-your-key" }
        }
      }
    }
"""
from __future__ import annotations

import json
import json as _json
import os
import textwrap
import time

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("MSDS_API_KEY", "")
API_URL = os.environ.get(
    "MSDS_API_URL",
    "https://msds-chain-backend-prod.wonderfulgrass-f1545190.southeastasia.azurecontainerapps.io",
).rstrip("/")
LANG = os.environ.get("MSDS_LANG", "en")  # en | zh | ja | de | id
TIMEOUT = 30.0

mcp = FastMCP(
    "MSDS Chain",
    instructions=textwrap.dedent("""
        MSDS Chain provides chemical safety intelligence:
        - Compatibility checks between chemicals
        - Risk / hazard warnings
        - Multi-region regulatory compliance (EU REACH/CLP, US OSHA/TSCA, CN/JP/KR/CA/AU/TW)
        - MSDS / SDS data lookup

        Use these tools when a user mentions chemicals, asks about safety, storage,
        handling, or compliance in an experimental or lab context.
    """).strip(),
)


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


async def _quick_chat(message: str) -> dict:
    """POST /quick-chat and return the parsed response."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/quick-chat",
            json={"message": message, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


def _format_tool_results(tool_results: list[dict]) -> str:
    """Render tool_results as compact structured text for context."""
    if not tool_results:
        return ""
    lines = ["\n\n---\n**Raw tool data:**"]
    for item in tool_results:
        tool = item.get("tool", "unknown")
        result = item.get("result", {})
        lines.append(f"\n`{tool}`: {json.dumps(result, ensure_ascii=False)[:600]}")
    return "\n".join(lines)


async def _log_call(tool_name: str, chemicals: list[str] | None, duration_ms: int,
                    success: bool, error_message: str | None = None,
                    input_params: str | None = None):
    """Fire-and-forget: POST call record to backend. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{API_URL}/mcp/call-log",
                json={
                    "tool_name": tool_name,
                    "chemicals": chemicals,
                    "duration_ms": duration_ms,
                    "success": success,
                    "error_message": error_message,
                    "input_params": input_params,
                    "api_key": API_KEY or None,
                },
                headers=_headers(),
            )
    except Exception:
        pass  # fire-and-forget


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def check_chemical_compatibility(chemicals: list[str]) -> str:
    """
    Check pairwise compatibility between a list of chemicals.

    Returns compatibility status (compatible / caution / incompatible) for each
    pair, along with specific hazard reasons and storage recommendations.

    Use this before an experiment to verify it is safe to use the listed
    chemicals together in the same lab setting.

    Args:
        chemicals: List of chemical names or CAS numbers, e.g.
                   ["acetone", "methanol", "ethanol"] or ["67-64-1", "67-56-1"]
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        if len(chemicals) < 2:
            return "Please provide at least 2 chemicals to check compatibility."

        chem_list = ", ".join(chemicals)
        message = f"Check compatibility for these chemicals used together in an experiment: {chem_list}"
        data = await _quick_chat(message)
        return data["answer"] + _format_tool_results(data["tool_results"])
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("check_chemical_compatibility", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool()
async def get_chemical_risk_warnings(chemicals: list[str]) -> str:
    """
    Get hazard and risk warnings for one or more chemicals.

    Returns GHS hazard classification, signal words (Danger/Warning), H-codes,
    flash point, toxicity, and recommended PPE.

    Use this to understand the specific dangers of each chemical before handling
    or storing them.

    Args:
        chemicals: List of chemical names or CAS numbers, e.g.
                   ["acetone", "67-56-1"]
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        chem_list = ", ".join(chemicals)
        message = f"What are the risk warnings and hazard classifications for: {chem_list}"
        data = await _quick_chat(message)
        return data["answer"] + _format_tool_results(data["tool_results"])
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_chemical_risk_warnings", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool()
async def check_regulatory_compliance(
    chemicals: list[str],
    regions: list[str] | None = None,
) -> str:
    """
    Check multi-region regulatory compliance status for chemicals.

    Checks against: EU (SVHC/REACH/CLP/CMR), US (OSHA PEL/TSCA),
    CN, JP, KR, CA, AU, TW regulations.

    Use this when preparing export documentation, compliance audits,
    or when working with chemicals that may be restricted in certain jurisdictions.

    Args:
        chemicals: List of chemical names or CAS numbers
        regions:   Optional list of region codes to check, e.g. ["EU", "US", "CN"]
                   Defaults to EU + US if not specified.
                   Valid codes: EU, US, CN, JP, KR, CA, AU, TW
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        chem_list = ", ".join(chemicals)
        region_str = ", ".join(regions) if regions else "EU, US"
        message = (
            f"Check regulatory compliance for these chemicals: {chem_list}. "
            f"Regions: {region_str}"
        )
        data = await _quick_chat(message)
        return data["answer"] + _format_tool_results(data["tool_results"])
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("check_regulatory_compliance", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals, "regions": regions}))


@mcp.tool()
async def ask_chemical_safety(question: str) -> str:
    """
    Ask any chemical safety question in natural language.

    Handles: storage conditions, spill/exposure emergency procedures,
    first-aid measures, PPE requirements, disposal guidance, MSDS lookups,
    GHS label interpretation, and general lab safety questions.

    Use this as the catch-all when the question doesn't fit neatly into
    compatibility, risk warnings, or compliance checks.

    Args:
        question: Any chemical safety question, e.g.
                  "How should I store acetone and methanol in the same cabinet?"
                  "What PPE is needed when handling concentrated HCl?"
                  "Is it safe to dispose of acetone down the drain?"
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        data = await _quick_chat(question)
        return data["answer"] + _format_tool_results(data["tool_results"])
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("ask_chemical_safety", None, dur, success, error_msg,
                        _json.dumps({"question": question}))


@mcp.tool()
async def get_ppe_recommendation(chemicals: list[str]) -> str:
    """
    Get PPE (Personal Protective Equipment) recommendations for chemicals.

    Returns specific glove types, eye protection, respiratory protection, and
    body protection requirements based on MSDS Section 8 data and GHS hazard codes.

    Args:
        chemicals: List of chemical names or CAS numbers, e.g.
                   ["acetone", "hydrochloric acid"] or ["67-64-1"]
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        chem_list = ", ".join(chemicals)
        message = (
            f"What PPE (personal protective equipment) is required when handling: {chem_list}? "
            "Include specific glove material, eye protection type, respiratory protection, "
            "and body/skin protection requirements."
        )
        data = await _quick_chat(message)
        return data["answer"] + _format_tool_results(data["tool_results"])
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_ppe_recommendation", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool()
async def get_storage_guidance(chemicals: list[str]) -> str:
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
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        chem_list = ", ".join(chemicals)
        message = (
            f"How should I store these chemicals: {chem_list}? "
            "Include storage class, cabinet type, temperature requirements, "
            "and what materials they must be separated from."
        )
        data = await _quick_chat(message)
        return data["answer"] + _format_tool_results(data["tool_results"])
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_storage_guidance", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool()
async def get_emergency_response(chemical: str, scenario: str = "spill") -> str:
    """
    Get emergency response guidance for a chemical incident.

    Returns immediate actions, SDS-specific instructions from Section 4/5/6,
    and H-code-based guidance for three scenario types.

    Args:
        chemical: Chemical name or CAS number, e.g. "hydrochloric acid"
        scenario: Type of emergency — "spill" (leak/release), "fire", or
                  "exposure" (skin/eye/inhalation first aid). Defaults to "spill".
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        message = (
            f"What should I do if there is a {scenario} involving {chemical}? "
            "Include immediate actions, specific cleanup/response procedures, "
            "and any special precautions."
        )
        data = await _quick_chat(message)
        return data["answer"] + _format_tool_results(data["tool_results"])
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_emergency_response", [chemical], dur, success, error_msg,
                        _json.dumps({"chemical": chemical, "scenario": scenario}))


@mcp.tool()
async def get_exposure_limits(chemicals: list[str], region: str | None = None) -> str:
    """Get occupational exposure limits (OEL/TLV/PEL/MAC) for chemicals.

    Returns TWA, STEL, and Ceiling values from multiple standards:
    - OSHA PEL (US)
    - ACGIH TLV (International)
    - EU SCOEL IOELV
    - Japan 産衛研
    - China GBZ

    Args:
        chemicals: List of chemical names or CAS numbers
        region: Optional filter — "US", "EU", "JP", "CN", or "INT"
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        parts = [f"exposure limits for {', '.join(chemicals)}"]
        if region:
            parts.append(f"in region {region}")
        message = "What are the occupational " + " ".join(parts) + "?"
        data = await _quick_chat(message)
        return data["answer"] + _format_tool_results(data.get("tool_results", []))
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_exposure_limits", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals, "region": region}))


@mcp.tool()
async def get_transport_classification(chemicals: list[str]) -> str:
    """Get UN transport classification for chemicals (dangerous goods shipping).
    Returns UN number, proper shipping name, hazard class, packing group,
    and transport mode details (ADR road, IATA air, IMDG sea).
    Args:
        chemicals: List of chemical names or CAS numbers
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        message = f"What is the UN transport classification for {', '.join(chemicals)}?"
        data = await _quick_chat(message)
        return data["answer"] + _format_tool_results(data.get("tool_results", []))
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_transport_classification", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool()
async def create_audit_session(
    experiment_name: str,
    chemicals: list[str],
) -> str:
    """
    Run a full MSDS safety audit for a list of chemicals and return a session id.

    Creates a persistent audit session on MSDS Chain, runs pairwise compatibility
    and risk analysis across all chemicals, and returns a session_id that can later
    be passed to `get_audit_report` to fetch the signed PDF report URL.

    Use this when the user wants an archivable, signed record of a safety review
    (e.g. for SOPs, compliance audits, or to share with a PI / safety officer),
    rather than a one-off Q&A.

    Args:
        experiment_name: Short human-readable label for the audit, e.g.
                         "Grignard prep — 2026-04-16" or "Solvent screening #3".
        chemicals:       List of chemical names or CAS numbers to include in the
                         audit, e.g. ["acetone", "methanol", "67-64-1"].

    Returns:
        Session id + compatibility summary (compatible/caution/incompatible pair
        counts + top warnings). An API key must be configured (MSDS_API_KEY) so
        the session is bound to your account and the report is retrievable.
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        if not chemicals:
            return "Please provide at least one chemical to audit."
        if not API_KEY:
            return (
                "create_audit_session requires MSDS_API_KEY to be set so the session "
                "is tied to your account. Get one at https://msdschain.lagentbot.com "
                "(API Keys tab) and add it to the MCP server env."
            )

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # 1. Create the session (will be bound to API key owner)
            res = await client.post(
                f"{API_URL}/sessions",
                json={"experiment_name": experiment_name, "source": "mcp"},
                headers=_headers(),
            )
            res.raise_for_status()
            session_id = res.json()["session_id"]

            # 2. Persist chemicals as MsdsRecord (so report PDF has data)
            res = await client.post(
                f"{API_URL}/sessions/{session_id}/chemicals",
                json={"chemicals": chemicals},
                headers=_headers(),
            )
            res.raise_for_status()
            chem_result = res.json()

            # 3. Run compatibility + risk analysis (reads CAS from MsdsRecord)
            res = await client.post(
                f"{API_URL}/sessions/{session_id}/compatibility",
                json={},
                headers={**_headers(), "Accept-Language": LANG},
            )
            res.raise_for_status()
            compat = res.json()

        matrix = compat.get("matrix", [])
        warnings = compat.get("warnings", [])
        counts = {"compatible": 0, "caution": 0, "incompatible": 0}
        for pair in matrix:
            level = pair.get("level", "")
            if level in counts:
                counts[level] += 1

        added = chem_result.get("added", [])
        not_found = chem_result.get("not_found", [])
        added_names = [c["name"] for c in added if c.get("status") in ("added", "already_added")]

        lines = [
            f"**Session created:** `{session_id}`",
            f"**Experiment:** {experiment_name}",
            f"**Chemicals added:** {', '.join(added_names) or 'none'}",
        ]
        if not_found:
            lines.append(f"**Not found in database:** {', '.join(not_found)}")
        lines.append(
            f"\n**Compatibility pairs:** {len(matrix)} total — "
            f"{counts['compatible']} compatible, {counts['caution']} caution, "
            f"{counts['incompatible']} incompatible"
        )
        if counts["incompatible"] or counts["caution"]:
            lines.append("\n**Flagged pairs:**")
            for pair in matrix:
                if pair.get("level") in ("caution", "incompatible"):
                    lines.append(
                        f"- [{pair.get('level').upper()}] "
                        f"{pair.get('chem1')} + {pair.get('chem2')}: "
                        f"{pair.get('reason', '')[:200]}"
                    )
        if warnings:
            lines.append(f"\n**Risk warnings:** {len(warnings)}")
            for w in warnings[:5]:
                lines.append(
                    f"- [{w.get('level', '').upper()}] {w.get('chemical', '')}: "
                    f"{w.get('description', '')[:160]}"
                )

        lines.append(
            f"\nCall `get_audit_report(\"{session_id}\")` to retrieve the signed PDF URL."
        )
        return "\n".join(lines)
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("create_audit_session", chemicals, dur, success, error_msg,
                        _json.dumps({"experiment_name": experiment_name, "chemicals": chemicals}))


@mcp.tool()
async def get_audit_report(session_id: str) -> str:
    """
    Get a short-lived signed URL to download the audit report PDF.

    Use after `create_audit_session` to retrieve an archivable PDF report
    containing the chemicals, compatibility matrix, risk warnings, and
    session metadata.

    Args:
        session_id: The session id returned by `create_audit_session`,
                    e.g. "DEMO-A1B2C3D4".

    Returns:
        A signed URL valid for ~5 minutes. The session must be owned by the
        API key's user (MSDS_API_KEY).
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        if not API_KEY:
            return "get_audit_report requires MSDS_API_KEY to be set."

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            res = await client.get(
                f"{API_URL}/sessions/{session_id}/report/signed-url",
                headers=_headers(),
            )
            if res.status_code == 403:
                return (
                    f"Not authorized to access session `{session_id}`. Make sure the "
                    f"session was created with the same MSDS_API_KEY."
                )
            if res.status_code == 404:
                return f"Session `{session_id}` not found."
            res.raise_for_status()
            relative = res.json()["url"]

        full_url = f"{API_URL}{relative}"
        return (
            f"**Signed report URL** (valid ~5 min):\n{full_url}\n\n"
            f"Open in a browser or `curl -O` to download the PDF."
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_audit_report", None, dur, success, error_msg,
                        _json.dumps({"session_id": session_id}))


@mcp.tool()
async def search_chemical_database(query: str) -> str:
    """
    Search the MSDS Chain database for a specific chemical.

    Returns structured information: CAS number, chemical name, NFPA ratings
    (flammability, health, reactivity), GHS classification, and whether full
    MSDS data is available.

    Use this to verify a chemical is in the database before running compatibility
    or risk checks, or to get the canonical CAS number for a chemical name.

    Args:
        query: Chemical name, synonym, or CAS number, e.g.
               "methanol", "wood alcohol", "67-56-1"
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            res = await client.get(
                f"{API_URL}/chemicals",
                params={"q": query},
                headers=_headers(),
            )
            if res.status_code == 200:
                data = res.json()
                chemicals = data if isinstance(data, list) else data.get("chemicals", [])
                if not chemicals:
                    return f'No chemicals found matching "{query}" in the MSDS Chain database.'
                lines = [f"Found {len(chemicals)} result(s) for '{query}':\n"]
                for c in chemicals[:5]:
                    name = c.get("name") or c.get("chemical_name", "Unknown")
                    cas = c.get("cas_number", "—")
                    flam = c.get("flammability", "—")
                    tox = c.get("toxicity", "—")
                    lines.append(
                        f"• **{name}** (CAS: {cas})\n"
                        f"  Flammability: {flam}  |  Toxicity: {tox}"
                    )
                return "\n".join(lines)
            # Fallback: use quick-chat for the search
            data = await _quick_chat(f"Search for chemical: {query}")
            return data["answer"]
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("search_chemical_database", [query], dur, success, error_msg,
                        _json.dumps({"query": query}))


@mcp.tool()
async def get_waste_disposal(chemicals: list[str]) -> str:
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
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        chem_list = ", ".join(chemicals)
        message = (
            f"What is the proper waste disposal procedure for: {chem_list}? "
            "For each chemical, specify: waste classification category "
            "(halogenated organic / non-halogenated organic / acidic / alkaline / "
            "heavy metal / oxidizing / reactive / biological), "
            "container type, labeling requirements, and which waste streams "
            "they must NOT be combined with. Also note any special disposal "
            "requirements from SDS Section 13."
        )
        data = await _quick_chat(message)
        return data["answer"] + _format_tool_results(data.get("tool_results", []))
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_waste_disposal", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool()
async def batch_safety_check(chemicals: list[str]) -> str:
    """
    Run a comprehensive safety check on a list of chemicals in one call.

    Returns a combined report with:
    - Pairwise compatibility matrix (compatible/caution/incompatible)
    - PPE requirements (merged across all chemicals)
    - Storage grouping recommendations (which chemicals can share a cabinet)
    - Key risk warnings

    This is the recommended first call when reviewing an experiment protocol
    or Opentrons deck layout — it gives a complete safety picture without
    needing to call multiple tools separately.

    Args:
        chemicals: List of chemical names or CAS numbers (2-20 items), e.g.
                   ["acetone", "sulfuric acid", "sodium hydroxide", "methanol"]
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        if len(chemicals) < 2:
            return "Please provide at least 2 chemicals for a batch safety check."
        if len(chemicals) > 20:
            return "Maximum 20 chemicals per batch check. Please split into smaller groups."

        chem_list = ", ".join(chemicals)

        # Run three queries in parallel for speed
        import asyncio

        async def _compat():
            return await _quick_chat(
                f"Check compatibility for these chemicals used together: {chem_list}"
            )

        async def _ppe():
            return await _quick_chat(
                f"What PPE is required when handling all of these chemicals together: {chem_list}? "
                "Give a consolidated recommendation covering the most protective requirements."
            )

        async def _storage():
            return await _quick_chat(
                f"How should I organize storage for these chemicals: {chem_list}? "
                "Group them by storage class and indicate which ones must be separated."
            )

        compat_data, ppe_data, storage_data = await asyncio.gather(
            _compat(), _ppe(), _storage()
        )

        # Assemble report
        sections = []

        sections.append("# Batch Safety Report")
        sections.append(f"**Chemicals ({len(chemicals)}):** {chem_list}\n")

        sections.append("## 1. Compatibility Matrix")
        sections.append(compat_data["answer"])
        if compat_data.get("tool_results"):
            sections.append(_format_tool_results(compat_data["tool_results"]))

        sections.append("\n## 2. PPE Requirements (Consolidated)")
        sections.append(ppe_data["answer"])

        sections.append("\n## 3. Storage Grouping")
        sections.append(storage_data["answer"])

        sections.append(
            "\n---\n*Use `create_audit_session` if you need a signed PDF report for compliance records.*"
        )

        return "\n".join(sections)
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("batch_safety_check", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not API_KEY:
        import sys
        print(
            "Warning: MSDS_API_KEY not set. "
            "Set it via environment variable: export MSDS_API_KEY=sk-msds-...",
            file=sys.stderr,
        )
    mcp.run()
