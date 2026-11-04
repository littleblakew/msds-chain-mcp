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

import sys
import types

import ui_compat_card
from request_identity import set_caller_credential

from msds_mcp import app, blocks, calls, direct, disclosures, params, regulatory_lists, render, sds_authoring
from msds_mcp import safety_tools, audit_tools, lookup_tools, document_tools, check_tools, route_tools  # noqa: E402 — listed order

from msds_mcp.app import API_KEY, __version__, _CARD_LINK_ORIGIN, _UPGRADE_ORIGIN, mcp  # noqa: F401

# Tool registration order is the order of `tools/list`; these modules register on import, in
# the order the tools are listed to clients. Helper-only modules are imported by their users.
_MODULES = (
    app, params, render, calls, direct, regulatory_lists, sds_authoring, disclosures, blocks,
    safety_tools, audit_tools, lookup_tools, document_tools, check_tools, route_tools,
)

# Rebound at runtime by the function that owns it (`global`), so a copy here would go stale:
# `server.<name>` reads it live from the owner instead (see `_ServerModule.__getattr__`).
_LIVE = {"_call_log_post_failures": calls}

# Everything the implementation modules define stays reachable as `server.<name>`.
for _m in _MODULES:
    for _n, _v in vars(_m).items():
        if not _n.startswith("__") and _n != "annotations" and _n not in _LIVE:
            globals().setdefault(_n, _v)


class _ServerModule(types.ModuleType):
    """`setattr(server, name, value)` rebinds `name` in every implementation module that holds it.

    The helpers live in the `msds_mcp` modules and each module looks a name up in its own
    globals, so a patch applied only to this module would silently leave the call sites
    unchanged. Tests and external callers patch through `server`, which keeps working."""

    def __getattr__(self, name):
        if name in _LIVE:
            return getattr(_LIVE[name], name)
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        if name in _LIVE:
            setattr(_LIVE[name], name, value)
            return
        super().__setattr__(name, value)
        for m in _MODULES:
            if name in vars(m):
                vars(m)[name] = value


sys.modules[__name__].__class__ = _ServerModule

# ---------------------------------------------------------------------------
# CI-1140：prompts —— MCP 里 workflow 的原生载体
# ---------------------------------------------------------------------------
# 我们此前只用了三种原语里的一种（23 个 tool，0 个 prompt，0 个 resource）。
# `tools` 是单次调用，**编排权在 host 的 agent 手里**；`prompts` 是 server 向 host 提供的
# 预置多步流程，用户在客户端打 `/` 就能选到。
#
# 🔴 **必须接受的边界，别在文案里假装不存在**：MCP 里编排权**永远**在 host 的 agent，
# 我们只能提供剧本、不能强制执行。⇒ 下面这些 prompt 的写法是**给 agent 的步骤说明**，
# 不是「请用户按顺序点」。任何要求用户多走一步的设计都会被 agent 省掉
# （day-1 hint 与 CI-174 两次实测，见那两张票）。
#
# 🔴 **内容不是新写的，是搬家**：同样的流程本来就在 `skills/msds-safety-check/rules/`，
# 但那是 **Claude Code skill 格式，只有 Claude Code 用户吃得到**。搬成 prompt 之后任何
# MCP host 都能用。⇒ **改流程时两处都要改**，否则两个受众拿到的步骤会分叉。
# 📌 守卫 `test_prompts.py` 钉住「prompt 里提到的工具名必须真实注册」——
# 教错一个参数名比不教更糟：模型会照着调，然后拿到一个它无法归因的错误。
#
# 🔴 **措辞红线（与工具文案同源）**：不许出现「未收录 / 没有数据 / 建议上传」那类断言
# ——那是在断言一件我们没验证过的事（CI-243 / CI-322 / CI-334 三次同形事故）。
# 说得出口的只有「这一轮没查」与「怎样才能让它被查」。


