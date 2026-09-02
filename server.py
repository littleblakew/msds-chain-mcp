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

import base64
import binascii
import functools
import json
import json as _json
import logging
import os
import re
import textwrap
import time

from typing import Annotated, Literal

from contextvars import ContextVar

import httpx
from mcp.server import MCPServer
from mcp.server.caching import CacheHint
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
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
LANG = os.environ.get("MSDS_LANG", "en")  # 实测后端只认 en / zh，见下 _BACKEND_LANGS
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
# VERSION and run scripts/release.sh. `version=` is a first-class MCPServer ctor arg
# (mcp 2.x) and is what surfaces as serverInfo.version in the MCP `initialize`
# handshake (what ChatGPT, claude.ai and any raw MCP client display). Without it the
# SDK falls back to reporting the `mcp` package version — a meaningless value.
__version__ = "1.5.10"

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


# ---------------------------------------------------------------------------
# Parameter types (CI-521)
# ---------------------------------------------------------------------------
# 🔴 An MCP client is an LLM agent, and the ONLY thing it reads when deciding what
# to put in an argument is that argument's JSON Schema entry. Prose that lives in
# the Python docstring reaches `Tool.description` — it does NOT reach
# `inputSchema.properties.<arg>.description`. Before CI-521 all 37 parameters had
# `description: null`, and `scenario` was the proof that this is not cosmetic: the
# backend hard-rejects anything outside spill/fire/exposure, the schema advertised
# a bare `"type": "string"`, and an agent writing a first-aid call naturally sent
# `"skin contact"` and got an error string back. Microsoft's own M365 connector
# guidance says the same thing — the per-parameter description is what the agent
# reads — and that same schema is what we ship in the connector package.
#
# So: every parameter goes through an Annotated alias or an inline Field below.
# Constrained values are expressed in the TYPE (Literal / ge / le) so the enum and
# the bounds land in the schema, rather than only being described in prose that a
# client is free to ignore.
# ---------------------------------------------------------------------------
# CI-356：让调用方的 AI 按对话语言点答复语言
# ---------------------------------------------------------------------------
# `LANG` 是**服务端环境变量**，托管网关上恒为 `en`，而且不是工具参数 ⇒ 中文用户在
# ChatGPT/Claude 里用中文问，拿到的安全答复是英文的。不是检测错了，是根本没有检测，
# AI 也没有地方可以表达。它正在用那个语言对话，它最清楚要什么语言。
#
# 🔴 **后端今天只真的支持 en 和 zh**（2026-08-15 逐语言实测，别信下面 `LANG` 那行注释
# 曾经写的 `en|zh|ja|de|id`——那是愿望不是事实）。v2 端点的行为是**二元**的：
# `lang == "en"` → 英文，**其他任何值**（`ja`/`de`/`id`/`fr`/`zh-CN`/空串）→ **中文**；
# quick-chat 同样（`lang=ja` 实测返回 343 个汉字、零假名）。
#
# ⇒ 所以**归一化必须放在我们这一层**：不归一的话，一个日语用户会拿到**中文**——
# 比现在的英文更糟（看不懂 + 误以为系统支持日语）。归一之后，参数描述里那句
# 「其他值回退英文」才是真的，而不是一句照抄自 CI-258 却没人验过的承诺。
#
# 🔴 **要加语言，判据是「实测那个语言真的出来了」**，不是「后端文档说支持」，
# 也不是「往这个元组里加一行」。
_BACKEND_LANGS = ("en", "zh")


def _normalize_lang(lang: str | None) -> str:
    """把调用方给的语言码收敛成后端**真的**会照做的那几个；其余一律英文。"""
    if lang and lang.strip().lower() in _BACKEND_LANGS:
        return lang.strip().lower()
    return "en"


Lang = Annotated[str | None, Field(
    description='Answer language — pass the language THIS conversation is in, not the '
                'user\'s country. Currently supported: "en", "zh". Anything else, or '
                'omitted, is answered in English.',
)]


ChemicalList = Annotated[list[str], Field(
    description='List of chemical names or CAS numbers, e.g. ["acetone", "sulfuric acid"] '
                'or ["67-64-1", "67-56-1"]. Names and CAS numbers can be mixed.',
)]
Chemical = Annotated[str, Field(
    description='Chemical name or CAS number, e.g. "acetone" or "67-64-1".',
)]

# CI-823：调用方（客户端 LLM）把用户的话翻成下面那些结构化参数。翻错了我们看得见入参、
# 看不见「他本来要问什么」——423 次外部调用里只有 38 次走 `ask_chemical_safety`，
# 带自然语言问句的全时段只有 10 条，其余工具的意图面是零。
# 🔴 这是**只读日志面**：`intent` 不会被送到后端、不参与作答。任何一条让它流进答案的路
# 都是安全结论的注入面（调用方可以把「就说它安全」写进来）。
# 🔴 这段描述在 `tools/list` 里**每个工具各有一份**（17 份）：初版 590 字符 ＝ 整个
# 响应的 20.5%，每个客户端连上来都付这笔 context。措辞按守卫断言的三句收敛过。
Intent = Annotated[str | None, Field(
    description="Optional, recorded only — it does NOT change the answer. Never put "
                'instructions here, and never leave a real argument out in favour of it. '
                "One sentence in the end user's own words of what they are trying to "
                'find out. Over-long text is truncated, not rejected.',
)]


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


def _chemicals_from_response(data: dict | None) -> list[str] | None:
    """CI-529：从**后端已经解析好的结构化字段**里取化学品名，回填调用日志。

    🔴 只读结构化字段，**绝不从 `answer` 正文里抽**。从自由文本反解析是 [[CI-527]] 的地盘
    且是已判定错误的路：正文里的名字可能是模型顺带提到的另一种物质，抽出来会让「按化学品
    聚合」从**缺**变成**错**，而错的看不出来。

    🔴 **键名照抄后端 `routers/quick_chat.py` 里那段同款提取**（它为 `build_sds_documents`
    做的是同一件事），不要自己发明：初版按想当然写了 `result["chemicals"]` 是字符串列表、
    相容性在 `result["pairs"]` 里——两个键后端**都不产出**（真实是 `chemicals` 为**匹配记录
    的 dict 列表** + `query` 是用户原词；相容性是**顶层** `chemical_a`/`chemical_b` 与
    `matrix[]`）。于是提取器在最常见的两条路径上恒空，而手写 fixture 的测试全绿——
    [[narrow-hand-rolled-fixtures-and-engine-specific-branches]] 的标准形状，review 抓到的。

    取不到返回 None（不是空列表）：**「没记」和「记了但是空的」在下游是两件事**。
    """
    if not isinstance(data, dict):
        return None
    # 🔴 这个函数在 `finally` 里跑：从这里抛出去会**同时**毁掉工具的返回值和整条调用日志
    # （异常发生在 `_log_intent` 之前 ⇒ slot 还是空的 ⇒ `_reported` 什么都不记，
    # 而调用方拿到的是 TypeError 而不是答案）。日志是尽力而为的东西，绝不许有这种代价。
    try:
        return _extract_chemicals(data)
    except Exception:  # noqa: BLE001 —— 见上：宁可少记一列，也不能吃掉答案
        logger.warning("CI-529 chemicals extraction failed", exc_info=True)
        return None


def _extract_chemicals(data: dict) -> list[str] | None:
    names: list[str] = []
    seen: set[str] = set()

    def _add(value) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            names.append(value)

    def _as_list(value):
        return value if isinstance(value, list) else []

    for doc in _as_list(data.get("documents")):
        if isinstance(doc, dict):
            # 🔴 `chemical`（调用方问的词）优先于 `chemical_name`（供应商 SDS 的产品标题，
            # 形如 "Acetone, ACS reagent, ≥99.5%"）——后者拿去 resolve_cas 可能解析成
            # 别的东西或解析不出来，而这一列的下游正是 CI-174 的报告范围。
            _add(doc.get("chemical") or doc.get("chemical_name"))

    for entry in _as_list(data.get("tool_results")):
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict):
            continue
        # search_chemical：`query` 是用户原词，`chemicals` 是**匹配记录**（dict）
        _add(result.get("query"))
        for hit in _as_list(result.get("chemicals")):
            if isinstance(hit, dict):
                _add(hit.get("name") or hit.get("chemical_name"))
        # risk / compliance / exposure / regulatory 等：顶层 chemical / chemical_name
        _add(result.get("chemical"))
        _add(result.get("chemical_name"))
        # check_compatibility：顶层 chemical_a / chemical_b
        _add(result.get("chemical_a"))
        _add(result.get("chemical_b"))
        # check_all_compatibility：matrix[]（不是 pairs）
        for pair in _as_list(result.get("matrix")):
            if isinstance(pair, dict):
                _add(pair.get("chemical_a") or pair.get("chem1"))
                _add(pair.get("chemical_b") or pair.get("chem2"))
        # generate_risk_warnings：warnings[].chemical
        # 🔴 CI-596：相容性派生的 warning 描述的是**两个**化学品，历史上把它们拼成
        # `"A+B"` 放进 `chemical`。这里收的名字会进 intent 日志的化学品列（需求语料
        # 的输入面）⇒ 拼接串会以一个不存在的化学品身份留痕。后端现在给结构化的
        # `chemicals`，有它就以它为准，拼接的那个只当显示文案、绝不当身份。
        # 🔴 分支打在 `kind` 上而不是「`chemicals` 非空」上：后者会让一条
        # kind=pair 但成员缺失的 warning 掉回 else，把拼接串重新当身份收进来
        # ——未知升级成乐观分支。pair 拿不到成员就一个都不收。
        for w in _as_list(result.get("warnings")):
            if not isinstance(w, dict):
                continue
            if w.get("kind") == "pair":
                for member in _as_list(w.get("chemicals")):
                    if isinstance(member, str):
                        _add(member)
                continue
            members = _as_list(w.get("chemicals"))
            if members:
                for member in members:
                    if isinstance(member, str):
                        _add(member)
                continue
            _add(w.get("chemical") or w.get("chemical_name"))

    # 🔴 名字里带逗号的丢掉：这一列在后端是 `",".join(names)` 存的（`mcp_log.py`），
    # 读回来按逗号切 ⇒ `N,N-dimethylformamide` 会变成 `N` + `N-dimethylformamide` 两条，
    # 而 CI-174 的报告范围正是从这里取的。丢掉比劈开好：缺一条是缺，劈开是**编造**。
    # （存储格式本身该修，那是 CI-344 那条线的事，不在本票范围。）
    names = [n for n in names if "," not in n]
    return names[:24] or None


def _quick_result(data: dict) -> CallToolResult:
    """Build a CallToolResult for quick_chat-backed tools.

    Preserves the human-readable answer as text content (for Claude and other
    clients) and exposes structuredContent (answer + raw tool_results) for
    clients that consume structured output (e.g. ChatGPT Apps SDK).

    CI-89: if the backend returns a top-level `documents` list (blob-backed SDS
    descriptors), append an "📄 Original SDS" section to the text and include
    the list in structuredContent.

    🔴 CI-592：structuredContent **从手抄白名单换成透传**（`_expose`，与 CI-342 给直连
    工具做的同一件事）。旧写法逐字段列了 3 个键，后端新增的字段**不会带上，也不会报错**
    ——客户端侧就是「这个字段不存在」。本票要送出去的 `unchecked` 正是这样一个新字段，
    而下一个新字段还会来。
    """
    answer = data.get("answer", "")
    tool_results = data.get("tool_results", [])
    documents = data.get("documents", [])
    # CI-89-followup: the SDS document links must come RIGHT AFTER the answer, before
    # the raw tool-data appendix. Appended last (after _format_tool_results' JSON blob)
    # the client model summarizes the answer and drops the trailing link — verified on
    # prod: backend returns documents correctly, but claude.ai never surfaced the link
    # for ask_chemical_safety while the (short, link-last) direct tools did.
    text = (_unchecked_directive(data.get("unchecked"))
            + answer + _format_sds_documents(documents)
            + _format_tool_results(tool_results))
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        # 🔴 `override` 那三个键是**已有契约**：老写法无论后端给没给都保证它们在
        # （`documents` 缺失时给 `[]`）。换成透传时若不补这一手，后端某次没带
        # `documents` 就会让读 `sc["documents"]` 的客户端 KeyError —— 全量测试当场
        # 抓到（`test_quick_result_ci89_no_documents`）。**新键不享受这个待遇**：
        # `unchecked` 缺失时就该缺失，那是「老后端」这个信息本身。
        structured_content=_expose(
            data, drop=_QUICK_INTERNAL_KEYS,
            override={"answer": answer, "tool_results": tool_results,
                      "documents": documents}),
    )


