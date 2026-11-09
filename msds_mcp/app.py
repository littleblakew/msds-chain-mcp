from __future__ import annotations

import logging
import os
import textwrap
import ui_compat_card
from mcp.server import MCPServer
from mcp.server.caching import CacheHint




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
    "https://api.msdschain.lagentbot.com",
).rstrip("/")
# Signed SDS links are issued on the backend's PUBLIC origin, which is not necessarily API_URL:
# a deployment can reach the backend through an internal address while the links it hands to
# users still point at the public domain. The compatibility card opens only this origin.
_CARD_LINK_ORIGIN = ui_compat_card.link_origin(
    os.environ.get("MSDS_SDS_LINK_ORIGIN", "https://api.msdschain.lagentbot.com"))
# CI-1403：余额耗尽时卡片上「升级」按钮唯一能打开的源；后端给的链接不在这个源上就退回默认页。
_UPGRADE_URL = os.environ.get("MSDS_UPGRADE_URL", "").strip() or "https://msdschain.lagentbot.com/pricing"
_UPGRADE_ORIGIN = ui_compat_card.link_origin(_UPGRADE_URL)
if not _UPGRADE_ORIGIN.startswith(("https://", "http://")) or _UPGRADE_ORIGIN.endswith("://"):
    # 配置坏了就退回默认页，别把一个空源注入卡片（卡片那侧对空源是拒绝打开）。
    _UPGRADE_URL = "https://msdschain.lagentbot.com/pricing"
    _UPGRADE_ORIGIN = ui_compat_card.link_origin(_UPGRADE_URL)
LANG = os.environ.get("MSDS_LANG", "en")  # 实测后端只认 en / zh，见下 _BACKEND_LANGS


def _log_secret_header() -> dict[str, str]:
    """CI-835: the backend's /mcp/call-log now requires this shared secret.

    🔴 Read at call time, not at import: the env var is set on the Container App
    a moment before the enforcing backend ships, and a core that cached "" at
    import would keep posting unauthenticated (⇒ 401 ⇒ every call record dropped,
    with only `mcp_call_log_post_failed` in stderr to say so).

    🔴 Sent ONLY on the call-log POST, never on tool calls to the backend: this
    credential authenticates "we are the log writer", and the tool path already
    carries the end user's own key. Mixing them would put a service credential
    on every request a third party's tool call generates.

    Name note: backend + core read `MCP_LOG_SECRET`; the gateway reads
    `MSDS_GW_MCP_LOG_SECRET` (its Settings has an env_prefix). Same value.
    """
    secret = os.environ.get("MCP_LOG_SECRET", "")
    return {"X-MCP-Log-Secret": secret} if secret else {}
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
# The budgets below were set from measured call latency, not from a guess. The
# distinguishing signal is `duration_ms` pinned at the client ceiling (≈15,0xx)
# rather than a backend 5xx: multi-component tools reach it, single-chemical and
# lookup tools stay an order of magnitude below it. A multi-component call that
# dies at the ceiling is a product failure, not a quota failure — see CI-176.
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
# tests/contract/test_version.py fails CI if the two ever drift. Do NOT hand-edit — bump
# VERSION and run scripts/release.sh. `version=` is a first-class MCPServer ctor arg
# (mcp 2.x) and is what surfaces as serverInfo.version in the MCP `initialize`
# handshake (what ChatGPT, claude.ai and any raw MCP client display). Without it the
# SDK falls back to reporting the `mcp` package version — a meaningless value.
__version__ = "1.5.13"

# ---------------------------------------------------------------------------
# 缓存提示（CI-515，2026-07-28 spec / SEP-2549）
# ---------------------------------------------------------------------------
# `tools/list` 和 `server/discover` 的内容**每个镜像内是常量**：23 个工具在 import 时
# 无条件注册（没有任何按用户/套餐的过滤），网关侧也不改工具表（只在 `tools/list` 上打一个
# funnel 埋点，`gateway/proxy.py:92`）⇒ 同一次部署里，任何调用方拿到的都是同一份。
# 不给 hint 的话 SDK 填的是 `ttlMs: 0`＝「立刻过期」，于是每个 agent 每轮对话都要重列一次。
#
# 🔴 scope 选 `private` 而不是 `public`，**明知内容是全用户一致的**：`public` 允许中间缓存
# 跨授权上下文复用同一份结果。今天成立，但我们随时可能按 plan 把某些工具收起来（配额/套餐
# 是既有机制），那一刻如果忘了改回来，共享缓存就会把 pro 的工具表发给 free 用户。
# `private` 的最坏情况只是「少了一层共享缓存」，`public` 的最坏情况是串数据。
# ⇒ **要改成 `public`，前提是先有一条守卫钉住「工具表不随调用方变化」。**
#
# TTL 取 5 分钟：部署换 revision 后，客户端最多晚 5 分钟看到新工具（我们一周部署数次，
# 不是数秒级变更），而 agent 每轮重列的开销实打实省掉。SDK 客户端侧上限是 24h。
_LIST_CACHE = CacheHint(ttl_ms=300_000, scope="private")

# mcp 2.x: `host`/`port`/`transport_security` are no longer ctor args — they moved to
# streamable_http_app()/sse_app() (see server_remote.py). host/port were already dead
# weight here: server_remote.py feeds them straight to uvicorn.
mcp = MCPServer(
    "MSDS Chain",
    version=__version__,
    # 只列我们真正提供的两个：resources/prompts 一个都没注册（`CACHEABLE_METHODS` 里其余
    # 四个方法在本服务上没有 handler），凭空给它们 hint 是写一份永远不执行的配置。
    cache_hints={"tools/list": _LIST_CACHE, "server/discover": _LIST_CACHE},
    # 🔴 CI-1101: this copy is served to **self-hosted / stdio** installs only.
    # Hosted clients read `hosted_instructions.txt` instead: with MSDS_MCP_EDGE=1
    # `server_remote.py` swaps it in (until the edge takes over, the distribution
    # gateway replaces `result.instructions` with its own copy of that text).
    # Editing the text below to change what hosted users read is a **silent
    # no-op**. The two documents are meant to differ (CI-405) — do not merge them.
    # Guard: `tests/contract/test_instructions_audience.py` makes the edit noisy.
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