_PROMPT_TAIL = (
    "\n\nGrounding rules for every step above:\n"
    "- Always cite the source each tool returns (supplier + revision date). "
    "Traceability is the product; an uncited answer is worth less than no answer.\n"
    "- Never state or imply that a substance is absent from the database. If a lookup "
    "returns nothing, say what was not checked this turn and how to check it.\n"
    "- Do not add hazard, medical or regulatory claims that the tool output does not "
    "contain. General knowledge may be added only if labelled as such.\n"
)


@mcp.resource(
    ui_compat_card.CARD_URI,
    name="compat_card",
    title="Compatibility card",
    description="Inline card for check_chemical_compatibility: verdicts, precursor notices, source SDS.",
    mime_type=ui_compat_card.CARD_MIME,
    meta=ui_compat_card.resource_meta(_CARD_LINK_ORIGIN, _UPGRADE_ORIGIN),
)
def compat_card() -> str:
    """CI-1098：`check_chemical_compatibility` 的卡片模板（MCP Apps）。见 `ui_compat_card.py`。"""
    return ui_compat_card.card_html(_CARD_LINK_ORIGIN, _UPGRADE_ORIGIN)


@mcp.prompt(
    name="lab_protocol_audit",
    title="Lab protocol safety audit",
    description="Walk a lab protocol (or a list of chemicals) through compatibility, "
                "PPE and storage, then offer the deeper checks. For researchers "
                "writing or reviewing an experimental protocol.",
)
def lab_protocol_audit(protocol_text: str = "", chemicals: str = "") -> str:
    """剧本：协议正文 → 抽化学品 → 一次 batch → 按需深挖。

    两个入参都可选，因为真实调用分两种：贴一段协议，或直接给名单。
    🔴 两个都空时**不要猜**——让 agent 去问，而不是拿一个空列表去调工具。
    """
    source = (f'The protocol text is:\n"""\n{protocol_text}\n"""\n'
              if protocol_text.strip() else "")
    listed = (f"The chemicals are: {chemicals}\n" if chemicals.strip() else "")
    if not source and not listed:
        opening = ("Ask the user for either the protocol text or the list of chemicals "
                   "before calling any tool. Do not guess a chemical list.\n")
    else:
        opening = source + listed
    return (
        "You are auditing a laboratory protocol for chemical safety.\n\n"
        + opening +
        "\nSteps:\n"
        "1. If you were given protocol text, call `validate_protocol_chemicals("
        "protocol_text=...)` to extract and resolve the chemical names. If you were "
        "given a list, use it as-is.\n"
        "2. Call `batch_safety_check(chemicals=[...])` ONCE for the pairwise picture: "
        "it returns the compatibility matrix plus key risk warnings with the source SDS "
        "for each. It does NOT return PPE or storage grouping — those are separate calls "
        "(`get_ppe_recommendation`, `get_storage_guidance`), so do not present them as "
        "already covered.\n"
        "3. Present a concise safety brief: incompatible pairs first, then required PPE, "
        "then storage. Lead with what would hurt someone.\n"
        "4. Then offer, without running them: mixing order for a specific pair "
        "(`check_mixing_order(chemical_a=..., chemical_b=..., context=...)`), safer "
        "substitutes (`get_chemical_alternatives(chemical=..., use_case=...)`), "
        "emergency procedures (`get_emergency_response(chemical=..., scenario=...)` — "
        "scenario is exactly one of \"spill\", \"fire\", \"exposure\"; if the material "
        "reached a person it is \"exposure\" even when they said \"spilled\"), "
        "or a signed PDF audit (`create_audit_session(...)` then `get_audit_report(...)`, "
        "which needs an API key).\n"
        + _PROMPT_TAIL
    )


