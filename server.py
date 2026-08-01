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

import functools
import json
import json as _json
import logging
import os
import textwrap
import time

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from request_identity import caller_headers, get_caller_credential, set_caller_credential

# Writes to stderr only (never stdout — stdout is the JSON-RPC channel for the
# stdio transport, see module docstring). Container Apps captures stderr into
# Log Analytics, so this is queryable in prod without any extra infra (CI-248).
logger = logging.getLogger("msds_mcp")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("MSDS_API_KEY", "")
API_URL = os.environ.get(
    "MSDS_API_URL",
    "https://msds-chain-backend-prod.orangepond-4b408d49.southeastasia.azurecontainerapps.io",
).rstrip("/")
LANG = os.environ.get("MSDS_LANG", "en")  # en | zh | ja | de | id
TIMEOUT = 15.0        # single-chemical / pure-lookup v2 endpoints — fast, no LLM
# ---------------------------------------------------------------------------
# TIMEOUT_MULTI — the budget for every v2 endpoint that takes `chemicals: list`.
#
# WHY these tools need more than 15s (and the single-chemical ones do not):
# work on the backend scales with the number of components. Compatibility /
# batch-safety additionally fall back to a serial LLM escalation call per
# uncategorized pair (asymmetric-trust gate in check_compatibility_pair — the
# rule engine is non-committal AND at least one CAS is uncategorized), capped at
# MAX_LLM_FALLBACK_PAIRS (=12) serial ~1-3s Azure OpenAI round-trips. The other
# multi-component endpoints do per-component SDS resolution (alias → CAS →
# authoritative record), which is DB-bound but still linear in component count.
# A single-chemical lookup does one resolution and is flat.
#
# Prod evidence (mcp_call_logs, all-time through 2026-07-26) — "hit 15s" means
# duration_ms ≈ 15,0xx, i.e. pinned to this client ceiling, NOT a backend 5xx:
#   get_chemical_risk_warnings      6/25 hit 15s (24%)  max 15,027  p90 15,024
#   get_storage_guidance            1/7  hit 15s        max 15,022  p90  9,303
#   get_transport_classification    1/2  hit 15s        max 15,018  p90 13,857
#   batch_safety_check (was 45s)   27/38 ≥14.5s         max 45,026  p90 31,251
# vs. the single-chemical / lookup tools, which are nowhere near the ceiling:
#   search_chemical_database        0/41 hit 15s        max  8,800  p90  5,696
#   get_sds_section                 0/39 hit 15s        max  1,499  p90    372
# CI-176: a real user (2nd-deepest by call volume, credits to spare) hit the
# 15s wall twice on get_chemical_risk_warnings for a 5-component excipient
# formulation and never came back — a product failure, not a quota failure.
#
# ∴ raise ONLY the multi-component tools. Deliberately NOT raised for
# single-chemical/lookup tools (_direct_sds_section, _direct_sds_document,
# _direct_compare_sds, _direct_online_search, _direct_emergency): their p90 is
# <1.5s, so a longer budget cannot turn a failure into a success — it can only
# make a genuinely broken call spin longer before failing, which is a worse
# experience, not a better one.
#
# 🔴 `_direct_compliance` also stays at 15s but for the OPPOSITE reason — it is
# NOT fast. check_regulatory_compliance's Prod p90 is 23.1s and 2 of 4 calls
# exceeded 14.5s. It stays short because the TOOL invokes this helper in a
# SEQUENTIAL LOOP, once per chemical, so the per-item budget MULTIPLIES:
# 3 chemicals × 45s = 135s, far past any client ceiling. Raising it here makes
# the tail worse, not better. The real fix is the loop itself (parallelise, or
# cap the chemical count and say so in the response) and belongs in the tool —
# same class as the batch_safety_check O(n²) tail.
# 45s stays well under the Container App ingress ~256s request timeout and
# under TIMEOUT_LLM (this is NOT the multi-turn quick-chat path).
# ---------------------------------------------------------------------------
TIMEOUT_MULTI = 45.0  # every v2 endpoint taking `chemicals: list`
# quick-chat runs up to 3 sequential gpt-5-mini turns (RAI → intent → summary); a
# single reasoning summary legitimately takes 30-60s and an unlisted chemical was
# measured end-to-end at ~55.7s on Prod. 45s cut those off mid-flight → httpx
# ReadTimeout (empty str) → opaque tool error that discarded a valid answer. 120s
# clears the realistic slow case with headroom while staying under the backend's own
# per-turn budget cap and the Container App ingress ~256s request timeout.
TIMEOUT_LLM = 120.0   # quick-chat endpoints — multi-turn LLM reasoning

# Single source of truth = the repo-root VERSION file. This literal is kept in
# sync by scripts/release.sh (which stamps VERSION into every manifest), and
# tests/test_version.py fails CI if the two ever drift. Do NOT hand-edit — bump
# VERSION and run scripts/release.sh. FastMCP.__init__ takes no `version` arg, so
# we assign it on the underlying low-level server after construction; this is what
# surfaces as serverInfo.version in the MCP `initialize` handshake (what ChatGPT,
# claude.ai and any raw MCP client display).
__version__ = "1.5.3"

