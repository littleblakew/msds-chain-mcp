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
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from request_identity import caller_headers, get_caller_credential, set_caller_credential

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("MSDS_API_KEY", "")
API_URL = os.environ.get(
    "MSDS_API_URL",
    "https://msds-chain-backend-prod.wonderfulgrass-f1545190.southeastasia.azurecontainerapps.io",
).rstrip("/")
LANG = os.environ.get("MSDS_LANG", "en")  # en | zh | ja | de | id
TIMEOUT = 15.0       # v2 direct endpoints — fast, no LLM
TIMEOUT_LLM = 45.0   # quick-chat endpoints — LLM reasoning, needs more time

mcp = FastMCP(
    "MSDS Chain",
    host="0.0.0.0",
    port=8080,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
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


_API_KEY_REQUIRED_MSG = (
    "⚠️ MSDS_API_KEY is required for all tools.\n\n"
    "Get a free API key (100 calls/day) at https://msdschain.lagentbot.com:\n"
    "1. Sign up / log in\n"
    "2. Go to API Keys tab\n"
    "3. Create a key\n"
    "4. Set it: export MSDS_API_KEY=sk-msds-your-key\n\n"
    "Then restart the MCP server."
)


def _require_api_key() -> str | None:
    """Return error message if no caller credential on request, None if OK."""
    if not get_caller_credential():
        return "No caller credential on request (gateway must inject identity)."
    return None


def _text_result(text: str) -> CallToolResult:
    """Wrap plain text as a CallToolResult (no structuredContent)."""
    return CallToolResult(content=[TextContent(type="text", text=text)])


def _quick_result(data: dict) -> CallToolResult:
    """Build a CallToolResult for quick_chat-backed tools.

    Preserves the human-readable answer as text content (for Claude and other
    clients) and exposes structuredContent (answer + raw tool_results) for
    clients that consume structured output (e.g. ChatGPT Apps SDK).
    """
    answer = data.get("answer", "")
    tool_results = data.get("tool_results", [])
    text = answer + _format_tool_results(tool_results)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"answer": answer, "tool_results": tool_results},
    )


def _headers() -> dict[str, str]:
    return caller_headers()


async def _quick_chat(message: str) -> dict:
    """POST /quick-chat and return the parsed response."""
    if err := _require_api_key():
        raise RuntimeError(err)
    async with httpx.AsyncClient(timeout=TIMEOUT_LLM) as client:
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
                    "api_key": get_caller_credential(),
                },
                headers=_headers(),
            )
    except Exception:
        pass  # fire-and-forget


# ---------------------------------------------------------------------------
# Direct service layer helpers (bypass LLM)
# ---------------------------------------------------------------------------