@mcp.prompt(
    name="ehs_compliance_review",
    title="EHS regulatory compliance review",
    description="Multi-region regulatory review for a chemical inventory: compliance "
                "status, exposure limits, GHS hazards and transport classification. "
                "For EHS officers preparing an audit or a shipment.",
)
def ehs_compliance_review(chemicals: str = "", regions: str = "EU,US") -> str:
    """剧本：逐区域合规 → 暴露限值 → GHS → 运输分类。

    🔴 `regions` 给默认值是**有意的**，但 agent 必须把「用了默认值」说出来——
    静默的默认值会被读成「查遍了所有法域」（CI-61 给 `check_regulatory_compliance`
    定的同一条规矩，这里只是把它复述给 agent）。
    """
    listed = (f"Chemicals: {chemicals}\n" if chemicals.strip() else
              "Ask the user which chemicals to review before calling any tool.\n")
    return (
        "You are performing a regulatory compliance review for an EHS officer.\n\n"
        + listed +
        f"Target regions: {regions}\n\n"
        "Steps:\n"
        "1. If the user gave a protocol or document instead of a list, call "
        "`validate_protocol_chemicals(protocol_text=...)` first.\n"
        "2. `check_regulatory_compliance(chemicals=[...], regions=[...])` — valid region "
        "codes are EU, US, CN, JP, KR, CA, AU, SG, TW.\n"
        "3. `get_exposure_limits(chemicals=[...], region=...)` for occupational limits.\n"
        "4. `get_chemical_risk_warnings(chemicals=[...])` for GHS class, signal word and "
        "H-codes.\n"
        "5. `get_transport_classification(chemicals=[...])` if anything will be shipped.\n"
        "6. Summarise per chemical, per region. Where a region was not requested, say it "
        "was not checked — do not present the requested regions as full coverage.\n\n"
        "If the region list came from the default rather than from the user, say so "
        "explicitly in the answer.\n"
        "No result from these tools is, on its own, an import/export clearance. An "
        "exposure limit answers 'how much exposure is allowed', never 'is this "
        "substance permitted'.\n"
        + _PROMPT_TAIL
    )


@mcp.prompt(
    name="incident_response",
    title="Chemical incident response",
    description="First-response steps for a spill, fire or exposure involving a known "
                "chemical, with the original supplier SDS attached.",
)
def incident_response(chemical: str = "", scenario: str = "spill") -> str:
    """剧本：先给急救步骤，再给原件链接。

    🔴 这个 prompt 的顺序与别的相反：**先答后溯源**。别的流程可以先摆出处再展开，
    而这条路上的人可能正站在泼洒物旁边。
    """
    named = (f"Chemical: {chemical}\n" if chemical.strip() else
             "Ask which chemical is involved before calling any tool.\n")
    return (
        "You are helping with a chemical incident. Answer the actionable steps FIRST, "
        "then the provenance — the person may be standing next to the spill.\n\n"
        + named +
        f"Reported scenario: {scenario}\n"
        "🔴 `get_emergency_response` accepts EXACTLY three values: \"spill\", \"fire\", "
        "\"exposure\". Anything else is rejected — map the incident to the closest one "
        "BEFORE calling.\n"
        "🔴 PERSON FIRST: if the material reached a person, use \"exposure\" EVEN IF the "
        "incident is also a spill and the user said \"spilled\" — \"I spilled it on my "
        "hand\" is \"exposure\". Skin contact, splash, inhalation, eye contact and "
        "ingestion all map to \"exposure\". A burn ON A PERSON is \"exposure\", not "
        "\"fire\"; use \"fire\" only when something is actually combusting.\n"
        "Only \"exposure\" returns the substance-specific first-aid protocol (for example "
        "the calcium gluconate protocol for hydrofluoric acid). Picking \"spill\" for a "
        "contact incident silently returns cleanup guidance instead of the antidote.\n\n"
        "Steps:\n"
        "1. `get_emergency_response(chemical=..., scenario=...)` and relay its steps in "
        "order, verbatim where they are procedural.\n"
        "2. `get_ppe_recommendation(chemicals=[...])` so the responder does not become "
        "the second casualty.\n"
        "3. `get_sds_document(chemical=...)` and give the link to the original supplier "
        "SDS.\n"
        "4. State the supplier and revision date of the record the steps came from.\n\n"
        "If the scenario involves a person, tell them to contact emergency services or "
        "poison control as well — these steps supplement professional help, never "
        "replace it.\n"
        + _PROMPT_TAIL
    )


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