mcp = FastMCP(
    "MSDS Chain",
    host="0.0.0.0",
    port=8080,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions=textwrap.dedent("""
        MSDS Chain provides chemical safety intelligence backed by traceable, sourced SDS data.

        WHICH TOOL:
        - For any general safety question — hazards, PPE, first aid, storage, disposal,
          "is X safe", "what do I need to handle Y" — call ask_chemical_safety FIRST.
          It returns ONE answer grounded in a specific supplier SDS. Do NOT chain
          search + risk-warnings + PPE for a broad question; that is slower, fragmented,
          and loses the single sourced citation.
        - Use the granular tools (get_chemical_risk_warnings, get_ppe_recommendation,
          get_storage_guidance, search_chemical_database) only when the user explicitly
          wants that one structured field, or to drill down.
        - check_chemical_compatibility for mixing/storing pairs; check_regulatory_compliance
          for multi-region legal status; get_sds_document for the original signed PDF link.

        HOW TO PRESENT (grounding — do not violate):
        - ALWAYS cite the source the tool returns (supplier + revision date). Traceability
          is the point — an answer the reader can point back to a specific document.
        - Do NOT add hazard, medical, or regulatory claims not in the tool output. If you
          add general knowledge, label it as general knowledge, not as SDS data.
        - After answering a hazard/PPE/handling question, ALSO call get_sds_document and give
          the user the signed link to the original supplier SDS PDF — closing the loop from
          "traceable citation" to "openable source document" is a core value; do it by default,
          not only when asked. If no original PDF is on file, say so and offer upload.
        - If the matched SDS is a different grade/concentration than the user's product (e.g.
          a pure-compound SDS when they use a dilute formulation), state that plainly as a
          precision point, not as a data gap — then invite them to upload their product's SDS
          via upload_msds_pdf to get an exact match (which also enriches the shared database).
          Never phrase it as "we don't have data"; frame it as "here is the authoritative
          record on file; upload your exact product to refine it."

        Use these tools when a user mentions chemicals, safety, storage, handling, or
        compliance in a lab/experimental context.
    """).strip(),
)

# Surface our product version in the MCP `initialize` handshake. Without this,
# the SDK falls back to reporting the `mcp` package version — a meaningless value
# that clients (ChatGPT, claude.ai) display as our server version.
mcp._mcp_server.version = __version__