async def _direct_compat(chemicals: list[str]) -> dict:
    """POST /api/v2/compatibility/check — direct service layer, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/compatibility/check",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_risk(chemicals: list[str]) -> dict:
    """POST /api/v2/risk-warnings — direct service layer, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/risk-warnings",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_batch(chemicals: list[str]) -> dict:
    """POST /api/v2/batch-safety — combined compat + risk, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/batch-safety",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_ppe(chemicals: list[str]) -> dict:
    """POST /api/v2/ppe-recommendation — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/ppe-recommendation",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_storage(chemicals: list[str]) -> dict:
    """POST /api/v2/storage-guidance — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/storage-guidance",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_emergency(chemical: str, scenario: str) -> dict:
    """POST /api/v2/emergency-response — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/emergency-response",
            json={"chemical": chemical, "scenario": scenario, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_compliance(chemical: str, regions: list[str]) -> dict:
    """POST /api/v2/compliance — direct rule engine, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/compliance",
            json={"chemical": chemical, "regions": regions, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_exposure(chemicals: list[str], region: str | None = None) -> dict:
    """POST /api/v2/exposure-limits — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        payload: dict = {"chemicals": chemicals, "lang": LANG}
        if region:
            payload["region"] = region
        res = await client.post(
            f"{API_URL}/api/v2/exposure-limits",
            json=payload,
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_transport(chemicals: list[str]) -> dict:
    """POST /api/v2/transport-classification — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/transport-classification",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_waste(chemicals: list[str]) -> dict:
    """POST /api/v2/waste-disposal — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/waste-disposal",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_sds_section(chemical: str, section: int) -> dict:
    """POST /api/v2/sds-section — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/sds-section",
            json={"chemical": chemical, "section": section, "lang": LANG},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


async def _direct_compare_sds(chemical: str, supplier: str = "", region: str = "") -> dict:
    """POST /api/v2/compare-sds-versions — direct service layer, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/compare-sds-versions",
            json={"chemical": chemical, "supplier": supplier, "region": region},
            headers=_headers(),
        )
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    annotations=ToolAnnotations(title="Check Chemical Compatibility", readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    structured_output=False,
)
async def check_chemical_compatibility(chemicals: list[str]) -> CallToolResult:
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
            return _text_result("Please provide at least 2 chemicals to check compatibility.")

        data = await _direct_compat(chemicals)
        lines = [f"**Compatibility Check** ({len(chemicals)} chemicals)\n"]

        if data.get("unresolved"):
            lines.append(f"**Unresolved:** {', '.join(data['unresolved'])}\n")

        struct_pairs = []
        counts = {"compatible": 0, "caution": 0, "incompatible": 0}
        for pair in data.get("pairs", []):
            level = pair.get("level", "unknown").upper()
            emoji = {"COMPATIBLE": "OK", "CAUTION": "CAUTION", "INCOMPATIBLE": "DANGER"}.get(level, level)
            lines.append(
                f"- **{pair.get('chem1', '?')}** + **{pair.get('chem2', '?')}**: "
                f"[{emoji}] {pair.get('level', 'unknown')}\n"
                f"  Reason: {pair.get('reason', 'N/A')}\n"
                f"  Source: {pair.get('source', 'unknown')}"
            )
            lvl = (pair.get("level") or "unknown").lower()
            if lvl in counts:
                counts[lvl] += 1
            struct_pairs.append({
                "chemical_a": pair.get("chem1"),
                "chemical_b": pair.get("chem2"),
                "level": pair.get("level"),
                "reason": pair.get("reason"),
                "source": pair.get("source"),
            })

        if not data.get("pairs"):
            lines.append("No compatibility pairs to check (need at least 2 resolved chemicals).")

        structured = {
            "chemicals": chemicals,
            "unresolved": data.get("unresolved", []),
            "pairs": struct_pairs,
            "summary": {"total_pairs": len(struct_pairs), **counts},
        }
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=structured,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("check_chemical_compatibility", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Get Chemical Risk Warnings", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        data = await _direct_risk(chemicals)
        lines = [f"**Risk Warnings** ({len(chemicals)} chemicals)\n"]

        if data.get("unresolved"):
            lines.append(f"**Unresolved:** {', '.join(data['unresolved'])}\n")

        for w in data.get("warnings", []):
            level = w.get("level", "unknown").upper()
            lines.append(
                f"### {w.get('chemical', 'Unknown')} — {level} RISK\n"
                f"- **Description:** {w.get('description', 'N/A')}\n"
                f"- **Mitigation:** {w.get('mitigation', 'N/A')}"
            )
            if w.get("reference"):
                lines.append(f"- **Reference:** {w['reference']}")

        if not data.get("warnings"):
            lines.append("No risk warnings found for the given chemicals.")

        structured = {
            "chemicals": chemicals,
            "unresolved": data.get("unresolved", []),
            "warnings": [
                {
                    "chemical": w.get("chemical"),
                    "level": w.get("level"),
                    "description": w.get("description"),
                    "mitigation": w.get("mitigation"),
                    "reference": w.get("reference"),
                }
                for w in data.get("warnings", [])
            ],
        }
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=structured,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_chemical_risk_warnings", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Check Regulatory Compliance", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        effective_regions = regions or ["EU", "US"]
        lines = ["**Regulatory Compliance**\n"]
        results = []
        for chemical in chemicals:
            data = await _direct_compliance(chemical, effective_regions)
            results.append(data)
            if data.get("unresolved"):
                lines.append(f"### {chemical}\n- **Status:** Not found in database\n")
                continue
            lines.append(f"### {data.get('chemical', chemical)} (CAS: {data.get('cas', 'N/A')})")
            lines.append(f"- **Overall compliance level:** {data.get('summary_level', 'unknown')}")
            for rr in data.get("region_results", []):
                lines.append(f"- **{rr.get('region', '?')}:** {rr.get('status', 'unknown')}")
                for flag in rr.get("flags", []):
                    lines.append(f"  - {flag}")
            lines.append("")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent={
                "chemicals": chemicals,
                "regions": effective_regions,
                "results": results,
            },
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("check_regulatory_compliance", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals, "regions": regions}))