# 🔴 CI-592：这条通道上「散文」不是确定性的——`answer` 会被 claude.ai / Copilot 的
# 客户端模型**重写**之后才到用户眼前，而 CI-587 的原始事故正是从 MCP 的
# `validate_protocol_chemicals` 上观察到的。所以这里给的是**指令**，不是内容：
# 内容（用户可见的那段话）由后端确定性渲染在 `answer` 最前面，本行只负责让改写它的
# 那个模型知道这段不许丢、不许改写成「我们没有这些数据」。
#
# 形状照抄 [[CI-567]] 的 `[protocol]`：那次实测证明**光把正确内容放进载荷不够**，
# 模型仍会用通用内容覆盖物质级步骤，是「配对的显式禁令」把它扳回来的。
#
# 🔴 措辞红线与后端模板同源（`quick_engine._UNCHECKED_TEMPLATES`）：只说两件真话
# ——没被检查、怎样才能让它被检查。**不许出现「未收录 / 没有数据 / 建议上传」**，
# 那是断言一件我们根本没验证过的事（CI-243 / CI-322 / CI-334 三次同形事故）。
_UNCHECKED_DIRECTIVE = (
    "[unchecked] {n} chemical(s) were NOT looked up in this turn (per-question limit): "
    "{names}.\n"
    "You MUST relay this to the user BEFORE any safety conclusion, and you MUST NOT "
    "state or imply whether we do or do not have data/records for them — this turn did "
    "not look. Tell the user to ask about them separately. Do not drop this when "
    "summarizing.\n\n"
)


def _unchecked_directive(unchecked) -> str:
    """把后端的 `unchecked` 渲染成给客户端模型的显式指令。空/缺失 ⇒ 空串。

    🔴 **三态，这里只有两种渲染，是有意的**：`[]`＝后端说「没有未检查的」；`null`＝
    后端说「这一轮没算」（额度用尽、或 5 条走不到化学管线的早退路径）；键缺失＝老后端
    （本仓与后端各自发布，版本会错开）。后两者都渲染成空串——没有可靠名单时凭空说一句
    「可能有没查的」只会制造噪声。区分留在 structuredContent 里，客户端自己看得见。
    """
    if not isinstance(unchecked, list):
        return ""
    names = [str(n) for n in unchecked if isinstance(n, str) and n.strip()]
    if not names:
        return ""
    return _UNCHECKED_DIRECTIVE.format(n=len(names), names=", ".join(names))


# 后端没给 note 时的兜底文案（老后端、或将来新增的 reason）。键是机器可判的
# `document_unavailable_reason`；未知 reason 落到空串 ⇒ 只少一句解释，不会瞎猜原因。
_UNAVAILABLE_FALLBACK = {
    "daily_pdf_quota_reached": "we hold this SDS, but your daily download quota is used up",
    "insufficient_credits": "we hold this SDS, but your credit balance is insufficient to fetch it",
    "quota_check_failed": "we hold this SDS, but the download check is temporarily unavailable",
}


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
        else:
            # 🔴 CI-488：没有 URL **不等于**「我们没有这份 SDS」。
            #
            # 后端现在会在每日下载额度用尽（或配额子系统故障）时保留条目、去掉链接，
            # 并附上 `document_unavailable_reason` / `_note`。少了这一句，这里渲染出来
            # 的是「📄 Original SDS」标题下一行**没有链接也没有解释**的条目——人和模型
            # 都只会读成「这份文件不存在/坏了」，正是后端那两个字段要避免的歧义。
            # 后端建模好了而渲染层没接，本仓吃过的亏就叫「修了但没到达真正的消费者」。
            note = doc.get("document_unavailable_note") or _UNAVAILABLE_FALLBACK.get(
                doc.get("document_unavailable_reason") or "", ""
            )
            if note:
                entry += f" — {note}"
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


def _traceability_label(traceability: str | None, chem_key: str | None,
                        sds_backed_chemicals: set) -> str:
    """把后端的 `traceability` 翻成给模型读的一句出处标注。

    🔴 **单一实现，三个格式化点共用。** 此前这段逻辑在 `server.py` 里**抄了三份**
    （risk-warnings / PPE / 综合报告），CI-336 只在其中一处发现缺陷——
    与 CI-277 记的「收口不止一处，判定链有三个入口会漏」是同一个形态：
    分头修会修三次补丁，而下一类错值来时还得有人记得再改三遍。

    🔴 **`none` 不等于「字段缺失」。** 它是 CI-243 引入、CI-365 沿用的值，含义是
    **什么依据都没有读到**（记录连危害数据都没有）。它一度掉进下面那个
    「这个化学品有没有 PDF」的推断分支，于是一条零危害数据的记录被盖上
    `[Source: SDS document]` —— **后端刚拿掉的虚假出处声明，被这一层原样加了回去**，
    而且是在模型直接消费的文本里。产品唯一的对外主张就是「答案可追溯到具体供应商 SDS」，
    这句话在这里是假的。

    推断分支只保留给**后端根本没给这个字段**的情况（老后端）。即便如此，它断言的也只是
    「这个化学品有文档」，不是「这条结论出自那份文档」——所以别把它扩大到任何已知值上。
    """
    if traceability == "sds_backed":
        return "[Source: SDS document]"
    if traceability == "rule_based":
        return "[Basis: rule/standard]"
    if traceability == "none":
        return ""
    key = (chem_key or "").lower()
    return "[Source: SDS document]" if key and key in sds_backed_chemicals else ""


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
            # 超时话术也跟调用方的语言走（`lang` 是关键字参数时才取得到；取不到就用服务端默认）
            lg = kwargs.get("lang") or LANG
            return _DIRECT_TIMEOUT_MSG.get(lg, _DIRECT_TIMEOUT_MSG["en"])
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


async def _quick_chat(message: str, lang: str | None = None) -> dict:
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
                json={"message": message, "lang": _normalize_lang(lang or LANG)},
                headers=_headers(),
            )
            return _billed_json(res)
    except httpx.TimeoutException:
        return {"answer": _TIMEOUT_ANSWER.get(lang or LANG, _TIMEOUT_ANSWER["en"]),
                "tool_results": [],
                "_timed_out": True}


# 🔴 CI-595：这两个数是**量出来的**，别凭感觉调。Prod 上一份 4 化学品的相容性结果
# 10,227 字符；旧的 600 会让 6 对里只剩 2 对、且切在 JSON 中间，而被切掉的正是排在
# 后面的漂白剂+盐酸（氯气）· 漂白剂+氨水（氯胺）。4000 的依据：matrix 2,534（结论）
# + sources 1,509（供应商与版本日期，工具说明要求必须引用）+ 结构开销 ≈ 3,900，
# 派生的 warnings（6,061，与 matrix 重复）被丢掉并留记号。
# 总预算是防「某个工具返回一份病态大列表就把上下文撑爆」——按条目先到先得。
_RAW_ENTRY_BUDGET = 4000
_RAW_TOTAL_BUDGET = 8000


def _shorten_strings(obj, allowance: int):
    """把过长的字符串截短（保留键），供 `_compact_for_context` 用。

    只动**字符串值**，不动键、不动数值、不删字段——结论住在字段里（`verdict` /
    `level`），解释住在长字符串里（`reason`）。截短处留一个带**量级**的记号。

    🔴 两个坑（review 实测抓到的）：
    ① 记号自己也占长度 ⇒ `allowance` 太小时「缩短」后的串**比原串还长**，
       于是逐级收紧的循环**不单调**，反而把载荷推进兜底截断。⇒ 下限钳住。
    ② 记号里的数字要是**真正丢掉的字符数**，不是「超出 allowance 的量」——
       两者差一个记号的长度，报少了会让人以为丢得比实际少。
    """
    if isinstance(obj, str):
        if len(obj) <= allowance:
            return obj
        keep = max(allowance - 20, 24)
        if keep >= len(obj):
            return obj
        return obj[:keep] + f"...(+{len(obj) - keep} chars)"
    if isinstance(obj, dict):
        return {k: _shorten_strings(v, allowance) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shorten_strings(v, allowance) for v in obj]
    return obj


def _with_markers_first(body: dict, omitted: dict) -> dict:
    """把 `_omitted_*` 记号排在**最前面**。

    🔴 review 实测：记号是新加的键，`json.dumps` 把它排在最后，于是兜底的字节截断
    **第一个砍掉的就是它** ⇒ 模型拿到一份空矩阵却看不到任何「有东西被删了」的提示，
    读起来就是「没有发现不相容」。丢弃重新变回静默——正是本函数存在的理由。
    """
    return {**omitted, **body}