_API_KEY_REQUIRED_MSG = (
    "⚠️ MSDS_API_KEY is required for all tools.\n\n"
    "Get a free API key (100 calls/month) at https://msdschain.lagentbot.com:\n"
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

    CI-89: if the backend returns a top-level `documents` list (blob-backed SDS
    descriptors), append an "📄 Original SDS" section to the text and include
    the list in structuredContent.
    """
    answer = data.get("answer", "")
    tool_results = data.get("tool_results", [])
    documents = data.get("documents", [])
    # CI-89-followup: the SDS document links must come RIGHT AFTER the answer, before
    # the raw tool-data appendix. Appended last (after _format_tool_results' JSON blob)
    # the client model summarizes the answer and drops the trailing link — verified on
    # prod: backend returns documents correctly, but claude.ai never surfaced the link
    # for ask_chemical_safety while the (short, link-last) direct tools did.
    text = answer + _format_sds_documents(documents) + _format_tool_results(tool_results)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"answer": answer, "tool_results": tool_results,
                           "documents": documents},
    )


def _format_sds_documents(documents: list[dict]) -> str:
    """Render a `documents` list as an '📄 Original SDS' section.

    Each document: {chemical, chemical_name, cas, supplier, revision_date, region,
    record_id, sds_document_url}.  URL is output verbatim (no domain rewriting).
    Returns "" when documents is empty so callers can safely concatenate.
    """
    if not documents:
        return ""
    lines = ["\n\n---\n**📄 Original SDS (click to verify):**"]
    for doc in documents:
        chemical = doc.get("chemical_name") or doc.get("chemical") or "?"
        supplier = doc.get("supplier", "")
        revision = doc.get("revision_date", "")
        url = doc.get("sds_document_url", "")
        meta_parts = [p for p in [supplier, revision] if p]
        meta = " · ".join(meta_parts)
        entry = f"- {chemical}"
        if meta:
            entry += f" ({meta})"
        if url:
            entry += f": {url}"
        lines.append(entry)
    return "\n".join(lines)


def _doc_link_lookup(documents: list[dict]) -> dict[str, str]:
    """Build {key -> sds_document_url} keyed by chemical, chemical_name and cas
    (all casefolded) so a per-item render can find its chemical's SDS link inline.

    CI-89-inline: a trailing '📄 Original SDS' block gets summarized away by the
    client model on long answers; an inline link ON each verdict/warning line
    survives because it is part of the structured row the model preserves.
    """
    lut: dict[str, str] = {}
    for doc in documents:
        url = doc.get("sds_document_url")
        if not url:
            continue
        for k in (doc.get("chemical"), doc.get("chemical_name"), doc.get("cas")):
            if k:
                lut.setdefault(str(k).casefold(), url)
    return lut


def _inline_sds(lookup: dict[str, str], *keys: str) -> str:
    """Return a compact inline SDS-link suffix for the first matching key, else ''."""
    for k in keys:
        if k and (url := lookup.get(str(k).casefold())):
            return f" 📄 SDS: {url}"
    return ""


def _headers() -> dict[str, str]:
    return caller_headers()


# CI-55: the direct/v2 tools call fast no-LLM endpoints on a 15s client timeout.
# Backend tail-latency (cold start right after a deploy, load spikes) can still
# overrun it → httpx.ReadTimeout, which stringifies to "" → the opaque
# `Error executing tool <name>: ` dead end. Unlike the quick-chat path (missing
# data → upload the MSDS), a direct-tool timeout is transient service slowness, so
# the graceful answer is retry-oriented. Applied as a wrapper so all direct tools
# share one behavior. NEVER assert safety here.
_DIRECT_TIMEOUT_MSG = {
    "en": "This safety check timed out — the service was briefly slow (often just after a deploy). "
          "Please try again in a moment.",
    "zh": "本次安全检查超时——服务短暂变慢（常见于刚部署后）。请稍候重试。",
    "ja": "この安全チェックはタイムアウトしました。サービスが一時的に遅くなっています（デプロイ直後によく発生）。少し待ってから再度お試しください。",
    "de": "Diese Sicherheitsprüfung hat das Zeitlimit überschritten — der Dienst war kurz langsam "
          "(oft direkt nach einem Deployment). Bitte versuchen Sie es gleich erneut.",
    "id": "Pemeriksaan keselamatan ini melebihi batas waktu — layanan sempat lambat (sering terjadi "
          "tepat setelah deploy). Silakan coba lagi sebentar.",
}


def _graceful_timeout(fn):
    """Wrap a direct-tool coroutine so a client read-timeout returns an actionable
    retry message instead of raising an opaque empty error (CI-55)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except httpx.TimeoutException:
            return _DIRECT_TIMEOUT_MSG.get(LANG, _DIRECT_TIMEOUT_MSG["en"])
    return wrapper


# Actionable fallback when quick-chat exceeds TIMEOUT_LLM. httpx.ReadTimeout
# stringifies to "", so re-raising surfaced `Error executing tool …: ` — an opaque
# dead end. On timeout, guide the user to the grounded path (retry / upload the SDS /
# give a CAS) instead of raising an empty error. NEVER assert safety here.
_TIMEOUT_ANSWER = {
    "zh": "安全助手响应超时，未能在限定时间内完成分析。请稍后重试。若这是未收录或专有产品，"
          "请上传其 MSDS/SDS PDF 或提供 CAS 号，以便直接查询其危害信息。",
    "en": "The safety assistant timed out before completing its analysis. Please try again. "
          "If this is an unlisted or proprietary product, upload its MSDS/SDS PDF or provide a "
          "CAS number so its hazards can be looked up directly.",
    "ja": "安全アシスタントの応答がタイムアウトし、分析を完了できませんでした。もう一度お試しください。"
          "未登録または独自製品の場合は、MSDS/SDS PDF をアップロードするか CAS 番号をご提供ください。",
    "de": "Der Sicherheitsassistent hat vor Abschluss der Analyse das Zeitlimit überschritten. Bitte "
          "erneut versuchen. Bei einem nicht gelisteten oder proprietären Produkt laden Sie dessen "
          "MSDS/SDS-PDF hoch oder geben Sie eine CAS-Nummer an, um die Gefahren direkt nachzuschlagen.",
    "id": "Asisten keselamatan melebihi batas waktu sebelum menyelesaikan analisis. Silakan coba lagi. "
          "Jika ini produk tak terdaftar atau proprietary, unggah PDF MSDS/SDS-nya atau berikan nomor "
          "CAS agar bahayanya dapat dicari langsung.",
}


async def _quick_chat(message: str) -> dict:
    """POST /quick-chat and return the parsed response.

    On client read-timeout (a slow-but-valid backend turn that overran TIMEOUT_LLM)
    degrade to an actionable message rather than raising an opaque empty error.
    """
    if err := _require_api_key():
        raise RuntimeError(err)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_LLM) as client:
            res = await client.post(
                f"{API_URL}/quick-chat",
                json={"message": message, "lang": LANG},
                headers=_headers(),
            )
            return _billed_json(res)
    except httpx.TimeoutException:
        return {"answer": _TIMEOUT_ANSWER.get(LANG, _TIMEOUT_ANSWER["en"]),
                "tool_results": [],
                "_timed_out": True}


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


def _error_text(e: BaseException) -> str:
    """Render an exception for storage/logging — never an empty string.

    CI-250: measured on prod `mcp_call_logs`, 66% of external failed calls had
    no `error_message` at all. Root cause: several httpx exceptions (notably
    ReadTimeout / PoolTimeout and other httpx.TimeoutException subclasses)
    stringify to "" — `str(e) == ""` — and every tool's except-block did
    `error_msg = _error_text(e)` with no fallback. Duration histograms on the
    empty-message rows land exactly on TIMEOUT / TIMEOUT_MULTI (15000 / 45000
    ms), confirming this is the mechanism, not a one-off. Always prefix with
    the exception's class name so rows are groupable even when the message
    itself is empty or generic.
    """
    msg = str(e).strip()
    label = type(e).__name__
    text = f"{label}: {msg}" if msg else f"{label}: (no message)"
    return text[:500]


# Process-local counter for dropped call-log POSTs (CI-248). Not persisted —
# it exists so a single `logger.warning` line carries a running rate, not just
# an isolated one-off, without standing up separate metrics infra for a
# network-isolated core that only ships stderr → Log Analytics.
_call_log_post_failures = 0