@mcp.tool(annotations=ToolAnnotations(title="Ask Chemical Safety Question", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        return _quick_result(data)
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("ask_chemical_safety", None, dur, success, error_msg,
                        _json.dumps({"question": question}))


@mcp.tool(annotations=ToolAnnotations(title="Get PPE Recommendation", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        data = await _direct_ppe(chemicals)
        lines = ["**PPE Recommendations**\n"]
        for item in data.get("results", []):
            lines.append(f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})")
            lines.append(f"- Signal word: **{item.get('signal_word', 'N/A')}**")
            lines.append(f"- Minimum PPE level: **{item.get('minimum_ppe_level', 'N/A')}**")
            ppe = item.get("ppe", {})
            for category, recs in ppe.items():
                if isinstance(recs, list):
                    lines.append(f"- **{category.title()}:** {', '.join(str(r) for r in recs)}")
                else:
                    lines.append(f"- **{category.title()}:** {recs}")
            lines.append("")
        if data.get("unresolved"):
            lines.append(f"**Unresolved:** {', '.join(data['unresolved'])}")
        if not data.get("results"):
            lines.append("No PPE data found for the given chemicals.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=data,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_ppe_recommendation", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Get Storage Guidance", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        data = await _direct_storage(chemicals)
        lines = ["**Storage Guidance**\n"]
        for item in data.get("results", []):
            lines.append(f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})")
            lines.append(f"- **Storage class:** {item.get('storage_class_label', 'N/A')}")
            lines.append(f"- **Cabinet color:** {item.get('cabinet_color', 'N/A')}")
            lines.append(f"- **Recommended cabinet:** {item.get('recommended_cabinet', 'N/A')}")
            lines.append(f"- **Temperature:** {item.get('temperature_requirement', 'N/A')}")
            reqs = item.get("storage_requirements", [])
            if reqs:
                lines.append("- **Storage requirements:** " + "; ".join(str(r) for r in reqs))
            incompatible = item.get("incompatible_materials", [])
            if incompatible:
                lines.append("- **Incompatible materials:** " + ", ".join(str(m) for m in incompatible))
            nfpa = item.get("nfpa_ratings", {})
            if nfpa:
                lines.append("- **NFPA ratings:** " + ", ".join(f"{k.title()} {v}" for k, v in nfpa.items()))
            lines.append("")
        if data.get("unresolved"):
            lines.append(f"**Unresolved:** {', '.join(data['unresolved'])}")
        if not data.get("results"):
            lines.append("No storage data found for the given chemicals.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=data,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_storage_guidance", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Get Emergency Response", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        data = await _direct_emergency(chemical, scenario)
        if data.get("error"):
            return _text_result(f"Emergency response error: {data['error']}")
        chem_display = data.get("chemical", chemical)
        cas = data.get("cas", "N/A")
        lines = [f"**Emergency Response: {scenario.title()} — {chem_display} ({cas})**\n"]
        if data.get("signal_word"):
            lines.append(f"Signal word: **{data['signal_word']}**\n")
        immediate = data.get("immediate_actions", [])
        if immediate:
            lines.append("**Immediate Actions:**")
            lines.extend(f"  - {a}" for a in immediate)
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
        lines.append(f"*Data source: {data.get('data_source', 'unknown')}*")
        if data.get("unresolved"):
            lines.append("\n**Note:** Chemical not found in database — showing general guidance only.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=data,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_emergency_response", [chemical], dur, success, error_msg,
                        _json.dumps({"chemical": chemical, "scenario": scenario}))


@mcp.tool(annotations=ToolAnnotations(title="Get Exposure Limits", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        data = await _direct_exposure(chemicals, region)
        lines = ["**Occupational Exposure Limits**\n"]
        for item in data.get("results", []):
            lines.append(f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})")
            if item.get("region_filter"):
                lines.append(f"Region filter: **{item['region_filter']}**")
            limits = item.get("limits", [])
            if limits:
                for lim in limits:
                    source = lim.get("source") or lim.get("authority") or "?"
                    ltype = lim.get("type", "?")
                    value = lim.get("value", "—")
                    unit = lim.get("unit", "")
                    region = lim.get("region", "")
                    region_suffix = f" ({region})" if region else ""
                    lines.append(
                        f"- **{source}**{region_suffix}: {ltype} = {value} {unit}".rstrip()
                    )
            else:
                lines.append("- No OEL data found for this chemical.")
            lines.append(f"*Data source: {item.get('data_source', 'unknown')}*\n")
        if data.get("unresolved"):
            lines.append(f"**Unresolved:** {', '.join(data['unresolved'])}")
        if not data.get("results"):
            lines.append("No exposure-limit data found for the given chemicals.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=data,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_exposure_limits", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals, "region": region}))


@mcp.tool(annotations=ToolAnnotations(title="Get Transport Classification", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        data = await _direct_transport(chemicals)
        lines = ["**UN Transport Classification**\n"]
        for item in data.get("results", []):
            lines.append(f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})")
            lines.append(f"- **UN Number:** {item.get('un_number', 'N/A')}")
            lines.append(f"- **Proper Shipping Name:** {item.get('proper_shipping_name', 'N/A')}")
            lines.append(f"- **Hazard Class:** {item.get('hazard_class', 'N/A')}")
            lines.append(f"- **Packing Group:** {item.get('packing_group', 'N/A')}")
            modes = item.get("transport_modes", {})
            if modes:
                lines.append("- **Transport Modes:**")
                lines.extend(f"  - {mode.upper()}: {details}" for mode, details in modes.items())
            lines.append(f"*Data source: {item.get('data_source', 'unknown')}*\n")
        if data.get("unresolved"):
            lines.append(f"**Unresolved:** {', '.join(data['unresolved'])}")
        if not data.get("results"):
            lines.append("No transport-classification data found for the given chemicals.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=data,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_transport_classification", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Create Audit Session", readOnlyHint=False, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        if not get_caller_credential():
            return (
                "create_audit_session requires an authenticated API key so the session "
                "is tied to your account. Get one at https://msdschain.lagentbot.com "
                "(API Keys tab); self-hosted stdio sets it via MSDS_API_KEY, remote "
                "callers authenticate through the gateway."
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
        structured = {
            "session_id": session_id,
            "experiment_name": experiment_name,
            "chemicals_added": added_names,
            "not_found": not_found,
            "compatibility": {"total_pairs": len(matrix), **counts},
            "flagged_pairs": [
                {
                    "level": p.get("level"),
                    "chemical_a": p.get("chem1"),
                    "chemical_b": p.get("chem2"),
                    "reason": p.get("reason"),
                }
                for p in matrix if p.get("level") in ("caution", "incompatible")
            ],
            "warnings": [
                {"level": w.get("level"), "chemical": w.get("chemical"), "description": w.get("description")}
                for w in warnings
            ],
        }
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=structured,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("create_audit_session", chemicals, dur, success, error_msg,
                        _json.dumps({"experiment_name": experiment_name, "chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Get Audit Report", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        if not get_caller_credential():
            return "get_audit_report requires an authenticated API key (MSDS_API_KEY for stdio, or gateway auth for remote)."

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
        return CallToolResult(
            content=[TextContent(type="text", text=(
                f"**Signed report URL** (valid ~5 min):\n{full_url}\n\n"
                f"Open in a browser or `curl -O` to download the PDF."
            ))],
            structuredContent={
                "session_id": session_id,
                "report_url": full_url,
                "expires_in_seconds": 300,
            },
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_audit_report", None, dur, success, error_msg,
                        _json.dumps({"session_id": session_id}))


@mcp.tool(annotations=ToolAnnotations(title="Search Chemical Database", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        if err := _require_api_key():
            return err
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
                struct_results = []
                for c in chemicals[:5]:
                    name = c.get("name") or c.get("chemical_name", "Unknown")
                    cas = c.get("cas_number", "—")
                    flam = c.get("flammability", "—")
                    tox = c.get("toxicity", "—")
                    lines.append(
                        f"• **{name}** (CAS: {cas})\n"
                        f"  Flammability: {flam}  |  Toxicity: {tox}"
                    )
                    struct_results.append({
                        "name": name,
                        "cas_number": c.get("cas_number"),
                        "flammability": c.get("flammability"),
                        "toxicity": c.get("toxicity"),
                    })
                return CallToolResult(
                    content=[TextContent(type="text", text="\n".join(lines))],
                    structuredContent={
                        "query": query,
                        "result_count": len(chemicals),
                        "results": struct_results,
                    },
                )
            return f"Chemical search failed (HTTP {res.status_code}). Try a different name or CAS number."
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("search_chemical_database", [query], dur, success, error_msg,
                        _json.dumps({"query": query}))


@mcp.tool(annotations=ToolAnnotations(title="Get SDS Section", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
async def get_sds_section(chemical: str, section: int) -> str:
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
    t0 = time.monotonic()
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
        data = await _direct_sds_section(chemical, section)
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
            lines.append("**Note:** Chemical not found in database.")
        elif content:
            lines.append(content)
        else:
            lines.append("No data available for this section in the canonical SDS.")
        lines.append(f"\n*Data source: {data.get('data_source', 'unknown')}*")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=data,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_sds_section", [chemical], dur, success, error_msg,
                        _json.dumps({"chemical": chemical, "section": section}))


@mcp.tool(annotations=ToolAnnotations(title="Get Chemical Alternatives", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
async def get_chemical_alternatives(chemical: str, use_case: str = "") -> str:
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
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        ctx = f" It is being used as: {use_case}." if use_case else ""
        message = (
            f"Suggest 2-4 safer alternatives to {chemical}.{ctx} "
            "For each alternative, provide: chemical name, CAS number, "
            "why it's safer (specific hazard reduction), any trade-offs "
            "(performance, cost, availability), and whether the original is "
            "restricted under any regulation (REACH SVHC, TSCA, etc.). "
            "Focus on drop-in replacements that serve the same function."
        )
        data = await _quick_chat(message)
        return _quick_result(data)
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_chemical_alternatives", [chemical], dur, success, error_msg,
                        _json.dumps({"chemical": chemical, "use_case": use_case}))


@mcp.tool(annotations=ToolAnnotations(title="Validate Protocol Chemicals", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
async def validate_protocol_chemicals(protocol_text: str) -> str:
    """
    Extract and validate chemical names from a protocol or experiment description.

    Parses free-text or code (e.g., Opentrons Python protocol, lab notebook entry,
    SOP paragraph) to identify all mentioned chemicals, then checks each against
    the MSDS Chain database.

    Returns a structured list with: chemical name as mentioned, canonical name,
    CAS number (if found), and whether full safety data is available.

    Use this as the FIRST step before calling batch_safety_check or
    check_chemical_compatibility — it saves the user from manually listing chemicals.

    Args:
        protocol_text: Any text containing chemical names — can be a Python script,
                       a natural language protocol description, or a reagent list.
                       Maximum ~4000 characters.
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        if len(protocol_text) > 4000:
            protocol_text = protocol_text[:4000] + "\n[...truncated]"

        message = (
            "Extract ALL chemical names, reagents, and solvents from the following "
            "text. For each one, look it up in our database and report:\n"
            "- Name as mentioned in the text\n"
            "- Canonical name (if different)\n"
            "- CAS number (if found)\n"
            "- Whether we have safety data for it (yes/no)\n\n"
            "If a name is ambiguous, note the ambiguity.\n\n"
            f"Text to analyze:\n```\n{protocol_text}\n```"
        )
        data = await _quick_chat(message)
        return _quick_result(data)
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("validate_protocol_chemicals", None, dur, success, error_msg,
                        _json.dumps({"protocol_text_length": len(protocol_text)}))


@mcp.tool(annotations=ToolAnnotations(title="Check Mixing Order", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
async def check_mixing_order(chemical_a: str, chemical_b: str, context: str = "") -> str:
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
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        ctx = f" Context: {context}." if context else ""
        message = (
            f"What is the safe order for mixing {chemical_a} and {chemical_b}?{ctx} "
            "Specify: (1) the RECOMMENDED addition order and why, "
            "(2) the DANGEROUS order to avoid and what happens if done wrong, "
            "(3) required precautions (cooling, addition rate, stirring, inert atmosphere). "
            "If order doesn't matter for this pair, say so explicitly."
        )
        data = await _quick_chat(message)
        return _quick_result(data)
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("check_mixing_order", [chemical_a, chemical_b], dur, success, error_msg,
                        _json.dumps({"chemical_a": chemical_a, "chemical_b": chemical_b, "context": context}))


@mcp.tool(annotations=ToolAnnotations(title="Get Waste Disposal Guidance", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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
        data = await _direct_waste(chemicals)
        lines = ["**Waste Disposal Guidance**\n"]
        for item in data.get("results", []):
            lines.append(f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})")
            lines.append(f"- **Waste classification:** {item.get('waste_classification', 'N/A')}")
            sds_13 = item.get("sds_section_13")
            if sds_13:
                lines.append(f"- **SDS Section 13 (Disposal Considerations):** {sds_13[:600]}")
            lines.append(f"*Data source: {item.get('data_source', 'unknown')}*\n")
        if data.get("unresolved"):
            lines.append(f"**Unresolved:** {', '.join(data['unresolved'])}")
        if not data.get("results"):
            lines.append("No waste-disposal data found for the given chemicals.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=data,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_waste_disposal", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(
    annotations=ToolAnnotations(title="Compare SDS Versions", readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    structured_output=False,
)
async def compare_sds_versions(
    chemical: str,
    supplier: str = "",
    region: str = "",
) -> CallToolResult:
    """
    Compare a chemical's two most recent SDS versions and report whether its
    hazard data changed (and whether the change is relevant to safety verdicts).

    Identifies H-code additions/removals between the latest two on-record SDS
    revisions, and flags whether the change could affect a prior compatibility
    or risk conclusion.

    Use when a user asks if a chemical's safety data has been updated, or to
    check whether a past safety conclusion might be affected by an SDS revision.

    Args:
        chemical: Chemical name or CAS number, e.g. "hydrogen peroxide" or "7722-84-1".
        supplier: Optional SDS supplier/manufacturer to disambiguate (e.g. "Sigma-Aldrich").
        region:   Optional region code to narrow the lookup (e.g. "US", "EU", "JP", "CN").
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        data = await _direct_compare_sds(chemical, supplier, region)
        if not data.get("has_newer"):
            if data.get("cas"):
                text = (
                    f"**{chemical}** (CAS {data['cas']}): no newer SDS version found"
                    " — current version is the latest on record."
                )
            else:
                text = f"Could not resolve **{chemical}** to a known chemical."
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                structuredContent=data,
            )
        lines = [
            f"**SDS Version Comparison — {chemical}** (CAS {data.get('cas', '?')})",
            f"Version {data.get('from_version')} → {data.get('to_version')}",
        ]
        for ch in data.get("hazard_changes", []):
            if ch.get("added"):
                lines.append(f"- Added hazard codes: {', '.join(ch['added'])}")
            if ch.get("removed"):
                lines.append(f"- Removed hazard codes: {', '.join(ch['removed'])}")
        lines.append(
            f"\n**Verdict-relevant change:** "
            f"{'YES — re-review recommended' if data.get('verdict_relevant') else 'no'}"
        )
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=data,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("compare_sds_versions", [chemical], dur, success, error_msg,
                        _json.dumps({"chemical": chemical, "supplier": supplier, "region": region}))


@mcp.tool(annotations=ToolAnnotations(title="Upload & Parse MSDS PDF", readOnlyHint=False, destructiveHint=False, openWorldHint=False), structured_output=False)
async def upload_msds_pdf(
    pdf_source: str,
    session_id: str | None = None,
    experiment_name: str = "MCP Upload",
) -> str:
    """
    Upload an MSDS/SDS PDF file to MSDS Chain and get AI-parsed safety data.

    Parses the PDF with GPT-4o-mini to extract: chemical name, CAS number,
    GHS hazard classification, NFPA ratings, flash point, LD50, H-codes,
    PPE requirements, storage conditions, incompatibilities, and safety rules.

    If no session_id is provided, a new audit session is automatically created
    and its ID is returned so you can call `get_audit_report` later.

    Requires MSDS_API_KEY — the parsed data is stored under your account.

    Args:
        pdf_source:      Either a local file path (e.g. "/tmp/acetone_sds.pdf")
                         or an HTTPS URL pointing to a publicly accessible PDF.
        session_id:      Existing session ID to attach this upload to. If omitted,
                         a new session is created automatically.
        experiment_name: Label for the auto-created session (ignored if
                         session_id is provided). Defaults to "MCP Upload".

    Returns:
        Parsed chemical info (name, CAS, risk level, key fields) and session_id.
        If parsing partially failed, missing fields are listed so you can follow
        up with `ask_chemical_safety` for the gaps.
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        if not get_caller_credential():
            return (
                "upload_msds_pdf requires an authenticated API key so the record "
                "is stored under your account. Get one at https://msdschain.lagentbot.com "
                "(API Keys tab); self-hosted stdio sets it via MSDS_API_KEY, remote "
                "callers authenticate through the gateway."
            )

        # 1. Resolve PDF bytes
        import os as _os
        pdf_bytes: bytes
        filename: str

        if pdf_source.startswith("http://") or pdf_source.startswith("https://"):
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as dl:
                resp = await dl.get(pdf_source)
                resp.raise_for_status()
                pdf_bytes = resp.content
                # Derive filename from URL path
                url_path = pdf_source.rstrip("/").split("?")[0]
                filename = url_path.split("/")[-1] or "upload.pdf"
                if not filename.lower().endswith(".pdf"):
                    filename += ".pdf"
        else:
            path = _os.path.expanduser(pdf_source)
            if not _os.path.isfile(path):
                return f"File not found: {pdf_source}"
            with open(path, "rb") as f:
                pdf_bytes = f.read()
            filename = _os.path.basename(path)

        if not pdf_bytes:
            return "Could not read PDF content — file is empty."

        # 2. Ensure session exists
        sid = session_id
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            if not sid:
                res = await client.post(
                    f"{API_URL}/sessions",
                    json={"experiment_name": experiment_name, "source": "mcp"},
                    headers=_headers(),
                )
                res.raise_for_status()
                sid = res.json()["session_id"]

            # 3. Upload PDF (multipart)
            upload_headers = {k: v for k, v in _headers().items() if k != "Content-Type"}
            res = await client.post(
                f"{API_URL}/sessions/{sid}/upload",
                files={"file": (filename, pdf_bytes, "application/pdf")},
                headers=upload_headers,
                timeout=60.0,
            )
            res.raise_for_status()
            upload_data = res.json()

        results = upload_data.get("results", [])
        summary = upload_data.get("summary", {})

        if not results:
            return (
                f"Upload succeeded but no files were parsed.\n"
                f"Summary: {summary}\n"
                f"Session: `{sid}`"
            )

        lines = [f"**Session:** `{sid}`", f"**File:** {filename}", ""]

        for r in results:
            status = r.get("status", "unknown")
            chem = r.get("chemical_name") or "Unknown"
            cas = r.get("cas_number") or "—"
            risk = r.get("risk_level") or "—"
            fields = r.get("fields") or {}
            missing = r.get("missing") or []

            status_icon = {"success": "✅", "warning": "⚠️", "failed": "❌"}.get(status, "❓")
            lines.append(f"{status_icon} **{chem}** (CAS: {cas})")
            lines.append(f"   Risk level: {risk}")

            if fields:
                field_parts = []
                for k in ("state", "flammability", "corrosivity", "toxicity", "temp_limit", "protection"):
                    v = fields.get(k)
                    if v:
                        field_parts.append(f"{k}={v}")
                if field_parts:
                    lines.append(f"   Fields: {', '.join(field_parts)}")

            safety_rules = r.get("safety_rules") or []
            if safety_rules:
                lines.append(f"   Safety rules extracted: {len(safety_rules)}")

            if missing:
                lines.append(f"   Missing fields: {', '.join(missing)}")
                lines.append(
                    f"   → Use `ask_chemical_safety(\"{chem} {', '.join(missing)}\")` to fill gaps."
                )

            fail_reason = r.get("fail_reason")
            if fail_reason:
                lines.append(f"   Reason: {fail_reason}")

        lines.append("")
        lines.append(
            f"**Summary:** {summary.get('success', 0)} success, "
            f"{summary.get('warning', 0)} warning, "
            f"{summary.get('failed', 0)} failed"
        )
        lines.append(
            f"\nCall `create_audit_session(\"{experiment_name}\", [...])` or "
            f"`get_audit_report(\"{sid}\")` to generate a signed PDF report."
        )
        structured = {
            "session_id": sid,
            "file": filename,
            "summary": summary,
            "results": [
                {
                    "status": r.get("status"),
                    "chemical_name": r.get("chemical_name"),
                    "cas_number": r.get("cas_number"),
                    "risk_level": r.get("risk_level"),
                    "missing": r.get("missing") or [],
                    "fail_reason": r.get("fail_reason"),
                }
                for r in results
            ],
        }
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=structured,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("upload_msds_pdf", None, dur, success, error_msg,
                        _json.dumps({"pdf_source": pdf_source, "session_id": session_id}))


@mcp.tool(annotations=ToolAnnotations(title="Batch Safety Check", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
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

        data = await _direct_batch(chemicals)
        sections = []

        sections.append("# Batch Safety Report")
        chem_list = ", ".join(chemicals)
        sections.append(f"**Chemicals ({len(chemicals)}):** {chem_list}\n")

        if data.get("unresolved"):
            sections.append(f"**Unresolved:** {', '.join(data['unresolved'])}\n")

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
        for pair in compat.get("pairs", []):
            level = pair.get("level", "unknown").upper()
            sections.append(
                f"- **{pair.get('chem1', '?')}** + **{pair.get('chem2', '?')}**: "
                f"{level} — {pair.get('reason', 'N/A')}"
            )

        # Risk warnings
        sections.append("\n## 2. Risk Warnings")
        for w in data.get("risk_warnings", []):
            sections.append(
                f"### {w.get('chemical', 'Unknown')} — {w.get('level', 'unknown').upper()} RISK\n"
                f"- {w.get('description', 'N/A')}\n"
                f"- Mitigation: {w.get('mitigation', 'N/A')}"
            )

        if not data.get("risk_warnings"):
            sections.append("No risk data available.")

        sections.append(
            "\n---\n*Use `create_audit_session` if you need a signed PDF report for compliance records.*"
        )

        structured = {
            "chemicals": chemicals,
            "unresolved": data.get("unresolved", []),
            "compatibility": {
                "summary": compat.get("summary", {}),
                "pairs": [
                    {
                        "chemical_a": p.get("chem1"),
                        "chemical_b": p.get("chem2"),
                        "level": p.get("level"),
                        "reason": p.get("reason"),
                    }
                    for p in compat.get("pairs", [])
                ],
            },
            "risk_warnings": [
                {
                    "chemical": w.get("chemical"),
                    "level": w.get("level"),
                    "description": w.get("description"),
                    "mitigation": w.get("mitigation"),
                }
                for w in data.get("risk_warnings", [])
            ],
        }
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(sections))],
            structuredContent=structured,
        )
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("batch_safety_check", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Check Regulatory Lists", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
async def check_regulatory_lists(chemical: str) -> str:
    """
    Check which international regulatory lists a chemical appears on.

    Searches across 15+ regulatory databases including:
    - US: EPA TSCA, OSHA PEL, California Prop 65, CompTox
    - EU: SVHC Candidate List, REACH Annex XVII, CLP, Seveso III
    - APAC: China Hazardous Chemicals, Japan CSCL, Australia AIIC, Singapore EPMA
    - Americas: Canada DSL

    Returns a summary of all matching lists, helping you understand
    a chemical's global regulatory footprint at a glance.

    Args:
        chemical: Chemical name or CAS number
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        message = (
            f"Check which regulatory lists {chemical} appears on. "
            "Use the check_regulatory_lists tool and report all matching lists."
        )
        data = await _quick_chat(message)
        return _quick_result(data)
    except Exception as e:
        success = False
        error_msg = str(e)[:500]
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("check_regulatory_lists", [chemical], dur, success, error_msg,
                        _json.dumps({"chemical": chemical}))


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
    else:
        # Local / stdio mode: seed the contextvar from the env key so that
        # caller_headers() returns the correct credential without a gateway.
        set_caller_credential(API_KEY)
    mcp.run()