def _compact_for_context(result, budget: int = _RAW_ENTRY_BUDGET) -> str:
    """把一条工具结果压进预算 —— **按结构压，不按字节切**。

    🔴 CI-595：这里原来是 `json.dumps(result)[:600]`。逐对的 `check_compatibility`
    结果各自远小于 600，所以每一对都活着；CI-589 之后快聊面改发**一份整份矩阵**
    （Prod 实测 **10,227 字符**）⇒ 附录被切在 JSON 中间，**6 对里只剩 2 对，而被切掉的
    正是排在后面的那几对**——漂白剂+盐酸（氯气）、漂白剂+氨水（氯胺）。而本文件另一处
    注释已写明「多数 MCP 客户端**只读 text**」⇒ 这条通道不是可有可无的补充。

    降级顺序（**每一步都可解释，不靠「哪个列表最大」这种启发式**）：
      ① 逐级缩短长字符串——解释可以短，结论不能没有
      ② 还放不下才丢条目，**在所有列表之间轮流丢**，并把 `_omitted_*` 记号排在最前
      ③ 最后才字节截断，并写明丢了多少

    🔴 **别再按「最大的那个列表」优先丢**（第一版就是这样，review 用 20 个化学品的
    批量结果打穿了）：那份载荷里最大的列表**恰好是 matrix 本身**（190 对，而 warnings
    只有 2 条）⇒ 190 对里 175 对被丢，**两条 incompatible 一条都没活下来**，因为它们
    排在尾部——正是本票说「绝不能被切掉」的那个位置。轮流丢不保证公平，但它保证
    **不会有某一条列表被单独清空**。
    """
    full = json.dumps(result, ensure_ascii=False)
    if len(full) <= budget:
        return full

    def _dump(obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

    if not isinstance(result, dict):
        keep = max(budget - 28, 1)
        return f"{full[:keep]}...(+{len(full) - keep} chars trimmed)"

    # ① 缩短字符串（保住全部字段与全部条目）
    work = result
    for allowance in (160, 100, 60, 40, 24):
        work = _shorten_strings(result, allowance)
        if len(_dump(work)) <= budget:
            return _dump(work)

    # ② 轮流丢条目，记号在最前
    body = {k: v for k, v in work.items()}
    omitted: dict = {}
    lists = [k for k, v in body.items() if isinstance(v, list) and v]
    # 🔴 dict 值同样能吃掉整个预算（`sources` 在实测载荷里 1,509 字符，而它不是列表）
    # ——第一版只认列表，于是 matrix 被清空、结果**仍然**超预算、最后走字节截断，
    # 两条红线（结论不许丢 / 必须仍是合法 JSON）被同一份载荷同时打破。
    dicts = [k for k, v in body.items() if isinstance(v, dict) and v]

    def _too_big() -> bool:
        return len(_dump(_with_markers_first(body, omitted))) > budget

    guard = 0
    while _too_big() and (lists or dicts) and guard < 10000:
        guard += 1
        pool = [(k, len(body[k])) for k in lists if len(body[k]) > 0] + \
               [(k, len(body[k])) for k in dicts if len(body[k]) > 0]
        if not pool:
            break
        # 轮流：每次从**当前条目数最多**的那个容器里砍掉一条尾部条目。
        # 🔴 用条目数而不是字节数选，才不会因为某个列表条目更长就被反复清空。
        key = max(pool, key=lambda kv: kv[1])[0]
        container = body[key]
        # 按比例先砍一刀，别一条一条删——review 实测 3,000 条的列表逐条重序列化
        # 要 7.7 秒 CPU，而这是**同步**调用在事件循环里。
        # 🔴 **二分出放得下的最大条目数**，别按固定比例砍：砍 25% 会过度丢弃
        # （实测同一份 Prod 载荷，预算够放 6 对时只留下了 3 对），而逐条砍在 3,000 条
        # 的列表上要 7.7 秒 CPU——这是**同步**调用在事件循环里。二分两头都躲开。
        def _sample(container, keep_n):
            """跨步取样，**不是砍尾巴**。CI-589 的原始 bug 就是位置偏置；砍尾巴是
            同一个毛病换个位置——review 实测 20 个化学品的批量结果里两条
            `incompatible` 落在下标 150 / 188，砍尾巴让它们一条都没活下来。
            取样不能保证留下危险的那些（那需要语义），但保证幸存者**铺满整个列表**。"""
            n = len(container)
            keep_n = max(1, min(keep_n, n))
            if keep_n >= n:
                return container
            idx = sorted({min(round(i * (n - 1) / max(keep_n - 1, 1)), n - 1)
                          for i in range(keep_n)}) if keep_n > 1 else [0]
            if isinstance(container, list):
                return [container[i] for i in idx]
            keys = list(container)
            return {keys[i]: container[keys[i]] for i in idx}

        original = container
        lo, hi = 0, len(container) - 1
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            body[key] = _sample(original, mid)
            omitted[f"_omitted_{key}"] = len(original) - len(body[key])
            if len(_dump(_with_markers_first(body, omitted))) <= budget:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        body[key] = _sample(original, best)
        kept = len(body[key]) if best else 0
        if best == 0:
            body[key] = [] if isinstance(original, list) else {}
            kept = 0
        omitted[f"_omitted_{key}"] = len(original) - kept
        if kept == len(original):
            # 这个容器已经放得下了却仍然超预算 ⇒ 换下一个容器，别死循环
            lists = [k for k in lists if k != key]
            dicts = [k for k in dicts if k != key]

    out = _dump(_with_markers_first(body, omitted))
    if len(out) > budget:
        # ③ 兜底必须**真的**兜住：不加这一步，`_RAW_TOTAL_BUDGET` 只是个愿望——
        # 调用方按返回值扣预算，而返回值可以任意大。代价是这一条不再是合法 JSON。
        keep = max(budget - 28, 1)
        return f"{out[:keep]}...(+{len(out) - keep} chars trimmed)"
    return out


def _format_tool_results(tool_results: list[dict]) -> str:
    """Render tool_results as compact structured text for context."""
    if not tool_results:
        return ""
    lines = ["\n\n---\n**Raw tool data:**"]
    remaining = _RAW_TOTAL_BUDGET
    left = len(tool_results)
    for item in tool_results:
        tool = item.get("tool", "unknown")
        result = item.get("result", {})
        # 🔴 **按剩余条目均分**，不是先到先得。先到先得时前两个大结果就能吃掉总预算的
        # 一半以上，第三个（可能正是相容性结论）只剩几十字符 ⇒ **比旧的 `[:600]` 还惨**，
        # 而且「哪个工具活下来」取决于后端返回的顺序——一个与安全无关的变量。
        share = max(remaining // max(left, 1), 200)
        rendered = _compact_for_context(result, min(_RAW_ENTRY_BUDGET, share))
        remaining = max(remaining - len(rendered), 0)
        left -= 1
        lines.append(f"\n`{tool}`: {rendered}")
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
                    input_params: str | None = None, response_text: str | None = None):
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
                    # CI-333：客户端真正读到的正文。后端在 CI-333/344 那版之前会静默
                    # 忽略这个字段（pydantic 默认 ignore extra），所以先发后存是安全的。
                    "response_text": response_text,
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
# CI-333：把「何时上报、上报多少」收进一个装饰器
# ---------------------------------------------------------------------------
# 要记的新东西是**回复正文**，而它在每个工具的 `finally` 里拿不到——返回值是在 `try` 里
# 直接 `return` 掉的（23 个工具共 48 个 return 点）。逐个改成 `result = …; return result`
# 的话，**漏掉任何一个 return，那条路径就永远没有正文，且不报错**。
#
# 所以反过来：工具在 `finally` 里只**声明要记什么**（`_log_intent`），由外层装饰器统一负责
# 计时、成败、取返回值、发 POST。装饰器拿到的是**函数真正返回的那个对象**，
# 结构上不可能漏掉某条 return 路径。
#
# 🔴 **声明的内容仍然由各工具自己算，装饰器不去 dump 入参**——那些手写的 dict
# **编码的是脱敏决定**：`upload_msds_pdf` 记的是 `<inline data URI, N chars>` 而不是那段
# base64（见该处注释：记录哪怕一个前缀都会把客户 SDS 内容写进 `mcp_call_logs`）。
# CI-344 之后 `input_params` 原文真的落库 ⇒ 这条脱敏从「防御性」变成「承重」，
# 自动 dump 入参会直接把客户文档字节写进日志表。
_log_slot: ContextVar[dict | None] = ContextVar("mcp_log_slot", default=None)


def _log_intent(tool_name: str, chemicals: list[str] | None,
                input_params: str | None = None, *,
                success: bool = True, error_message: str | None = None) -> None:
    """工具声明：本次调用要记的身份 + **已脱敏的**入参（+ 可选的失败标记）。

    🔴 `success` 存在是因为**有些失败不抛异常**：quick-chat 超时被转成一句可读消息、
    `upload_msds_pdf` 把失败作为文本返回。装饰器只看得见「抛没抛」，看不见这一类
    ⇒ 工具必须能把它降级。初版漏了这个参数，基线比对当场抓到 `upload_msds_pdf`
    从 `success=False` 变成了 `True`——一个只在日志里、线上完全看不出来的退化。

    只能**降级**：装饰器那边取的是 `抛没抛 and 这里说的`，工具说不了「其实成功」。
    """
    _log_slot.set({"tool_name": tool_name, "chemicals": chemicals,
                   "input_params": input_params,
                   "success": success, "error_message": error_message})


# CI-823：上限只**截断不拒绝**。写成 schema 的 `maxLength` 会让 pydantic 直接打回整次调用
# ⇒ 一个纯诊断字段就能把一次真实的安全查询挡在门外，方向反了。
_MAX_INTENT_CHARS = 500


def _cap_intent(text: str) -> str:
    return text if len(text) <= _MAX_INTENT_CHARS else text[:_MAX_INTENT_CHARS] + "…[truncated]"


def _intent_params(payload: dict, intent: str | None) -> str:
    """把工具**手写的**脱敏 dict 序列化，调用方给了 `intent` 就多挂一个键（截断后）。

    🔴 「手写 dict 编码的是脱敏决定」这条约定不变（见 `_reported` 上方那段）——这里只
    多挂一个键，仍然不去自动 dump 入参。`intent` 缺省时输出与加这个参数之前**逐字节相同**。
    """
    if intent and intent.strip():
        payload = {**payload, "intent": _cap_intent(intent.strip())}
    return _json.dumps(payload)


# 发送侧的上限。后端也会截（`_clean_payload`），这里再挡一道是因为**这一段要走网络**：
# 一份 PDF 抽出来的正文可以很大，让它先跑一趟 HTTP 再被对面砍掉是白费带宽和延迟，
# 而这个 POST 挂在每个工具调用的关键路径后面。数值与后端保持一致，改一处要改两处。
_MAX_RESPONSE_LOG_CHARS = 20_000


def _response_text(result) -> str | None:
    """把工具的返回值压成一段文本——客户端真正读到的那一份。"""
    if result is None:
        return None
    if isinstance(result, str):
        return _cap(result)
    content = getattr(result, "content", None)
    if content:
        parts = [t for b in content if (t := getattr(b, "text", None))]
        if parts:
            return _cap(("\n".join(parts)))
    return None


def _cap(text: str) -> str:
    return text if len(text) <= _MAX_RESPONSE_LOG_CHARS else text[:_MAX_RESPONSE_LOG_CHARS] + "…[truncated]"


def _reported(fn):
    """包在每个工具最内层：计时 / 成败 / 回复正文 / 上报，一处做完。

    🔴 必须在 `_graceful_timeout` **内层**（装饰器列表里写在它下面）：超时被转成一句
    可读消息之前，这里要先看到原始异常，否则超时会被记成 success=True——那正是
    CI-55 想看见的信号。
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        token = _log_slot.set(None)
        t0 = time.monotonic()
        success, error_msg, result = True, None, None
        try:
            result = await fn(*args, **kwargs)
            return result
        except Exception as e:  # noqa: BLE001 — 记完再抛，行为不变
            success, error_msg = False, _error_text(e)
            raise
        finally:
            slot = _log_slot.get()
            if slot is not None:
                # 只能降级：工具报的失败与「抛了异常」取与
                ok = success and slot.get("success", True)
                err = error_msg or slot.get("error_message")
                await _log_call(
                    slot["tool_name"], slot["chemicals"],
                    int((time.monotonic() - t0) * 1000), ok, err,
                    slot["input_params"], _response_text(result),
                )
            _log_slot.reset(token)
    return wrapper


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


def _detail_text(res: "httpx.Response", limit: int = 400) -> str:
    """把后端 4xx 的 `detail` 压成一句可行动的话。

    FastAPI/pydantic 的 422 是 `{"detail": [{"loc": [...], "msg": "...", ...}, ...]}`，
    HTTPException 则是 `{"detail": "一句话"}`——两种都要认。

    🔴 解析失败时**不把原始响应打出来**（memory: `ps-leaks-credentials-from-command-lines`
    的同族——响应体可能带凭证或客户文档字节），只说「后端没给出原因」并附状态码。
    🔴 截断到 400（不是复用 `_cap` 的 20000——那是**日志**的预算）：这句话会进工具返回值，
    调用方读的是它，塞进去一整个响应体只会把真正的原因埋掉。
    """
    def _short(text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit] + "…"

    try:
        detail = res.json().get("detail")
    except Exception:  # noqa: BLE001 — 见上：不打印原始响应
        return f"backend gave no machine-readable reason (HTTP {res.status_code})"
    if isinstance(detail, str) and detail.strip():
        return _short(detail.strip())
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            # loc 形如 ["body", "chemicals"] ⇒ 只留字段名那截，"body" 对调用方没信息量
            loc = ".".join(str(x) for x in (item.get("loc") or []) if x != "body")
            msg = str(item.get("msg") or "").strip()
            parts.append(f"{loc}: {msg}" if loc and msg else (msg or loc))
        joined = "; ".join(p for p in parts if p)
        if joined:
            return _short(joined)
    return f"backend gave no machine-readable reason (HTTP {res.status_code})"


def _raise_for_status_with_reason(res: "httpx.Response") -> None:
    """`raise_for_status()` 的替身：422 带上后端说的原因（CI-410）。

    🔴 存在的理由是**别的路径不走 `_billed_json`**：`_build_audit_session` 的三步与
    `upload_msds_pdf` 直接打 `/sessions*`，此前它们的 422 仍是裸状态行 —— 同一张票要修的
    同一种缺陷，只是在两个不路由到计费包装的工具上。review 抓到的完整性缺口。
    """
    if res.status_code == 422:
        raise RuntimeError(f"Request rejected (422): {_detail_text(res)}")
    res.raise_for_status()


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
    # CI-410：pydantic 把「为什么不合法」放在响应体的 `detail` 里，而这条错误路径此前
    # 从不读它 ⇒ 调用方只拿到 `Client error '422 Unprocessable Entity' for url …`。
    # 不是哑失败（调用可见地失败了），但**不可行动**：模型看不出是"化学品超过 24 个"
    # 还是"参数名写错了"。同 CI-523 一族——信息在，只是没到达读它的人。
    _raise_for_status_with_reason(res)
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
    sc = result.structured_content
    if sc is not None:
        sc = {**sc, "usage": usage}
    return CallToolResult(content=content, structured_content=sc)


def _strip_usage(data: dict) -> dict:
    """Drop the internal `_usage` key that _billed_json attaches, so lookup tools
    that expose `structured_content=data` don't leak it into the client output.
    (Value tools build their own structuredContent + surface a clean `usage` block.)"""
    if not isinstance(data, dict) or "_usage" not in data:
        return data
    return {k: v for k, v in data.items() if k != "_usage"}


# ---------------------------------------------------------------------------
# CI-342：structuredContent 从「白名单」翻成「透传 + 显式挡掉的键」
# ---------------------------------------------------------------------------
# 旧写法是逐字段手抄的 dict：后端往响应里加字段，我们**不会带上，也不会报错**，
# 客户端侧就是「这个字段不存在」。实测丢掉的（2026-08-15，真调后端 + 真调工具做的差集）：
#   顶层 `unresolved_detail`（compat / risk / batch）——`unresolved` 只给了名字，
#     而**为什么没解析出来**的机器可读 `code` 全在这个键里
#   `compat.pairs[]` / `batch.compatibility.pairs[]` 丢 `cas_a`/`cas_b`/`citation`/
#     `source_detail`/`verdict`（batch 还多丢 `source`）——11 个字段只透出 5 个
#   `risk.warnings[]` 丢 `additional_hazards`（带 supplier + revision_date）
#   `get_sds_document` 丢 `physical_form`/`physical_form_disclosure`
#   `search_msds_online` 丢 `status`/`completeness`/`chemical_name`
#
# 🔴 票里担心「白名单也承担着不外泄内部字段的职责，别一把梭透传」——**对实测到的这批
# 不成立**，逐个看过值：`citation` 是 `CAMEO:1x10-acid-strong-base-strong`（公开引用）、
# `source_detail` 引的是 CAMEO 的分类规则、`additional_hazards` 带 supplier 与修订日期
# ——全是可追溯性数据，正是我们要给出去的东西，没有一个泄露内部机制。
# 但「今天这批没问题」不等于「以后都没问题」⇒ 不做裸透传，走这个 helper：**默认全透，
# 要挡的键必须写进 `drop`**，于是「决定不给」这件事永远是显式的、可 grep 的。
_INTERNAL_KEYS = frozenset({"_usage"})

# CI-592：`/quick-chat` 的载荷除了 `_usage` 还带一个 `_timed_out`——那是 `_quick_chat`
# 自己在超时兜底时打的标记，工具函数用它记 `_log_intent`，对客户端没有意义（超时这件事
# 已经写在 `answer` 里）。🔴 它必须**显式**出现在这里：`_expose` 默认全透，漏掉就会把一个
# 下划线开头的内部标记发给客户端。
_QUICK_INTERNAL_KEYS = _INTERNAL_KEYS | frozenset({"_timed_out"})


def _expose(data: dict, *, rename: dict[str, str] | None = None,
            drop: frozenset[str] = _INTERNAL_KEYS, override: dict | None = None) -> dict:
    """把后端的一层 dict 透给客户端：默认全给，只挡 `drop` 里的键。

    `rename` 处理**有意的**对外命名（如 `chem1` → `chemical_a`：MCP 面的命名是对外契约，
    改它会打断现有客户端）。`override` 覆盖我们自己算过的值（如归一化后的 traceability）。
    """
    if not isinstance(data, dict):
        return data
    rename = rename or {}
    out = {rename.get(k, k): v for k, v in data.items() if k not in drop}
    if override:
        out.update(override)
    return out


async def _direct_compat(chemicals: list[str], lang: str | None = None) -> dict:
    """POST /api/v2/compatibility/check — direct service layer, bounded LLM fallback."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/compatibility/check",
            json={"chemicals": chemicals, "lang": _normalize_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_risk(chemicals: list[str], lang: str | None = None) -> dict:
    """POST /api/v2/risk-warnings — direct service layer, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/risk-warnings",
            json={"chemicals": chemicals, "lang": _normalize_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_batch(chemicals: list[str], lang: str | None = None) -> dict:
    """POST /api/v2/batch-safety — combined compat + risk, bounded LLM fallback."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/batch-safety",
            json={"chemicals": chemicals, "lang": _normalize_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_ppe(chemicals: list[str], lang: str | None = None) -> dict:
    """POST /api/v2/ppe-recommendation — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/ppe-recommendation",
            json={"chemicals": chemicals, "lang": _normalize_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_storage(chemicals: list[str], lang: str | None = None) -> dict:
    """POST /api/v2/storage-guidance — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/storage-guidance",
            json={"chemicals": chemicals, "lang": _normalize_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_emergency(chemical: str, scenario: str, lang: str | None = None) -> dict:
    """POST /api/v2/emergency-response — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/emergency-response",
            json={"chemical": chemical, "scenario": scenario, "lang": _normalize_lang(lang or LANG)},
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


# CI-523: the coverage caveat is part of the ANSWER, not decoration — it is what
# stops "we found EU entries" from being read as "cleared everywhere else". It must
# therefore appear on BOTH result branches (hits and no hits), and it must follow the
# caller's language: quick-chat used to answer zh callers in Chinese, so rendering it
# in English only would be a regression this ticket introduced. en/zh only — that is
# what `_normalize_lang` clamps to and what the backend actually supports (CI-356).
_REG_LIST_COVERAGE_NOTE = {
    "en": ("Coverage note: this is our curated copy of the 23 lists, not a live "
           "regulatory feed, and it holds no Taiwan and no IARC data. An absent list "
           'means "not found in our copy", never "not regulated".'),
    "zh": "覆盖范围说明：这是我们整理的 23 份清单副本，不是实时监管数据源，"
          "且不含台湾与 IARC 数据。某份清单没命中只代表「我们这份副本里没有」，"
          "绝不等于「不受监管」。",
}
_REG_LIST_STRINGS = {
    "en": {"title": "Regulatory Lists", "not_checked": "⚠️ **Not checked.**",
           "status": "Status", "not_found": "Not found in the database.",
           "near": "Near matches in the database", "cas": "CAS",
           "count": "Matching lists", "unknown": "Unknown list",
           "none": "No match in our copy of the 23 lists."},
    "zh": {"title": "监管清单", "not_checked": "⚠️ **未核查。**",
           "status": "状态", "not_found": "库中未收录。",
           "near": "库中的近似命中", "cas": "CAS",
           "count": "命中清单数", "unknown": "未知清单",
           "none": "在我们那份 23 清单副本中没有命中。"},
}


def _format_regulatory_lists(data: dict, chemical: str, lang: str | None = None) -> str:
    """Render `/api/v2/regulatory-lists` (CI-523). Pure function so the three
    branches below are testable without a backend.

    🔴 Three branches, and the two failure ones are the reason this tool stopped
    going through an LLM at all:
      - `lists_unavailable` → say we did NOT check. An empty list rendered without
        this is read as "on no watch list", which is the opposite of what happened
        (CI-507).
      - unresolved → say which kind of "we don't have it" this is (CI-375),
        including near matches, so the caller can retry with a CAS instead of
        dead-ending.
      - resolved with zero hits → "not found in our copy", never "not regulated".
    """
    lg = _normalize_lang(lang or LANG)
    s = _REG_LIST_STRINGS.get(lg, _REG_LIST_STRINGS["en"])
    lists = data.get("lists") or []
    lines = [f"**{s['title']} — {data.get('chemical') or chemical}**\n"]

    if data.get("lists_unavailable"):
        lines.append(f"{s['not_checked']} {data.get('error') or ''}".rstrip())
        return "\n".join(lines)

    if not data.get("cas"):
        lines.append(f"**{s['status']}:** {data.get('error') or s['not_found']}")
        near = data.get("near_matches") or []
        if near:
            lines.append(f"**{s['near']}:** {', '.join(near)}")
        return "\n".join(lines)

    lines.append(f"**{s['cas']}:** {data.get('cas')}")
    lines.append(f"**{s['count']}:** {data.get('count', len(lists))}\n")
    if lists:
        lines.extend(
            f"- **{entry.get('list') or s['unknown']}** ({entry.get('region') or '—'})"
            for entry in lists
        )
    else:
        lines.append(s["none"])
    lines.append(f"\n> {_REG_LIST_COVERAGE_NOTE.get(lg, _REG_LIST_COVERAGE_NOTE['en'])}")
    return "\n".join(lines)


async def _direct_regulatory_lists(chemical: str, lang: str | None = None) -> dict:
    """POST /api/v2/regulatory-lists — direct list lookup, no LLM (CI-523)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/regulatory-lists",
            json={"chemical": chemical, "lang": _normalize_lang(lang or LANG)},
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


async def _direct_sds_section(chemical: str, section: int, lang: str | None = None) -> dict:
    """POST /api/v2/sds-section — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/sds-section",
            json={"chemical": chemical, "section": section,
                  "lang": _normalize_lang(lang or LANG)},
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


async def _build_audit_session(experiment_name: str, chemicals: list[str]) -> dict:
    """Create a session, attach the chemicals, run the analysis. Returns the raw parts.

    Extracted so `get_audit_report()` (no session_id — CI-174) builds sessions the
    exact same way `create_audit_session` does. 🔴 Two implementations of a
    three-call sequence is how the two paths start producing different reports.
    """
    # 🔴 TIMEOUT_MULTI 而不是 TIMEOUT：第 3 步是 pairwise 兼容性分析（O(n²) 且带 LLM 兜底），
    # 本来就属于「多组分」预算。此前挂在 15s 上是 `create_audit_session` 一直带着的隐患，
    # 而 CI-174 的零参路径会自动喂进最多 12 个化学品，把它变成常态。
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        # 1. Create the session (bound to the API key owner)
        res = await client.post(
            f"{API_URL}/sessions",
            json={"experiment_name": experiment_name, "source": "mcp"},
            headers=_headers(),
        )
        _raise_for_status_with_reason(res)
        session_id = res.json()["session_id"]

        # 2. Persist chemicals as MsdsRecord (so the report PDF has data)
        res = await client.post(
            f"{API_URL}/sessions/{session_id}/chemicals",
            json={"chemicals": chemicals},
            headers=_headers(),
        )
        _raise_for_status_with_reason(res)
        chem_result = res.json()

        # 3. Run compatibility + risk analysis (reads CAS from MsdsRecord)
        res = await client.post(
            f"{API_URL}/sessions/{session_id}/compatibility",
            json={},
            headers={**_headers(), "Accept-Language": LANG},
        )
        _raise_for_status_with_reason(res)
        compat = res.json()

    return {"session_id": session_id, "chemicals": chem_result, "compatibility": compat}


async def _direct_alternatives(chemical: str, use_case: str = "", lang: str | None = None) -> dict:
    """POST /api/v2/chemical-alternatives — 确定性 curated 替代表，不走 LLM（CI-137）。"""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/chemical-alternatives",
            json={"chemical": chemical, "use_case": use_case or "general",
                  "lang": _normalize_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


def _format_alternatives(data: dict, chemical: str) -> str:
    """渲染确定性替代品结果（CI-137）。纯函数，便于逐分支测。

    🔴 三件必须原样出现在文本里，别让它们只留在 structuredContent：
      - `note`：curated 表的边界（「没有硬编码替代品」也是一种答案，不是空）
      - `risk_level`：替代建议的**前提**——不知道原物质多危险，"更安全"就没有意义
      - `source_info`：CI-65 的可追溯性红线（供应商 + 版本）
    """
    alts = data.get("alternatives") or []
    lines = [f"**Safer alternatives — {data.get('chemical') or chemical}**"]
    cas = data.get("cas_number")
    if cas:
        lines.append(f"**CAS:** {cas}")
    else:
        # 🔴 身份没定下来时**必须显著说出来**：curated 表的兜底分支会拿用户原串去撞 CAS 键
        # （`"50" in "50-00-0"` 为真），于是可能返回甲醛的替代品而 `cas_number` 是空的。
        # 不说这句，用户看到的是「一份针对某物质的替代清单」，而我们根本没认出那是什么。
        lines.append(
            "⚠️ **We could not identify this substance** (no CAS resolved). "
            "Anything below is generic hazard-reduction advice, **not** a substitution "
            "recommendation for a confirmed substance — confirm the identity first.")
    if data.get("risk_level"):
        lines.append(f"**Risk level of the original:** {data['risk_level']}")
    src = data.get("source_info") or {}
    if src.get("supplier"):
        lines.append(
            f"**Source:** {src.get('supplier')}"
            + (f" · revision {src['revision_date']}" if src.get("revision_date") else "")
        )
    # 🔴 CI-226 的两条披露：风险等级是**从某一份 SDS 推出来的**，而那份 SDS 可能是替代品
    # 或不同浓度的。丢掉它们，等于把一个有前提的判断说成无条件的。
    if src.get("substitution"):
        lines.append(f"> ⚠️ Risk level derived from a substituted SDS: {src['substitution']}")
    if src.get("concentration_mismatch"):
        lines.append(
            f"> ⚠️ Concentration mismatch vs the SDS used: {src['concentration_mismatch']}")
    lines.append("")
    if alts:
        for a in alts:
            name = a.get("name") or "Unknown"
            a_cas = f" (CAS {a['cas']})" if a.get("cas") else ""
            lines.append(f"- **{name}**{a_cas}")
            # 🔴 字段名照后端抄（`chemical_substitution.py` 的 `alternatives.append`）：
            # `rationale` / `trade_offs` / `risk_level`。初版按想当然写了 `reason`/`trade_off`
            # ——那正是 CI-529 刚栽过的「键名是我编的」，两次都发生在同一天。
            for key, label in (("rationale", "Why safer"),
                               ("risk_level", "Alternative risk"),
                               ("trade_offs", "Trade-offs")):
                if a.get(key):
                    lines.append(f"  - {label}: {a[key]}")
    else:
        # 后端在没有 curated 映射时会给一条通用建议（`name` 形如
        # "No hardcoded substitution available"），所以真正的空列表极少见——
        # 但空了就说空，别渲染成"有建议"。
        lines.append("No alternative could be produced for this chemical.")
    if data.get("note"):
        lines.append(f"\n> {data['note']}")
    return "\n".join(lines)


async def _direct_recent_chemicals(days: int = 7, limit: int = 12) -> dict:
    """GET /api/v2/recent-chemicals — what this caller analysed recently (CI-174)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.get(
            f"{API_URL}/api/v2/recent-chemicals",
            params={"days": days, "limit": limit},
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
    return out


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
    # 🔴 **主句不许声称我们持有一份记录**（CI-679 的第八处，trust 2026-08-28 定）。
    # 旧句是「the SDS record **we hold for this substance** parsed no hazard data」，
    # 而三个消费者（storage / emergency / waste）的「判不了」**都有两种成因**：
    # `direct_service` 里 `resolved`（有 CAS）与 `has_canonical`（另查一次）是**独立判断**，
    # `/emergency-response` 只在 `not resolved` 时早返回 ⇒ **CAS 解析得出但没有 canonical 行时
    # 照样落到这个载荷**。对那一种，旧句是**一句关于我们自己数据的肯定假话**，
    # 且失效方向很坏：用户因此更不会自己去找 SDS。
    # ⚠️ 我一度以为 emergency/waste 只有一种成因、想加个开关只给 storage 用 —— 那是错的，
    # **加开关反而会留下「哪些调用方传了」这份要人记得维护的名单**。
    # 📌 backend 有字面守卫钉这两句话（`test_ci679_no_record_wording.py`），但它**只扫 backend 仓**
    # ⇒ 跨不了仓，这处才活到今天。本仓自己的守卫在 `tests/test_storage_insufficient_disclosure.py`。
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
    reqs = item.get("storage_requirements", [])
    if reqs:
        lines.append("- **Storage requirements:** " + "; ".join(str(r) for r in reqs))
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


@mcp.tool(
    annotations=ToolAnnotations(title="Check Chemical Compatibility", read_only_hint=True, destructive_hint=False, open_world_hint=False),
    structured_output=False,
)
@_graceful_timeout
@_reported
async def check_chemical_compatibility(chemicals: ChemicalList, lang: Lang = None, intent: Intent = None) -> CallToolResult:
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
    error_msg = None
    success = True
    try:
        if len(chemicals) < 2:
            return _text_result("Please provide at least 2 chemicals to check compatibility.")

        data = await _direct_compat(chemicals, lang=lang)
        lines = [f"**Compatibility Check** ({len(chemicals)} chemicals)\n"]

        if data.get("unresolved"):
            lines.extend(_unresolved_block(data, trailing_newline=True))
        lines.extend(_rejected_products_block(data))
        lines.extend(_precursor_disclosure_block(data))
        lines.extend(_no_hazard_basis_block(data))

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
            struct_pairs.append(_expose(
                pair,
                rename={"chem1": "chemical_a", "chem2": "chemical_b"},
                override={"traceability": traceability},   # 我们归一化过，用自己的
            ))

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
        ), data)
    finally:
        _log_intent("check_chemical_compatibility", chemicals,
                        _intent_params({"chemicals": chemicals}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get Chemical Risk Warnings", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_chemical_risk_warnings(chemicals: ChemicalList, lang: Lang = None, intent: Intent = None) -> str:
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
        data = await _direct_risk(chemicals, lang=lang)
        lines = [f"**Risk Warnings** ({len(chemicals)} chemicals)\n"]

        if data.get("unresolved"):
            lines.extend(_unresolved_block(data, trailing_newline=True))
        lines.extend(_rejected_products_block(data))
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
                        _intent_params({"chemicals": chemicals}, intent),
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
    return lines


@mcp.tool(annotations=ToolAnnotations(title="Check Regulatory Compliance", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def check_regulatory_compliance(
    chemicals: ChemicalList,
    regions: Annotated[list[str] | None, Field(
        description='Region codes to check. Valid codes: "EU", "US", "CN", "JP", "KR", '
                    '"CA", "AU", "TW". Omit to check EU + US (the default pair) — the '
                    'response says explicitly which regions were used.',
    )] = None, intent: Intent = None,
) -> str:
    """
    Check multi-region regulatory status for chemicals. Answers TWO separate questions
    per region — do not collapse them (CI-493):

    1. `status` — is it on a RESTRICTION list (SVHC / REACH Annex XVII / CLP Annex VI /
       Prop 65 / China Catalogue of Hazardous Chemicals / JP CSCL / SG EPMA)?
         `restricted`     on at least one restriction list, or a CMR hazard code
         `detected`       only indirect evidence (occupational exposure limit, or an SDS
                          Section 15 mention). 🔴 NOT a clearance and NOT a violation.
         `not_restricted` we DID check that region's restriction lists and it is not on
                          them. `details` names which lists were checked. Lists we do not
                          hold were not checked, so this is not a clearance either.
         `unverified`     no check was performed — we hold no restriction list for that
                          region (KR, TW, CA, AU), or the source could not be read.
                          🔴 Never report this as "not regulated".
    2. `inventory` — is it on that region's EXISTING-SUBSTANCE inventory (TSCA / IECSC /
       KECL / DSL / AIIC / REACH registered)? Here the polarity is REVERSED:
         `on_inventory: true`  registered there — about registration, not restriction
         `on_inventory: false` 🔴 the direction that needs attention: a substance absent
                               from the inventory typically needs new-substance
                               notification before import
         `on_inventory: null`  we hold no inventory for that region

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
            lines.extend(_format_region_results(data.get("region_results", [])))
            lines.append("")
        _usage = ({"cost": _usage_cost, "balance": _usage_bal, "reason": _usage_reason}
                  if _usage_bal is not None else None)
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content={
                "chemicals": chemicals,
                "regions": effective_regions,
                "regions_defaulted": not regions,
                "results": results,
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
    lang: Lang = None,
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
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get PPE Recommendation", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_ppe_recommendation(chemicals: ChemicalList, lang: Lang = None, intent: Intent = None) -> str:
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
        data = await _direct_ppe(chemicals, lang)
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
                        _intent_params({"chemicals": chemicals}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Get Storage Guidance", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_storage_guidance(chemicals: ChemicalList, lang: Lang = None, intent: Intent = None) -> str:
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
        data = await _direct_storage(chemicals, lang=lang)
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
                        _intent_params({"chemicals": chemicals}, intent),
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
                    'returns cleanup guidance instead of the antidote.',
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
        # 🔴 CI-567：物质级规程条目单独成段并排在最前。事故形状（2026-08-18 Prod 实调）：
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
        # 🔴 必须渲进**文本**：后端可答率因这一层从 15.5% 升到 79.2%（exposure 场景），
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
            lines.append("\n**Note:** Chemical not found in database — showing general guidance only.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("get_emergency_response", [chemical],
                        _intent_params({"chemical": chemical, "scenario": scenario}, intent),
                    success=success, error_message=error_msg)


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
            limits = item.get("limits", [])
            if limits:
                for lim in limits:
                    source = lim.get("source") or lim.get("authority") or "?"
                    ltype = lim.get("type", "?")
                    value = lim.get("value", "—")
                    unit = lim.get("unit", "")
                    # CI-578: 别叫 `region` —— 它会盖掉调用方传的过滤条件，而 `finally`
                    # 里的 `_log_intent` 在循环之后跑，记进日志的就成了最后一条限值的
                    # region。守卫见 tests/test_ci578_logged_params_not_reassigned.py。
                    lim_region = lim.get("region", "")
                    region_suffix = f" ({lim_region})" if lim_region else ""
                    lines.append(
                        f"- **{source}**{region_suffix}: {ltype} = {value} {unit}".rstrip()
                    )
            else:
                lines.append("- No OEL data found for this chemical.")
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
async def get_transport_classification(chemicals: ChemicalList, intent: Intent = None) -> str:
    """Get UN transport classification for chemicals (dangerous goods shipping).
    Returns UN number, proper shipping name, hazard class, packing group,
    and transport mode details (ADR road, IATA air, IMDG sea).
    Args:
        chemicals: List of chemical names or CAS numbers
    """
    error_msg = None
    success = True
    try:
        data = await _direct_transport(chemicals)
        lines = ["**UN Transport Classification**\n"]
        for item in data.get("results", []):
            lines.append(f"### {item.get('chemical_name', '?')} ({item.get('cas', 'N/A')})")
            lines.extend(_form_disclosure_lines(item))  # CI-572
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
            lines.extend(_unresolved_block(data))
        if not data.get("results"):
            lines.append("No transport-classification data found for the given chemicals.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("get_transport_classification", chemicals,
                        _intent_params({"chemicals": chemicals}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Create Audit Session", read_only_hint=False, destructive_hint=False, open_world_hint=False), structured_output=False)
@_reported
async def create_audit_session(
    experiment_name: Annotated[str, Field(
        description='Short human-readable label for the audit, shown on the report, e.g. '
                    '"Grignard prep — 2026-04-16" or "Solvent screening #3".',
    )],
    chemicals: ChemicalList, intent: Intent = None,
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

        built = await _build_audit_session(experiment_name, chemicals)
        session_id, chem_result, compat = (
            built["session_id"], built["chemicals"], built["compatibility"])

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
            structured_content=structured,
        )
    finally:
        _log_intent("create_audit_session", chemicals,
                        _intent_params({"experiment_name": experiment_name, "chemicals": chemicals}, intent),
                    success=success, error_message=error_msg)


# 🔴 `read_only_hint=False`：零参路径会建 session、跑分析、落库（CI-174）。宿主会**自动放行**
# 标为只读的工具，而这个工具已经不是只读的了，也不是幂等的（问两次＝两个 session、两次分析）。
@mcp.tool(annotations=ToolAnnotations(title="Get Audit Report", read_only_hint=False, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_audit_report(session_id: Annotated[str | None, Field(
    description='The session id returned by `create_audit_session`, e.g. "DEMO-A1B2C3D4". '
                'OMIT IT to report on what this user has already analysed in the last 7 '
                'days — no session needed, nothing to restate.',
)] = None) -> str:
    """
    Get a short-lived signed URL to download an archivable PDF safety report.

    Two ways to call it:
    - **No arguments** — builds the report from the chemicals this user has analysed
      over the last 7 days (compatibility / risk / regulatory / protocol checks).
      Use this whenever the user asks for "a report", "something for the file",
      "a document for my PI / safety officer" and you do not already hold a
      session id. You do NOT need to ask them to list the chemicals again.
    - **With a session_id** — the classic path, for a session you just created
      with `create_audit_session`.

    The PDF contains the chemicals, compatibility matrix, risk warnings and session
    metadata, and is signed, so it can be filed as a compliance record.

    Args:
        session_id: Optional. The session id returned by `create_audit_session`,
                    e.g. "DEMO-A1B2C3D4". Omit to report on recent analyses.

    Returns:
        A signed URL valid for ~5 minutes. The session must be owned by the
        API key's user (MSDS_API_KEY).
    """
    error_msg = None
    success = True
    built_from_recent: list[str] = []
    not_in_report: list[str] = []
    try:
        if not get_caller_credential():
            return "get_audit_report requires an authenticated API key (MSDS_API_KEY for stdio, or gateway auth for remote)."

        if not session_id:
            # CI-174: the report used to be reachable only through a session id the
            # caller never had. Measured on Prod (2026-08-16): the "call
            # create_audit_session if you want a signed report" hint has been on
            # batch_safety_check since the day it shipped — 60 external calls,
            # 6 external users, ZERO sessions created. So the missing piece was not
            # another hint, it was this step. Build the session from what they have
            # already analysed instead of asking them to say it again.
            # 🔴 12 而不是后端上限 24：兼容性分析是 pairwise，24 个＝276 对，串行跑 + LLM
            # 兜底会稳稳超时；12 个＝66 对。用户没指定范围时，宁可少覆盖也别给他一个超时。
            try:
                recent = await _direct_recent_chemicals(limit=12)
            except httpx.HTTPStatusError as e:
                # 两个仓独立部署 ⇒ MCP 先上时后端还没有这个端点。别把 404 抛成裸异常：
                # 那正好发生在我们刚把所有模型都指向零参调用之后。
                if e.response is not None and e.response.status_code == 404:
                    success, error_msg = False, "recent-chemicals endpoint unavailable"
                    return (
                        "Reporting on recent analyses is not available on this server yet. "
                        "Call `create_audit_session(name, chemicals)` and then "
                        "`get_audit_report(session_id)`."
                    )
                raise
            chemicals = recent.get("chemicals") or []
            if not chemicals:
                # 🔴 不标 success=False：这是一句正常的答案（新用户必然先撞它），
                # 标成失败会让 `get_audit_report` 的错误率被新用户淹掉——刚清理过
                # 79 条自造失败的那张表，别马上又往里灌。
                return (
                    "No analyses found in the last 7 days to report on.\n\n"
                    "Run a safety check first (e.g. `batch_safety_check` or "
                    "`check_chemical_compatibility`), then ask for the report again — "
                    "or call `create_audit_session(name, chemicals)` to report on a "
                    "specific list of chemicals."
                )
            built = await _build_audit_session(
                f"Recent MCP analyses ({len(chemicals)} chemicals)", chemicals)
            session_id = built["session_id"]
            # 🔴 覆盖范围只能报**后端真的收进去的**那些。请求了 5 个、库里认得 3 个时，
            # 说「涵盖 5 个」＝让用户把一份不含另外两个的签名文件当成含了。
            added = built.get("chemicals", {}).get("added") or []
            built_from_recent = [c["name"] for c in added
                                 if c.get("status") in ("added", "already_added")]
            not_in_report = built.get("chemicals", {}).get("not_found") or []

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
            _raise_for_status_with_reason(res)
            relative = res.json()["url"]

        full_url = relative if str(relative).startswith("http") else f"{API_URL}{relative}"
        lines = [f"**Signed report URL** (valid ~5 min):\n{full_url}\n"]
        if built_from_recent:
            # 🔴 Say what went in. A signed compliance record whose scope the user
            # never stated must state its own scope, or they will file a document
            # believing it covers work it does not.
            lines.append(
                f"Built from your last 7 days of analyses — covers "
                f"{', '.join(built_from_recent)}."
            )
            if not_in_report:
                lines.append(
                    f"⚠️ **Not in this report:** {', '.join(not_in_report)} — we hold no "
                    f"record for these, so they are absent from the PDF. Do not read the "
                    f"report as covering them."
                )
            lines.append(
                "Want a different set? Call `create_audit_session(name, chemicals)` and "
                "report on that session."
            )
        lines.append("Open in a browser or `curl -O` to download the PDF.")
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content={
                "session_id": session_id,
                "report_url": full_url,
                "expires_in_seconds": 300,
                "built_from_recent_analyses": bool(built_from_recent),
                "chemicals": built_from_recent or None,
                "chemicals_not_in_report": not_in_report or None,
            },
        )
    finally:
        # 🔴 把「走的是哪条路径」记下来：这次改动要回答的正是「零参调用有没有被用起来」，
        # 两条路径记成同一个形状的话，下一轮读日志的人读不出答案。
        _log_intent("get_audit_report", built_from_recent or None,
                        _json.dumps({"session_id": session_id,
                                     "built_from_recent": bool(built_from_recent)}),
                    success=success, error_message=error_msg)


def _unresolved_block(data: dict, *, trailing_newline: bool = False) -> list[str]:
    """CI-470 phase 3b: render WHY each name was unresolved, not just that it was.

    The backend has always known the difference between "we hold no record",
    "a lookup step failed so this absence is not trustworthy" and "the name and
    the CAS you gave contradict each other, so we refused" — and until now this
    surface flattened all three into one line of bare names. A model reading
    `**Unresolved:** hydrofluoric acid` reasonably concludes we have no data and
    tells the user so; that conclusion is one we are not entitled to when the
    real cause was a failed lookup.

    🔴 Falls back to the plain name list whenever the backend did not send
    `unresolved_detail` (older backend, or an endpoint that does not build it
    yet). Silence must never turn into an invented reason.
    """
    names = data.get("unresolved") or []
    if not names:
        return []
    tail = "\n" if trailing_newline else ""
    detail = {d.get("query"): d for d in (data.get("unresolved_detail") or [])
              if isinstance(d, dict)}
    if not detail:
        return [f"**Unresolved:** {', '.join(names)}{tail}"]
    lines = ["**Unresolved:**"]
    for name in names:
        d = detail.get(name)
        # English surface: this server renders in English (see the tool
        # descriptions); `reason_en` is the backend's own English sentence, and
        # `reason` (zh) is deliberately NOT used as a fallback — a Chinese
        # sentence dropped into an English answer reads as a rendering bug.
        why = (d or {}).get("reason_en")
        lines.append(f"- **{name}**" + (f" — {why}" if why else ""))
    return lines + ([""] if trailing_newline else [])


def _rejected_products_block(data: dict) -> list[str]:
    """CI-277: render the backend's explicit product/mixture refusals.

    🔴 Why this must never be silently dropped: the backend removes formulated
    products from BOTH `name_to_cas` and `unresolved` and reports them only in
    `rejected_products`. A renderer that ignores the key shows "2 chemicals
    submitted, 0 unresolved, 0 incompatible pairs" for
    `["Windex", "hydrochloric acid"]` — an unearned all-clear for a pair that
    was never evaluated at all. Absence of a verdict must read as absence, not
    as safety.
    """
    rejected = data.get("rejected_products") or []
    if not rejected:
        return []
    lines = [
        "**⚠️ Not evaluated — formulated products (mixtures):**",
    ]
    for r in rejected:
        name = r.get("query") or "(unnamed)"
        lines.append(
            f"- **{name}** — a formulated product. Its hazard classification "
            f"applies to the whole formulation, so it is NOT accepted as an input "
            f"to compatibility or batch safety checks, and it was NOT substituted "
            f"by any of its components. Its SDS is retrievable with "
            f"`get_sds_document(\"{name}\")`."
        )
    lines.append(
        "These were **excluded from the results below** — treat any conclusion "
        "here as covering only the other inputs.\n"
    )
    return lines


def _no_hazard_basis_block(data: dict) -> list[str]:
    """CI-666: render the backend's "we matched a record but it has no hazard data" note.

    🔴 **Without this the fix does not reach the consumer that produced the bug report.**
    The Prod repro was `get_chemical_risk_warnings(["carbon disulfide"])` over MCP:
    the response was fully-formed and completely empty, and the only thing the model
    saw in `TextContent` was `"No risk warnings found for the given chemicals."` — which
    reads as "we checked, there are none". The backend now publishes a top-level
    `no_hazard_basis` list saying *why*; `_expose()` carries it into structuredContent
    for free, **but the model reads TextContent**, which is assembled field-by-field.
    Same shape of miss as CI-553/CI-562 for `precursor_disclosure`.

    🔴 The wording is rendered backend-side (5 languages, single source in the i18n
    catalog) — do NOT re-phrase it here or a second, unversioned copy starts drifting.
    That wording deliberately names the matched CAS: the substance-level answer can
    still be wrong (a name can match the wrong record), and the CAS is the only clue
    the caller has to notice that.

    🔴 A non-dict entry must not take down the whole safety answer — same guard, and
    same reason, as `_precursor_disclosure_block` / `_unresolved_block`.

    🔴 **天花板：渲染进 TextContent ≠ 用户读到。** [[CI-592]] 与 [[CI-523]] 都实测过——
    工具文本要经过 claude.ai / Copilot 那一层**重写**才到用户眼前，而结构化披露会被
    summary LLM 复述掉。所以这个 block 让披露**有机会**到达，不保证到达。
    ⇒ 别在票里、也别对外把「加了渲染线」说成「修复已到达消费者」；真要确定性，
    得走那条我们自己控制渲染的通道（web 快聊 / Slack / Teams 的 `rich_message.py`）。
    """
    entries = [e for e in (data.get("no_hazard_basis") or []) if isinstance(e, dict)]
    if not entries:
        return []
    lines = [
        "**⚠️ Some chemicals matched a record that carries no hazard data — "
        "this is NOT a finding that they are safe.**",
    ]
    for e in entries:
        name = e.get("query") or e.get("cas") or "Unknown"
        reason = e.get("reason_en") or e.get("reason") or ""
        lines.append(f"- **{name}** (CAS {e.get('cas', 'n/a')}): {reason}")
    return lines


def _batch_not_analysed(data: dict, submitted: list[str]) -> list[str]:
    """调用方提交了、但**没进分析**的那些名字（CI-570）。

    后端在解析之后按 `MAX_BATCH_CHEMICALS` 截断，被丢掉的输入在 `data["chemicals"]` 里
    一条都不出现——analysed / unresolved / rejected 三类都没有 ⇒ 差集就是它们。

    🔴 差集之所以敢做：后端 `_resolve_all` 用**调用方原样的字符串**（strip 后）当 key，
    不是解析后的规范名。若它换成规范名，`"67-64-1"` 会被算成「没进分析」——而那是一个
    看起来很权威的**假警报**，比不提示更糟。改后端那一侧的人：这里会跟着错，且不报错。
    """
    if not data.get("truncated"):
        return []
    # 🔴 **按大小写敏感比**（review 抓到的）：后端 `name_to_cas[name]` 保留原样大小写，
    # 所以 `"Acetone"` 和 `"acetone"` 在它那边是**两个 key**。这边若折叠成小写，
    # 「留下 Acetone、丢掉 acetone」时那个被丢的会被当成已入账 ⇒ `truncated=true` 配
    # `not_analysed=[]`，正是本票要防的那句「我们什么都没丢」。后端已经 strip 过，
    # 所以这里只 strip、不 lower。
    accounted = {
        (e.get("name") or "").strip()
        for e in (data.get("chemicals") or []) if isinstance(e, dict)
    }
    out: list[str] = []
    seen: set[str] = set()
    for raw in submitted:
        name = (raw or "").strip()
        if not name or name in accounted or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _batch_truncation_block(data: dict, submitted: list[str]) -> list[str]:
    """CI-570：提交了 13–20 个时，超出 12 的那些**既不在结果里、也没有任何提示**。

    与 CI-277 同一个形状：**没有结论被读成没有危害**。所以这段话要说清的不是「我们只算了
    12 个」，而是「下面的一切——**包括没有警告这件事**——都不涉及这几个」。
    """
    if not data.get("truncated"):
        return []
    dropped = _batch_not_analysed(data, submitted)
    lines = [
        "🔴 **This request exceeded the batch size limit, so not every submitted "
        "chemical was analysed.**",
    ]
    if dropped:
        # 🔴 作用域必须收窄到「相容性 / 风险」两节（review 抓到的）：受管制前体披露是
        # **在截断之前**算的（后端有意为之），它**能**点到被丢掉的那个物质。原文写
        # 「nothing below says anything about them」会让读者把两行之下那条真正适用的
        # 披露也当成不适用 —— 那是比不提示更糟的一种错。
        lines.append(
            f"These {len(dropped)} were NOT analysed. The compatibility and risk results "
            f"below cover only the others — **the absence of a warning there says nothing "
            f"about these**:"
        )
        lines.extend(f"- {name}" for name in dropped)
        if data.get("precursor_disclosure"):
            lines.append(
                "(The regulated-precursor notice below is computed on everything you "
                "submitted, so it may name these.)"
            )
        # 🔴 本工具拒收少于 2 个输入 ⇒ 丢掉恰好 1 个（13 送 12 算，最常见的一种）时，
        # 「拿这些再调一次」是一条**保证会被拒**的建议。
        lines.append(
            "Call batch_safety_check again with just those to get their results."
            if len(dropped) > 1 else
            f"This tool needs at least 2 chemicals, so check {dropped[0]} with "
            f"get_chemical_risk_warnings, or re-run the batch with it plus one other."
        )
    else:
        # 🔴 走到这里说明差集丢了东西：被截断的输入在 `data["chemicals"]` 的三类里
        # 一条都不该出现。**要吵，别含糊过去** —— 含糊的措辞会让
        # `_batch_not_analysed` 那条跨仓前提失效时看起来一切正常。
        lines.append(
            "We could not determine which ones. This is a defect on our side, not a "
            "statement that nothing was dropped — treat the results below as covering "
            "an unknown subset of what you submitted, and re-submit in groups of 12."
        )
    lines.append("")
    return lines


def _precursor_disclosure_block(data: dict) -> list[str]:
    """CI-553/CI-562: render the backend's regulated-precursor disclosure into **text**.

    CI-541 added a top-level `precursor_disclosure` key to /compatibility/check,
    /risk-warnings and /batch-safety. `_expose()` passes it through to
    structuredContent for free — but the model reads `TextContent`, and that is
    assembled field-by-field by each renderer, so a key nobody mentions simply
    never appears in the text面 for any of the three.

    ⚠️ Do NOT repeat the CI-553 ticket's claim that `batch_safety_check` "returns a
    bare string so it has no structuredContent" — measured false: it is annotated
    `-> str` but actually returns a CallToolResult carrying `_expose(data)`
    (`test_ci342_structured_passthrough.py` asserts exactly that). All three tools
    reach structured clients; what was missing everywhere is the text面.

    🔴 The disclosure is informational and the answer still stands — Blake's CI-541
    ruling was "answer AND disclose", never "refuse". The wording is rendered
    backend-side (`statement`, 5 languages, single source in the i18n catalog);
    do NOT re-phrase it here or a second, unversioned copy starts drifting.

    🔴 Batch truncation is why the header cannot promise "all of this was analysed":
    the backend computes the disclosure BEFORE the 12-chemical size gate (deliberate
    — a dropped chemical was still submitted and still audited), while this tool
    accepts up to 20. So a disclosed chemical can be one that never entered the
    analysis. When `truncated` is set we say so instead of implying coverage.
    """
    entries = data.get("precursor_disclosure") or []
    if not entries:
        return []
    lines = [
        "**⚠️ Regulated-precursor notice — informational only. This is a listing "
        "notice, not a refusal: the requested analysis was performed and its "
        "results follow below.**",
    ]

    # 🔴 A non-dict entry must not take down the whole safety answer: this block runs
    # before any result rendering, so an AttributeError here replaces the
    # compatibility/risk answer the user asked for with a tool error — strictly worse
    # than the missing disclosure being fixed. `_unresolved_block` guards the same way.
    bad = [e for e in entries if not isinstance(e, dict)]
    entries = [e for e in entries if isinstance(e, dict)]
    for e in bad:
        lines.append(f"- {e} (unrecognised disclosure entry — reported verbatim)")

    # Group by chemical. Everyday bulk chemicals sit on several control lists at once
    # (HCl: UN 1988 Table II + DEA List II + EU 273/2004 Cat 3), and those statements are
    # the same sentence with the list name swapped in — printed one per row they buried
    # the actual answer under seven near-identical paragraphs (measured live on Prod,
    # hydrochloric acid + acetone).
    #
    # 🔴 The safety property is the equality test below, NOT this grouping: a follow-up
    # statement is folded away ONLY if it equals the first one with its own tier
    # substituted in. Anything else is printed in full — acetone's EU 2019/1148 Annex II
    # entry (reportable suspicious transactions) survives that way, and so would two
    # tiers of one regime that differ on a threshold.
    #
    # Grouping by (chemical, regime) was tried first; a mutation run showed it changes
    # no output at all, because the equality test already refuses to fold anything that
    # reads differently. Keeping it would have been a knob that looks protective and
    # isn't — the kind of guard this repo keeps catching itself writing.
    groups: dict[tuple, list[dict]] = {}
    for e in entries:
        key = (e.get("cas") or e.get("query_name"),)
        groups.setdefault(key, []).append(e)

    for key, items in groups.items():
        head = items[0]
        name = head.get("query_name") or head.get("matched_name") or "(unnamed)"
        cas = head.get("cas")
        label = f"**{name}**" + (f" (CAS {cas})" if cas else "")
        statement = (head.get("statement") or "").strip()
        if not statement:
            # 🔴 Never drop an entry just because the rendered sentence is missing:
            # a hit with no `statement` is exactly when the reader most needs to be
            # told something was flagged. Fall back to the machine-readable facets
            # and say plainly that the wording did not arrive.
            facets = " / ".join(
                str(head[k]) for k in ("regime", "tier", "authority") if head.get(k))
            lines.append(
                f"- {label} — listed as a regulated precursor"
                + (f" ({facets})" if facets else "")
                + ". The disclosure text was not returned by the backend; treat this "
                  "as a flagged listing and verify against the cited regime."
            )
            rest = items[1:]
        else:
            lines.append(f"- {label} — {statement}")
            rest = items[1:]

        head_tier = head.get("tier") or ""
        same, different = [], []
        for e in rest:
            stmt = (e.get("statement") or "").strip()
            tier = e.get("tier") or ""
            if statement and stmt and head_tier and tier and \
                    stmt == statement.replace(head_tier, tier):
                same.append(e)
            else:
                different.append(e)
        if same:
            listed = " · ".join(
                (e.get("tier") or "?") + (f" ({e['authority']})" if e.get("authority") else "")
                for e in same)
            lines.append(f"  Also listed on: {listed} — same wording as above.")
        for e in different:
            stmt = (e.get("statement") or "").strip()
            if stmt:
                lines.append(f"- {label} — {stmt}")
            else:
                facets = " / ".join(
                    str(e[k]) for k in ("regime", "tier", "authority") if e.get(k))
                lines.append(
                    f"- {label} — listed as a regulated precursor"
                    + (f" ({facets})" if facets else "")
                    + ". The disclosure text was not returned by the backend; treat this "
                      "as a flagged listing and verify against the cited regime."
                )
    # 🔴 CI-570：截断提示**曾经写在这里**，于是它只在「这批里恰好有受管制前体」时才出现
    # ——一个与截断毫无关系的条件。没有前体命中的普通批次（绝大多数）全程沉默。
    # 现在它是 `_batch_truncation_block`，无条件渲染。别再往这里搬回来。
    lines.append("")
    return lines


@mcp.tool(annotations=ToolAnnotations(title="Search Chemical Database", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_reported
async def search_chemical_database(query: Annotated[str, Field(
    description='A single chemical name, synonym, or CAS number, e.g. "methanol", '
                '"wood alcohol", "67-56-1". Not a natural-language question — use '
                'ask_chemical_safety for those.',
)], intent: Intent = None) -> str:
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
                # 「上传 SDS」这个对他无效的动作（Prod agent 面实测 4/4）。
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
                _no_cas = [c for c in chemicals
                           if (c.get("record_kind") or "") == "substance_no_cas"]
                _ordinary = [c for c in chemicals
                             if (c.get("record_kind") or "") != "substance_no_cas"]
                shown = _ordinary[:5] + _no_cas[:3]
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
                    if kind == "substance_no_cas":
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
                        "included_in_assessment": c.get(
                            "included_in_assessment", kind == "substance"
                        ),
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
            lines.append("**Note:** Chemical not found in database.")
        elif content:
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
            lines.append(f"\n- **Source:** {data['supplier']}{region_suffix}")
            if data.get("revision_date"):
                lines.append(f"- **Revision date:** {data['revision_date']}")
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
        # 实测 p50 **9.7 秒**；而后端 `agent/tools/chemical_substitution.py` 早就是确定性实现
        # （curated 替代表 + `resolve_cas` + GHS 风险比较，全文件零 LLM 引用）。
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
    lang: Lang = None,
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

    🔴 **这串是量出来的，不是写出来的**，改它之前先读 `docs/pm/tickets/CI-613.md`。
    Prod 交错采样（bleach + hydrochloric acid，同一时间窗，判据取 `/quick-chat`
    返回的 `intent`）：

    | 措辞 | 被 RAI 判 `rejected` |
    |---|---|
    | 旧串（`the DANGEROUS order to avoid and what happens if done wrong`） | **4/5**（跨轮累计约 2/3）|
    | 本串 | **0/20**（跨轮累计 0/30）|

    两个改动点，各自单独测过、缺一不可：
    ① 开头换成 RAI 提示词里逐字白名单的形状（"Is it safe to mix …"）。
       **只去掉反向条款而不改开头仍是 3/5** ⇒ 票里原本设想的「方向 1」就此证伪。
    ② 反向那条从祈使式索取（"the DANGEROUS order to avoid and what happens if done
       wrong"）改成判断句（"whether … is unsafe"）。内容一条没丢 ⇒ 不需要在 MCP 层
       确定性渲染反向顺序。
    🔴 **别再去改 RAI 提示词**：两条修法已证伪并回滚（`62a8f174` / `a53bd446`）。
    🔴 0/30 只够说「没看到回归」，不够说「稳」⇒ 拒答兜底照样留着。

    做成独立函数是为了让这串**可被引用而不是被抄写**：golden
    `safety_guard.yaml::safe-order-001` 钉的就是它，跨仓一致性由
    `scripts/cross-repo-consistency-check.py` 看住（抄写过的探针盯不住措辞漂移）。
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
    for pair in pairs:
        level = (pair.get("level") or "unknown").lower()
        tag = {"compatible": "OK", "caution": "CAUTION",
               "incompatible": "DANGER"}.get(level, level.upper())
        lines.append(
            f"- **{pair.get('chem1', chemical_a)}** + **{pair.get('chem2', chemical_b)}**: "
            f"[{tag}] {pair.get('level', 'unknown')}\n"
            f"  Reason: {pair.get('reason', 'N/A')}\n"
            f"  Basis (rule): {pair.get('source', 'unknown')}"
        )
        if level == "incompatible":
            lines.append("  ⚠️ There is **no safe addition order** for an incompatible pair — "
                         "do not combine them in either direction.")
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
    不相容的对不需要这句：那种情况下正确的话是「没有安全的加料顺序」，已单独给出。
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
        return ("\n\n---\n⚠️ **This pair is INCOMPATIBLE — there is no safe addition order.** "
                "Do not combine them in either direction.")
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
    lang: Lang = None,
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
        # 改措辞把误判率从 ~2/3 压到低位，但**压不到 0**（实测：任何写法都还有残余，
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
async def get_waste_disposal(chemicals: ChemicalList, intent: Intent = None) -> str:
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
        data = await _direct_waste(chemicals)
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
                        _intent_params({"chemicals": chemicals}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(
    annotations=ToolAnnotations(title="Compare SDS Versions", read_only_hint=True, destructive_hint=False, open_world_hint=False),
    structured_output=False,
)
@_graceful_timeout
@_reported
async def compare_sds_versions(
    chemical: Chemical,
    supplier: Annotated[str, Field(
        description='Optional SDS supplier/manufacturer, to disambiguate when several '
                    'suppliers\' sheets exist for the same chemical, e.g. "Sigma-Aldrich".',
    )] = "",
    region: Annotated[str, Field(
        description='Optional region code to narrow the lookup, e.g. "US", "EU", "JP", "CN".',
    )] = "", intent: Intent = None,
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
                structured_content=_strip_usage(data),
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
            structured_content=_strip_usage(data),
        )
    finally:
        _log_intent("compare_sds_versions", [chemical],
                        _intent_params({"chemical": chemical, "supplier": supplier, "region": region}, intent),
                    success=success, error_message=error_msg)


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
#
# The remaining gap (this pass): a public HTTPS URL is *also* something a
# remote client rarely has — a PDF the user just uploaded into ChatGPT/claude.ai
# lives in that client's sandbox with no public URL at all. So `pdf_source` now
# also accepts the file bytes inline, base64-encoded, either as a
# `data:application/pdf;base64,...` URI or as a bare base64 string long enough
# to decode to a real PDF (checked via the `%PDF` magic bytes). Resolution
# order: http(s) URL -> data URI -> bare base64 -> local path (last one only
# ever succeeds for a self-hosted stdio server on the same machine as the file).
# ---------------------------------------------------------------------------

_MAX_INLINE_PDF_BYTES = 10 * 1024 * 1024  # 10 MB decoded — inline-upload guardrail
_BASE64_CHARS_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


class _InlinePdfError(ValueError):
    """pdf_source clearly declared itself as inline content (data: URI, or a
    long base64-looking string that decoded to a %PDF) but failed validation.
    Distinct from "doesn't look like inline content at all", which falls
    through to local-path handling instead of raising."""


def _sanitize_upload_filename(name: str | None, fallback: str = "upload.pdf") -> str:
    """Make a caller-supplied filename safe to hand to the upload endpoint.

    The filename travels to POST /sessions/{sid}/upload and is used there to
    build a path on disk, so a raw passthrough of a tool argument would let a
    caller write outside the session directory ("../x", "/etc/y"). Before this
    tool grew a `filename` parameter every filename was machine-derived (URL
    last segment / os.path.basename), so this is a new surface: strip it back
    to a bare basename with a conservative charset, and keep the .pdf suffix
    the backend dispatches on.
    """
    import os as _os
    raw = (name or "").strip().replace("\x00", "")
    raw = raw.replace("\\", "/").split("/")[-1]      # kill both separators, keep basename
    raw = _os.path.basename(raw)
    raw = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
    raw = raw.lstrip(".")                             # no hidden/relative names
    if len(raw) > 120:
        stem, _, _ext = raw.rpartition(".")
        raw = (stem or raw)[:116] + ".pdf"
    if not raw:
        raw = fallback
    if not raw.lower().endswith(".pdf"):
        raw += ".pdf"
    return raw


def _reject_oversize_encoded(payload: str) -> None:
    """Reject before decoding. base64 inflates by 4/3, so an encoded payload
    longer than that bound cannot decode under the cap — checking first keeps a
    multi-GB string from being materialised in memory just to be rejected."""
    max_encoded = (_MAX_INLINE_PDF_BYTES * 4) // 3 + 8
    if len(payload) > max_encoded:
        raise _InlinePdfError(
            f"encoded payload is {len(payload) / 1_048_576:.1f} MB, which cannot "
            f"decode under the {_MAX_INLINE_PDF_BYTES // 1_048_576} MB inline-upload "
            "limit. Host it at a public HTTPS URL instead and pass that URL."
        )


def _validate_inline_pdf(data: bytes) -> None:
    if not data.startswith(b"%PDF"):
        raise _InlinePdfError(
            "decoded content does not start with the PDF magic bytes (%PDF) — "
            "make sure you are base64-encoding the raw PDF file bytes, not "
            "text extracted from it."
        )
    if len(data) > _MAX_INLINE_PDF_BYTES:
        raise _InlinePdfError(
            f"decoded PDF is {len(data) / 1_048_576:.1f} MB, over the "
            f"{_MAX_INLINE_PDF_BYTES // 1_048_576} MB inline-upload limit. "
            "Host it at a public HTTPS URL instead and pass that URL."
        )


def _decode_data_uri_pdf(pdf_source: str) -> bytes:
    """Decode a `data:...;base64,<payload>` URI. Raises _InlinePdfError on any
    failure — a data: prefix is an unambiguous declaration of intent, so
    failures here are real errors, never a signal to fall through."""
    header, sep, payload = pdf_source.partition(",")
    if not sep or "base64" not in header or not payload:
        raise _InlinePdfError(
            "data URI must be base64-encoded, e.g. "
            "data:application/pdf;base64,<...>"
        )
    _reject_oversize_encoded(payload)
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as e:
        raise _InlinePdfError(f"could not base64-decode data URI content: {e}") from e
    _validate_inline_pdf(data)
    return data


def _decode_bare_base64_pdf(pdf_source: str) -> bytes | None:
    """Try to interpret pdf_source as a bare (no `data:` prefix) base64 PDF
    blob. Returns None — never raises — when pdf_source doesn't decode to a
    %PDF payload, so the caller falls through to local-path handling; that is
    the only way a short local path like "a.pdf" keeps working. The one
    exception is a decoded payload that IS a real PDF but over the size cap —
    that's unambiguously inline content, so it raises instead of silently
    trying (and failing) local-path handling next.
    """
    stripped = pdf_source.strip()
    if len(stripped) < 100 or not _BASE64_CHARS_RE.match(stripped):
        return None
    _reject_oversize_encoded(stripped)
    try:
        data = base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data.startswith(b"%PDF"):
        return None
    if len(data) > _MAX_INLINE_PDF_BYTES:
        raise _InlinePdfError(
            f"decoded PDF is {len(data) / 1_048_576:.1f} MB, over the "
            f"{_MAX_INLINE_PDF_BYTES // 1_048_576} MB inline-upload limit. "
            "Host it at a public HTTPS URL instead and pass that URL."
        )
    return data


def _upload_local_path_message(pdf_source: str) -> str:
    return (
        f"❌ Could not read `{pdf_source}`.\n\n"
        "This MCP server runs on MSDS Chain's servers, not on your machine, so it "
        "cannot open files on your computer or inside your chat client's sandbox. "
        "(A local file path only works for a self-hosted stdio server running on the "
        "same machine as the file.)\n\n"
        "Three ways to get this SDS in, best first:\n"
        "1. **Send the bytes inline (do this if you can read the file at all)** — "
        "base64-encode the PDF and call `upload_msds_pdf` again with that string, or "
        "with a `data:application/pdf;base64,<...>` data URI. Max 10 MB decoded. This "
        "is the only route that needs nothing from the user, and it is usually "
        "available: if the file was attached to this conversation, or your sandbox can "
        "open the path you just tried, you already hold the bytes.\n"
        "2. **Public link** — if the PDF has a publicly reachable HTTPS URL (supplier "
        "site, or a share link that needs no login), call `upload_msds_pdf` again with "
        "that URL and it will be fetched and parsed right here.\n"
        "3. **Web upload** — go to https://msdschain.lagentbot.com, sign in with the "
        "same account, and upload the PDF there. This works for any local file.\n\n"
        "Either way the PDF is parsed into structured safety data (chemical name, CAS, "
        "GHS classification, H-codes, PPE, storage, incompatibilities) and stored under "
        "your account, so the other tools here can use it. Contributing an SDS we do "
        "not already hold also earns credits."
    )


@mcp.tool(annotations=ToolAnnotations(title="Upload & Parse MSDS PDF", read_only_hint=False, destructive_hint=False, open_world_hint=False), structured_output=False)
@_reported
async def upload_msds_pdf(
    pdf_source: Annotated[str, Field(
        description="The PDF itself, in one of three forms, tried in this order: "
                    "(1) a publicly reachable HTTPS URL, fetched server-side; "
                    "(2) inline base64 of the raw PDF bytes — either a data URI "
                    "(`data:application/pdf;base64,<...>`) or a bare base64 string, max "
                    "10 MB decoded — use this whenever you already hold the file's bytes, "
                    "which is the normal case for hosted clients like ChatGPT/claude.ai; "
                    "(3) a local file path, which works ONLY for a self-hosted stdio "
                    "server on the same machine as the file. Never pass a path from the "
                    "user's machine or your own sandbox to the hosted server — it does "
                    "not exist there.",
    )],
    session_id: Annotated[str | None, Field(
        description="Existing audit session id to attach this upload to. Omit to create "
                    "a new session automatically.",
    )] = None,
    experiment_name: Annotated[str, Field(
        description="Label for the auto-created session. Ignored when session_id is given.",
    )] = "MCP Upload",
    filename: Annotated[str | None, Field(
        description="Display filename, used when pdf_source is inline base64 (which "
                    'carries no filename of its own). Defaults to "upload.pdf".',
    )] = None,
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
        pdf_source:      One of, tried in this order:
                         1. A publicly reachable HTTPS URL of the PDF (fetched
                            server-side).
                         2. **Inline file bytes, base64-encoded** — use this
                            when you already have the PDF's bytes in this
                            conversation (e.g. the user just uploaded a PDF to
                            you and you can read/attach it), which is the
                            common case for remote clients like ChatGPT or
                            claude.ai where the file lives in YOUR sandbox with
                            no public URL. Pass either a data URI
                            (`data:application/pdf;base64,<...>`) or a bare
                            base64 string of the raw PDF bytes. Max 10 MB
                            decoded.
                         3. A local file path (e.g. "/tmp/acetone_sds.pdf") —
                            works ONLY for a self-hosted stdio server running
                            on the same machine as the file. Do NOT pass a path
                            from the user's machine or a client-side sandbox to
                            the hosted server; it will not exist there. If you
                            cannot get a URL or the raw bytes, send the user to
                            the web uploader instead.

                         Rule of thumb: if you (the model) can see/hold the PDF's
                         bytes right now, base64-encode them and pass that —
                         don't go looking for a local path that only exists on
                         the user's machine, not the server's.
        session_id:      Existing session ID to attach this upload to. If omitted,
                         a new session is created automatically.
        experiment_name: Label for the auto-created session (ignored if
                         session_id is provided). Defaults to "MCP Upload".
        filename:        Optional display filename, used when pdf_source is
                         inline base64 content (which has no filename of its
                         own). Defaults to "upload.pdf" if omitted.

    Returns:
        Parsed chemical info (name, CAS, risk level, key fields) and session_id.
        If parsing partially failed, missing fields are listed so you can follow
        up with `ask_chemical_safety` for the gaps.
    """
    error_msg = None
    success = True
    parsed_chemicals: list[str] = []
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

        # 1. Resolve PDF bytes: http(s) URL -> data URI -> bare base64 -> local path
        import os as _os
        pdf_bytes: bytes
        resolved_filename: str

        if pdf_source.startswith("http://") or pdf_source.startswith("https://"):
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as dl:
                resp = await dl.get(pdf_source)
                _raise_for_status_with_reason(resp)
                pdf_bytes = resp.content
                # Derive filename from URL path
                url_path = pdf_source.rstrip("/").split("?")[0]
                resolved_filename = _sanitize_upload_filename(
                    filename or url_path.split("/")[-1]
                )
        elif pdf_source.startswith("data:"):
            try:
                pdf_bytes = _decode_data_uri_pdf(pdf_source)
            except _InlinePdfError as e:
                success = False
                error_msg = f"invalid inline pdf (data URI): {e}"
                return f"❌ Could not use the inline PDF you sent: {e}"
            resolved_filename = _sanitize_upload_filename(filename)
        else:
            try:
                inline_bytes = _decode_bare_base64_pdf(pdf_source)
            except _InlinePdfError as e:
                success = False
                error_msg = f"invalid inline pdf (base64): {e}"
                return f"❌ Could not use the inline PDF you sent: {e}"

            if inline_bytes is not None:
                pdf_bytes = inline_bytes
                resolved_filename = _sanitize_upload_filename(filename)
            else:
                path = _os.path.expanduser(pdf_source)
                if not _os.path.isfile(path):
                    success = False
                    error_msg = "local file path not readable by server (remote client?)"
                    return _upload_local_path_message(pdf_source)
                with open(path, "rb") as f:
                    pdf_bytes = f.read()
                resolved_filename = _sanitize_upload_filename(
                    filename or _os.path.basename(path)
                )

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
                _raise_for_status_with_reason(res)
                sid = res.json()["session_id"]

            # 3. Upload PDF (multipart)
            upload_headers = {k: v for k, v in _headers().items() if k != "Content-Type"}
            res = await client.post(
                f"{API_URL}/sessions/{sid}/upload",
                files={"file": (resolved_filename, pdf_bytes, "application/pdf")},
                headers=upload_headers,
                timeout=60.0,
            )
            _raise_for_status_with_reason(res)
            upload_data = res.json()

        results = upload_data.get("results", [])
        # CI-529：后端解析出来的化学品名回填进调用日志。🔴 取的是解析结果 `chemical_name`，
        # 不是文件名、也不是正文——那是 CI-527 的地盘且是错的路。
        parsed_chemicals = _chemicals_from_response({"tool_results": [
            {"result": {"chemical_name": r.get("chemical_name")}}
            for r in results if isinstance(r, dict)
        ]}) or []
        summary = upload_data.get("summary", {})

        if not results:
            return (
                f"Upload succeeded but no files were parsed.\n"
                f"Summary: {summary}\n"
                f"Session: `{sid}`"
            )

        lines = [f"**Session:** `{sid}`", f"**File:** {resolved_filename}", ""]

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
            "file": resolved_filename,
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
            structured_content=structured,
        )
    finally:
        # Inline base64 IS the document's bytes — logging even a prefix of it
        # would put customer SDS content into mcp_call_logs.input_params (a table
        # other roles can read). Record only the shape, never the payload.
        if pdf_source.startswith("data:"):
            logged_source = f"<inline data URI, {len(pdf_source)} chars>"
        elif len(pdf_source) > 200:
            logged_source = f"<inline base64 or long source, {len(pdf_source)} chars>"
        else:
            logged_source = pdf_source
        _log_intent("upload_msds_pdf", parsed_chemicals or None,
                    _json.dumps({"pdf_source": logged_source, "session_id": session_id}),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Batch Safety Check", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def batch_safety_check(
    chemicals: Annotated[list[str], Field(
    description='List of chemical names or CAS numbers to check together, e.g. '
                '["acetone", "sulfuric acid", "sodium hydroxide", "methanol"]. '
                'Intended for 2-20 items; this runs compatibility, hazards and PPE in '
                'one call, so cost and latency grow with the list length.',
    )],
    lang: Lang = None, intent: Intent = None,
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

    Good first call when reviewing an experiment protocol or Opentrons deck
    layout: it covers the pairwise interactions in one round-trip.

    Args:
        chemicals: List of chemical names or CAS numbers (2-20 items), e.g.
                   ["acetone", "sulfuric acid", "sodium hydroxide", "methanol"]
    """
    error_msg = None
    success = True
    try:
        if len(chemicals) < 2:
            return "Please provide at least 2 chemicals for a batch safety check."
        if len(chemicals) > 20:
            return "Maximum 20 chemicals per batch check. Please split into smaller groups."

        data = await _direct_batch(chemicals, lang=lang)
        sections = []

        sections.append("# Batch Safety Report")
        chem_list = ", ".join(chemicals)
        sections.append(f"**Chemicals ({len(chemicals)}):** {chem_list}\n")
        # 🔴 紧跟抬头：抬头刚说「Chemicals (20)」，截断这件事必须挨着它说，
        # 埋在报告中段等于没说。
        sections.extend(_batch_truncation_block(data, chemicals))

        if data.get("unresolved"):
            sections.extend(_unresolved_block(data, trailing_newline=True))
        sections.extend(_rejected_products_block(data))
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
                "pairs": [
                    _expose(p, rename={"chem1": "chemical_a", "chem2": "chemical_b"},
                            override={"traceability": p.get("traceability", "rule_based")})
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
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(sections))],
            structured_content=structured,
        ), data)
    finally:
        _log_intent("batch_safety_check", chemicals,
                        _intent_params({"chemicals": chemicals}, intent),
                    success=success, error_message=error_msg)


@mcp.tool(annotations=ToolAnnotations(title="Check Regulatory Lists", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def check_regulatory_lists(chemical: Chemical, lang: Lang = None, intent: Intent = None) -> str:
    """
    Check which international regulatory lists a chemical appears on.

    Searches 23 lists — 8 jurisdictions plus 3 international conventions:
    - US: EPA TSCA Inventory, OSHA PEL, California Prop 65
    - EU: SVHC Candidate List, REACH Annex XVII, REACH Annex XIV, REACH registered
      substances, CLP Annex VI, Seveso III, Water Framework Directive priority substances
    - APAC: China Catalogue of Hazardous Chemicals, China IECSC, Japan CSCL,
      Korea KECL, Australia AIIC, Singapore EPMA
    - Americas: Canada DSL
    - Conventions: Rotterdam PIC, Stockholm POPs, Montreal Protocol
    - Dual-use / export control: CWC Schedules 1/2/3, Australia Group precursors
    It also reports presence in the EPA CompTox Dashboard, which is an identifier
    resource rather than a regulatory list.

    Coverage limits, so the answer is not over-read:
    - There is NO Taiwan and NO IARC coverage. Do not infer either from this tool.
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
        #      paraphrased away or dropped. Five identical calls returned
        #      174/722/2588/2592/2599 characters.
        # Now it calls the deterministic endpoint and renders it. Same decision as
        # the 2026-04-22 Direct Service Layer switch; this tool was simply missed.
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


@mcp.tool(annotations=ToolAnnotations(title="Get SDS Document", read_only_hint=True, destructive_hint=False, open_world_hint=False), structured_output=False)
@_graceful_timeout
@_reported
async def get_sds_document(chemical: Chemical, intent: Intent = None) -> CallToolResult:
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
            region_suffix = f" · {region}" if region else ""
            heading = (
                f"**SDS Document: {chem_name}** (formulated product — no single CAS)"
                if kind == "product"
                else f"**SDS Document: {chem_name}** (CAS: {cas})"
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