async def _log_call(tool_name: str, chemicals: list[str] | None, duration_ms: int,
                    success: bool, error_message: str | None = None,
                    input_params: str | None = None):
    """Fire-and-forget: POST call record to backend.

    Never raises into the caller — a logging failure must not break the user's
    tool call, so the POST is still wrapped in try/except and awaited without
    blocking the tool's own response path (this coroutine is only ever awaited
    from each tool's own `finally` block, after the result is already computed).

    CI-248: previously the except-block was a bare `except Exception: pass` —
    any backend hiccup (network blip, 5xx, auth resolution failure) dropped the
    call record with literally no trace anywhere, so a low call count could
    silently be an undercount with no way to know by how much. Now every drop
    is logged to stderr (Container Apps → Log Analytics for this
    network-isolated core) with enough context to correlate: which tool, the
    caller's credential presence, why it failed, and a running per-process
    count.
    """
    global _call_log_post_failures
    cred = get_caller_credential()
    # CI-113: strip "Bearer " prefix before logging so the backend's sk-msds-
    # prefix check resolves correctly. The gateway always forwards the resolved
    # sk-msds- key via X-API-Key (no Bearer), so this only fires for direct-to-
    # core callers that set Authorization instead of X-API-Key.
    if cred and cred.startswith("Bearer "):
        cred = cred[len("Bearer "):].strip()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.post(
                f"{API_URL}/mcp/call-log",
                json={
                    "tool_name": tool_name,
                    "chemicals": chemicals,
                    "duration_ms": duration_ms,
                    "success": success,
                    "error_message": error_message,
                    "input_params": input_params,
                    "api_key": cred,
                },
                headers=_headers(),
            )
            # Previously unchecked: a non-2xx response from the logging
            # endpoint itself (e.g. validation 4xx, backend 5xx) was silently
            # accepted as "logged" since no exception was raised without this.
            res.raise_for_status()
    except Exception as e:
        _call_log_post_failures += 1
        logger.warning(
            "mcp_call_log_post_failed tool=%s call_success=%s dur_ms=%s "
            "cred_present=%s reason=%s failures_this_process=%d",
            tool_name, success, duration_ms, bool(cred), _error_text(e),
            _call_log_post_failures,
        )


# ---------------------------------------------------------------------------
# Direct service layer helpers (bypass LLM)
# ---------------------------------------------------------------------------

def _parse_usage(res: "httpx.Response") -> dict | None:
    """Read per-call credit usage the backend echoes via X-Msds-Credits-* headers.
    Returns {cost, balance, reason} or None when the call wasn't metered."""
    cost = res.headers.get("X-Msds-Credits-Cost")
    if cost is None:
        return None
    try:
        return {
            "cost": float(cost),
            "balance": float(res.headers.get("X-Msds-Credits-Balance", "-1")),
            "reason": res.headers.get("X-Msds-Credits-Reason", ""),
        }
    except (TypeError, ValueError):
        return None


def _billed_json(res: "httpx.Response") -> dict:
    """raise_for_status with a caller-friendly 402 (balance exhausted) message, then
    return the JSON body with any credit usage attached under `_usage`."""
    if res.status_code == 402:
        bal = None
        try:
            bal = (res.json().get("detail") or {}).get("balance")
        except Exception:
            pass
        msg = "Credit balance exhausted."
        if bal is not None:
            try:
                msg += f" Remaining: {float(bal):g} credits."
            except (TypeError, ValueError):
                pass
        raise RuntimeError(msg + " Top up at msdschain.lagentbot.com to continue.")
    res.raise_for_status()
    data = res.json()
    usage = _parse_usage(res)
    if usage and isinstance(data, dict):
        data["_usage"] = usage
    return data


def _usage_line(usage: dict) -> str:
    """Human-readable one-liner appended to a metered tool's text output."""
    bal = usage.get("balance", -1)
    reason = usage.get("reason", "")
    cost = usage.get("cost", 0) or 0
    if reason == "subscription" or bal < 0:
        return "\n\n---\n💳 Included in your plan (no credits deducted)."
    head = (f"This call used {cost:g} credits" if cost > 0
            else "Free lookup (0 credits)")
    return f"\n\n---\n💳 {head} · Balance: {bal:g} credits remaining."


def _with_usage(result: "CallToolResult", data: dict) -> "CallToolResult":
    """Append the credit usage line to a value tool's result (text + structuredContent).
    No-op when the backend didn't meter the call (`_usage` absent)."""
    usage = (data or {}).get("_usage")
    if not usage:
        return result
    content = list(result.content or [])
    line = _usage_line(usage)
    if content and isinstance(content[0], TextContent):
        content = [TextContent(type="text", text=content[0].text + line)] + content[1:]
    sc = result.structuredContent
    if sc is not None:
        sc = {**sc, "usage": usage}
    return CallToolResult(content=content, structuredContent=sc)


def _strip_usage(data: dict) -> dict:
    """Drop the internal `_usage` key that _billed_json attaches, so lookup tools
    that expose `structuredContent=data` don't leak it into the client output.
    (Value tools build their own structuredContent + surface a clean `usage` block.)"""
    if not isinstance(data, dict) or "_usage" not in data:
        return data
    return {k: v for k, v in data.items() if k != "_usage"}


async def _direct_compat(chemicals: list[str]) -> dict:
    """POST /api/v2/compatibility/check — direct service layer, bounded LLM fallback."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/compatibility/check",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_risk(chemicals: list[str]) -> dict:
    """POST /api/v2/risk-warnings — direct service layer, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/risk-warnings",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_batch(chemicals: list[str]) -> dict:
    """POST /api/v2/batch-safety — combined compat + risk, bounded LLM fallback."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/batch-safety",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_ppe(chemicals: list[str]) -> dict:
    """POST /api/v2/ppe-recommendation — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/ppe-recommendation",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_storage(chemicals: list[str]) -> dict:
    """POST /api/v2/storage-guidance — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/storage-guidance",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_emergency(chemical: str, scenario: str) -> dict:
    """POST /api/v2/emergency-response — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/emergency-response",
            json={"chemical": chemical, "scenario": scenario, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_compliance(chemical: str, regions: list[str]) -> dict:
    """POST /api/v2/compliance — direct rule engine, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/compliance",
            json={"chemical": chemical, "regions": regions, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_online_search(chemical_name: str = "", cas_number: str = "") -> dict:
    """POST /api/v2/online-search — stateless PubChem GHS fallback (SE-19), unmetered."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/online-search",
            json={"chemical_name": chemical_name, "cas_number": cas_number, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_exposure(chemicals: list[str], region: str | None = None) -> dict:
    """POST /api/v2/exposure-limits — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        payload: dict = {"chemicals": chemicals, "lang": LANG}
        if region:
            payload["region"] = region
        res = await client.post(
            f"{API_URL}/api/v2/exposure-limits",
            json=payload,
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_transport(chemicals: list[str]) -> dict:
    """POST /api/v2/transport-classification — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/transport-classification",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_waste(chemicals: list[str]) -> dict:
    """POST /api/v2/waste-disposal — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/waste-disposal",
            json={"chemicals": chemicals, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_sds_section(chemical: str, section: int) -> dict:
    """POST /api/v2/sds-section — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/sds-section",
            json={"chemical": chemical, "section": section, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_compare_sds(chemical: str, supplier: str = "", region: str = "") -> dict:
    """POST /api/v2/compare-sds-versions — direct service layer, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/compare-sds-versions",
            json={"chemical": chemical, "supplier": supplier, "region": region},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_sds_document(chemical: str) -> dict:
    """GET /api/v2/sds-document-url — return signed PDF URL or availability status."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.get(
            f"{API_URL}/api/v2/sds-document-url",
            params={"chemical": chemical},
            headers=_headers(),
        )
        return _billed_json(res)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    annotations=ToolAnnotations(title="Check Chemical Compatibility", readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    structured_output=False,
)
@_graceful_timeout
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

        # CI-89-inline: per-chemical link lookup so each pair row carries its own
        # SDS links (survives client-model summarization better than a trailing block).
        doc_lut = _doc_link_lookup(data.get("documents", []))
        struct_pairs = []
        counts = {"compatible": 0, "caution": 0, "incompatible": 0}
        for pair in data.get("pairs", []):
            level = pair.get("level", "unknown").upper()
            emoji = {"COMPATIBLE": "OK", "CAUTION": "CAUTION", "INCOMPATIBLE": "DANGER"}.get(level, level)
            # CI-89: compat verdicts come from a rule engine — label as Basis(rule)
            traceability = pair.get("traceability", "rule_based")
            basis_label = "Basis (rule)" if traceability == "rule_based" else "Source (SDS)"
            pair_line = (
                f"- **{pair.get('chem1', '?')}** + **{pair.get('chem2', '?')}**: "
                f"[{emoji}] {pair.get('level', 'unknown')}\n"
                f"  Reason: {pair.get('reason', 'N/A')}\n"
                f"  {basis_label}: {pair.get('source', 'unknown')}"
            )
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
            struct_pairs.append({
                "chemical_a": pair.get("chem1"),
                "chemical_b": pair.get("chem2"),
                "level": pair.get("level"),
                "reason": pair.get("reason"),
                "source": pair.get("source"),
                "traceability": traceability,
            })

        if not data.get("pairs"):
            lines.append("No compatibility pairs to check (need at least 2 resolved chemicals).")

        # CI-89: append SDS document links when backend provides them
        documents = data.get("documents", [])
        if documents:
            lines.append(_format_sds_documents(documents))

        structured = {
            "chemicals": chemicals,
            "unresolved": data.get("unresolved", []),
            "pairs": struct_pairs,
            "summary": {"total_pairs": len(struct_pairs), **counts},
            "documents": documents,
        }
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=structured,
        ), data)
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("check_chemical_compatibility", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Get Chemical Risk Warnings", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
async def get_chemical_risk_warnings(chemicals: list[str]) -> str:
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
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        data = await _direct_risk(chemicals)
        lines = [f"**Risk Warnings** ({len(chemicals)} chemicals)\n"]

        if data.get("unresolved"):
            lines.append(f"**Unresolved:** {', '.join(data['unresolved'])}\n")

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
            if traceability == "sds_backed":
                trace_label = "[Source: SDS document]"
            elif traceability == "rule_based":
                trace_label = "[Basis: rule/standard]"
            else:
                # Backend didn't provide field — infer from documents list
                chem_key = (w.get("chemical") or "").lower()
                if chem_key and chem_key in sds_backed_chemicals:
                    trace_label = "[Source: SDS document]"
                else:
                    trace_label = ""
            # CI-89-inline: SDS link on the warning line itself
            inline = _inline_sds(doc_lut, w.get("chemical"), w.get("cas"))
            lines.append(
                f"### {w.get('chemical', 'Unknown')} — {level} RISK {trace_label}{inline}\n"
                f"- **Description:** {w.get('description', 'N/A')}\n"
                f"- **Mitigation:** {w.get('mitigation', 'N/A')}"
            )
            if w.get("reference"):
                lines.append(f"- **Reference:** {w['reference']}")

        if not data.get("warnings"):
            lines.append("No risk warnings found for the given chemicals.")

        # CI-89: append SDS document links
        if documents:
            lines.append(_format_sds_documents(documents))

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
                    "traceability": w.get("traceability"),
                }
                for w in data.get("warnings", [])
            ],
            "documents": documents,
        }
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent=structured,
        ), data)
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_chemical_risk_warnings", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Check Regulatory Compliance", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
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
        for chemical in chemicals:
            data = await _direct_compliance(chemical, effective_regions)
            _u = data.pop("_usage", None)  # strip internal key from stored per-chemical result
            if _u:
                _usage_cost += _u.get("cost", 0) or 0
                _usage_bal = _u.get("balance")
                _usage_reason = _u.get("reason", "")
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
        _usage = ({"cost": _usage_cost, "balance": _usage_bal, "reason": _usage_reason}
                  if _usage_bal is not None else None)
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structuredContent={
                "chemicals": chemicals,
                "regions": effective_regions,
                "regions_defaulted": not regions,
                "results": results,
            },
        ), {"_usage": _usage})
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("check_regulatory_compliance", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals, "regions": regions}))


@mcp.tool(annotations=ToolAnnotations(title="Ask Chemical Safety Question", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
async def ask_chemical_safety(question: str) -> str:
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

    Args:
        question: Any chemical safety question, e.g.
                  "What are the main hazards and PPE for TMAH?"
                  "How should I store acetone and methanol in the same cabinet?"
                  "A worker got hydrofluoric acid on their skin — first aid?"
    """
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        data = await _quick_chat(question)
        if data.get("_timed_out"):
            success = False
            error_msg = "timeout"
        return _quick_result(data)
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("ask_chemical_safety", None, dur, success, error_msg,
                        _json.dumps({"question": question}))


@mcp.tool(annotations=ToolAnnotations(title="Get PPE Recommendation", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
async def get_ppe_recommendation(chemicals: list[str]) -> str:
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
    t0 = time.monotonic()
    error_msg = None
    success = True
    try:
        data = await _direct_ppe(chemicals)
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
            if traceability == "sds_backed":
                trace_label = "[Source: SDS document]"
            elif traceability == "rule_based":
                trace_label = "[Basis: rule/standard]"
            else:
                chem_key = (item.get("chemical_name") or "").lower()
                if chem_key and chem_key in sds_backed_chemicals:
                    trace_label = "[Source: SDS document]"
                else:
                    trace_label = ""
            header = f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})"
            if trace_label:
                header += f"  {trace_label}"
            header += _inline_sds(doc_lut, item.get("chemical_name"), item.get("cas"))  # CI-89-inline
            lines.append(header)
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
            lines.append(f"**Unresolved:** {', '.join(data['unresolved'])}")
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
            structuredContent=sc,
        )
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_ppe_recommendation", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Get Storage Guidance", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
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
            structuredContent=_strip_usage(data),
        )
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_storage_guidance", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(annotations=ToolAnnotations(title="Get Emergency Response", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
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
            structuredContent=_strip_usage(data),
        )
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_emergency_response", [chemical], dur, success, error_msg,
                        _json.dumps({"chemical": chemical, "scenario": scenario}))


@mcp.tool(annotations=ToolAnnotations(title="Get Exposure Limits", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
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
            structuredContent=_strip_usage(data),
        )
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_exposure_limits", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals, "region": region}))


@mcp.tool(annotations=ToolAnnotations(title="Get Transport Classification", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
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
            structuredContent=_strip_usage(data),
        )
    except Exception as e:
        success = False
        error_msg = _error_text(e)
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
        error_msg = _error_text(e)
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

        full_url = relative if str(relative).startswith("http") else f"{API_URL}{relative}"
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
        error_msg = _error_text(e)
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
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("search_chemical_database", [query], dur, success, error_msg,
                        _json.dumps({"query": query}))


@mcp.tool(annotations=ToolAnnotations(title="Search MSDS Online (PubChem)", readOnlyHint=True, destructiveHint=False, openWorldHint=True), structured_output=False)
@_graceful_timeout
async def search_msds_online(chemical_name: str = "", cas_number: str = "") -> "CallToolResult | str":
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
    t0 = time.monotonic()
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
            structuredContent={
                "query": chemical_name or cas_number,
                "cas_number": data.get("cas_number") or "",
                "source": "pubchem",
                "ghs": ghs,
            },
        )
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("search_msds_online", [chemical_name or cas_number], dur, success,
                        error_msg, _json.dumps({"chemical_name": chemical_name, "cas_number": cas_number}))


@mcp.tool(annotations=ToolAnnotations(title="Get SDS Section", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
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
            structuredContent=_strip_usage(data),
        )
    except Exception as e:
        success = False
        error_msg = _error_text(e)
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
        if data.get("_timed_out"):
            success = False
            error_msg = "timeout"
        return _quick_result(data)
    except Exception as e:
        success = False
        error_msg = _error_text(e)
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
        if data.get("_timed_out"):
            success = False
            error_msg = "timeout"
        return _quick_result(data)
    except Exception as e:
        success = False
        error_msg = _error_text(e)
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
        if data.get("_timed_out"):
            success = False
            error_msg = "timeout"
        return _quick_result(data)
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("check_mixing_order", [chemical_a, chemical_b], dur, success, error_msg,
                        _json.dumps({"chemical_a": chemical_a, "chemical_b": chemical_b, "context": context}))


@mcp.tool(annotations=ToolAnnotations(title="Get Waste Disposal Guidance", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
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
            structuredContent=_strip_usage(data),
        )
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_waste_disposal", chemicals, dur, success, error_msg,
                        _json.dumps({"chemicals": chemicals}))


@mcp.tool(
    annotations=ToolAnnotations(title="Compare SDS Versions", readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    structured_output=False,
)
@_graceful_timeout
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
                structuredContent=_strip_usage(data),
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
            structuredContent=_strip_usage(data),
        )
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("compare_sds_versions", [chemical], dur, success, error_msg,
                        _json.dumps({"chemical": chemical, "supplier": supplier, "region": region}))


# ---------------------------------------------------------------------------
# CI-169: upload_msds_pdf resolves `pdf_source` on the machine running THIS
# server. For the hosted core that is our container — never the caller's laptop
# and never the client's sandbox — so os.path.isfile() can only ever fail for a
# remote client. Prod evidence: our deepest user called upload_msds_pdf twice on
# 2026-07-26 (10:20, 10:21) and landed here both times — duration_ms=0, zero rows
# in demo.msds_records, and (before this fix) success=t with an empty
# error_message, so the failure was invisible to us and unexplained to him.
# CI-101 telemetry says 100% of remote MCP traffic is chatgpt.com, i.e. this was
# the entire contribution path for every remote user. The reply must therefore
# name the constraint and give a next step the caller can actually take.
# ---------------------------------------------------------------------------
def _upload_local_path_message(pdf_source: str) -> str:
    return (
        f"❌ Could not read `{pdf_source}`.\n\n"
        "This MCP server runs on MSDS Chain's servers, not on your machine, so it "
        "cannot open files on your computer or inside your chat client's sandbox. "
        "(A local file path only works for a self-hosted stdio server running on the "
        "same machine as the file.)\n\n"
        "Two ways to get this SDS in:\n"
        "1. **Public link** — if the PDF has a publicly reachable HTTPS URL (supplier "
        "site, or a share link that needs no login), call `upload_msds_pdf` again with "
        "that URL and it will be fetched and parsed right here.\n"
        "2. **Web upload** — go to https://msdschain.lagentbot.com, sign in with the "
        "same account, and upload the PDF there. This works for any local file.\n\n"
        "Either way the PDF is parsed into structured safety data (chemical name, CAS, "
        "GHS classification, H-codes, PPE, storage, incompatibilities) and stored under "
        "your account, so the other tools here can use it. Contributing an SDS we do "
        "not already hold also earns credits."
    )


@mcp.tool(annotations=ToolAnnotations(title="Upload & Parse MSDS PDF", readOnlyHint=False, destructiveHint=False, openWorldHint=False), structured_output=False)
async def upload_msds_pdf(
    pdf_source: str,
    session_id: str | None = None,
    experiment_name: str = "MCP Upload",
) -> str:
    """
    Upload an MSDS/SDS PDF file to MSDS Chain and get AI-parsed safety data.

    Parses the PDF with an LLM to extract: chemical name, CAS number,
    GHS hazard classification, NFPA ratings, flash point, LD50, H-codes,
    PPE requirements, storage conditions, incompatibilities, and safety rules.

    If no session_id is provided, a new audit session is automatically created
    and its ID is returned so you can call `get_audit_report` later.

    Requires MSDS_API_KEY — the parsed data is stored under your account.

    Args:
        pdf_source:      A publicly reachable HTTPS URL of the PDF — this is the
                         only form that works from a REMOTE MCP client (ChatGPT,
                         claude.ai, any hosted client), because the server reads
                         the file on ITS OWN filesystem, not the user's. A local
                         file path (e.g. "/tmp/acetone_sds.pdf") works only for a
                         self-hosted stdio server running on the same machine as
                         the file. Do NOT pass a path from the user's machine or
                         from a client-side sandbox to the hosted server — it will
                         not exist there; send the user to the web uploader instead.
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
        # Every early return below is a FAILED upload: nothing is parsed and nothing
        # is stored. They must be logged success=False — otherwise they inflate the
        # mcp_call_logs success rate and we never see them (CI-169, same class as
        # the CI-83 quick-chat-timeout fix).
        if not get_caller_credential():
            success = False
            error_msg = "no caller credential"
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
                success = False
                error_msg = "local file path not readable by server (remote client?)"
                return _upload_local_path_message(pdf_source)
            with open(path, "rb") as f:
                pdf_bytes = f.read()
            filename = _os.path.basename(path)

        if not pdf_bytes:
            success = False
            error_msg = "empty pdf content"
            return "Could not read PDF content — the file is empty (0 bytes)."

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
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("upload_msds_pdf", None, dur, success, error_msg,
                        _json.dumps({"pdf_source": pdf_source, "session_id": session_id}))


@mcp.tool(annotations=ToolAnnotations(title="Batch Safety Check", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
async def batch_safety_check(chemicals: list[str]) -> str:
    """
    Run a comprehensive safety check on a list of chemicals in one call.

    Returns a combined report with:
    - Pairwise compatibility matrix (compatible/caution/incompatible)
    - Key risk warnings per chemical, with the source SDS for each

    It does NOT return PPE or storage grouping — for those call
    `get_ppe_recommendation` / `get_storage_guidance`. (This list previously
    advertised both; the description is what you read when choosing a tool, so
    naming an output that never arrives invites answering from nothing.)

    Good first call when reviewing an experiment protocol or Opentrons deck
    layout: it covers the pairwise interactions in one round-trip.

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
        for pair in compat.get("pairs", []):
            level = pair.get("level", "unknown").upper()
            # CI-89: compat verdicts are rule-based
            traceability = pair.get("traceability", "rule_based")
            basis_label = "Basis (rule)" if traceability == "rule_based" else "Source (SDS)"
            line = (
                f"- **{pair.get('chem1', '?')}** + **{pair.get('chem2', '?')}**: "
                f"{level} — {pair.get('reason', 'N/A')}  [{basis_label}]"
            )
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
            if traceability == "sds_backed":
                trace_label = "[Source: SDS document]"
            elif traceability == "rule_based":
                trace_label = "[Basis: rule/standard]"
            else:
                chem_key = (w.get("chemical") or "").lower()
                if chem_key and chem_key in sds_backed_chemicals:
                    trace_label = "[Source: SDS document]"
                else:
                    trace_label = ""
            inline = _inline_sds(doc_lut, w.get("chemical"), w.get("cas"))  # CI-89-inline
            sections.append(
                f"### {w.get('chemical', 'Unknown')} — {w.get('level', 'unknown').upper()} RISK "
                f"{trace_label}{inline}\n"
                f"- {w.get('description', 'N/A')}\n"
                f"- Mitigation: {w.get('mitigation', 'N/A')}"
            )

        if not data.get("risk_warnings"):
            sections.append("No risk data available.")

        # CI-89: append SDS document links
        if documents:
            sections.append(_format_sds_documents(documents))

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
                        "traceability": p.get("traceability", "rule_based"),
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
                    "traceability": w.get("traceability"),
                }
                for w in data.get("risk_warnings", [])
            ],
            "documents": documents,
        }
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(sections))],
            structuredContent=structured,
        ), data)
    except Exception as e:
        success = False
        error_msg = _error_text(e)
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
        if data.get("_timed_out"):
            success = False
            error_msg = "timeout"
        return _quick_result(data)
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("check_regulatory_lists", [chemical], dur, success, error_msg,
                        _json.dumps({"chemical": chemical}))


@mcp.tool(annotations=ToolAnnotations(title="Get SDS Document", readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
@_graceful_timeout
async def get_sds_document(chemical: str) -> CallToolResult:
    """
    Return a signed download URL for the original SDS/MSDS PDF of a chemical.

    The URL is valid for approximately 5 minutes and can be opened in a browser
    or downloaded with `curl -O`. The response also includes the document's
    source (supplier, region, revision date) so the provenance is clear.

    If only parsed text is available (no original PDF on file), the tool says
    so and suggests using `get_sds_section` to query specific sections instead.

    If the chemical is not in the database at all, the tool suggests uploading
    an SDS PDF via `upload_msds_pdf`.

    Args:
        chemical: Chemical name or CAS number, e.g. "acetone" or "67-64-1"
    """
    t0 = time.monotonic()
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

            region_suffix = f" · {region}" if region else ""
            lines = [
                f"**SDS Document: {chem_name}** (CAS: {cas})",
                f"- **Source:** {supplier}{region_suffix}",
                f"- **Revision date:** {revision_date}",
                f"- **Signed URL** (valid ~5 min):",
                f"  {full_url}",
                "",
                "Open in a browser or `curl -O` to download the PDF.",
            ]
            return CallToolResult(
                content=[TextContent(type="text", text="\n".join(lines))],
                structuredContent={
                    "available": True,
                    "chemical_name": chem_name,
                    "cas": cas,
                    "supplier": supplier,
                    "revision_date": revision_date,
                    "region": region,
                    "record_id": data.get("record_id"),
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

            display = f"{chem_name} (CAS: {cas})" if cas else chem_name
            return CallToolResult(
                content=[TextContent(type="text", text=f"**{display}**: {message}{hint}")],
                structuredContent={
                    "available": False,
                    "chemical_name": chem_name,
                    "cas": cas,
                    "message": message,
                },
            )
    except Exception as e:
        success = False
        error_msg = _error_text(e)
        raise
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        await _log_call("get_sds_document", [chemical], dur, success, error_msg,
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
