from __future__ import annotations

import httpx
import json as _json
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from request_identity import get_caller_credential
from typing import Annotated

from msds_mcp.app import API_URL, LANG, TIMEOUT, mcp
from msds_mcp.calls import (CreditsExhausted, ToolReason, _billed_json, _graceful_timeout, _headers, _intent_params, _log_intent, _reported, _with_usage)
from msds_mcp.direct import _build_audit_session, _direct_recent_chemicals
from msds_mcp.params import ChemicalList, Intent, ReportLang, _normalize_report_lang



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
async def get_audit_report(
    session_id: Annotated[str | None, Field(
        description='The session id returned by `create_audit_session`, e.g. "DEMO-A1B2C3D4". '
                    'OMIT IT to report on what this user has already analysed in the last 7 '
                    'days — no session needed, nothing to restate.',
    )] = None,
    lang: ReportLang = None,
) -> str:
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
    # 🔴 CI-905：**这次调用自己建了 session 吗**——不能用 `built_from_recent` 代替。
    # 那个列表只装后端真的收进去的化学品，`recent` 非空但一个都没入库时它是 `[]`,
    # 而 session **已经建出来了**。两者在「建没建」这一问上不同形，正是 402 那条路要问的。
    built_here = False
    try:
        if not get_caller_credential():
            return "get_audit_report requires an authenticated API key (MSDS_API_KEY for stdio, or gateway auth for remote)."

        if not session_id:
            # CI-174: the report used to be reachable only through a session id the
            # caller never had. Measured: the "call create_audit_session if you
            # want a signed report" hint has been on batch_safety_check since the
            # day it shipped — over every external call it has seen,
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
            built_here = True
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
                # 🔴 CI-361 ⑥：这个参数**决定用户下载到的那份 PDF 是什么语言**。
                # 在它之前，这条通道压根没传 `lang` ⇒ 经 MCP 拿报告的人永远只拿得到
                # 英文，而后端那侧也不报错（未知 query 参数被丢弃）。
                # ⚠️ **后端先于本仓上是安全的、反过来也是**：旧后端忽略这个参数 ⇒
                # 退回英文（今天的行为），不是 422。所以两个仓不必卡时序。
                # 🔴 `or LANG` 不能省（本仓守卫 `test_lang_forwarding_has_exactly_one_spelling`
                # 抓到我第一版漏了它）：`LANG` 是 `MSDS_LANG` 那个服务端默认语言。
                # 漏掉它的后果是**自托管者设了 `MSDS_LANG=de` 时，别的工具答德语、
                # 报告却给英文** —— 一个只在配置过默认语言的部署上才出现的不一致。
                params={"lang": _normalize_report_lang(lang or LANG)},
            )
            if res.status_code == 403:
                return (
                    f"Not authorized to access session `{session_id}`. Make sure the "
                    f"session was created with the same MSDS_API_KEY."
                )
            if res.status_code == 404:
                return f"Session `{session_id}` not found."
            # 🔴 CI-892：此前这里是 `_raise_for_status_with_reason(res)` + `res.json()["url"]`,
            # 也就是**唯一一个不走计费响应处理的取值型工具**（`_billed_json` 的另外 19 个
            # 调用点它一个都不在）。今天这条路后端零扣费所以无害，但 CI-892 要让报告收
            # 10 credits（web 走 `/report/generate` 已经在扣，MCP 走 `signed-url` 没扣，
            # 实测过），那一刻它会变成**扣钱且零提示**，且余额耗尽时
            # `_raise_for_status_with_reason` 不处理 402 ⇒ 落到裸 `raise_for_status()`,
            # 用户拿到一句 `Client error '402 Payment Required'`。
            # 🔴 **本仓先于后端上是有意的、也是安全的**：后端今天不发 usage header ⇒
            # `_parse_usage` 空 ⇒ `_with_usage` 是 no-op，402 也还不会发生。等后端开收费时
            # 这条通道已经是好的，不用两个仓卡时序。
            try:
                billed = _billed_json(res)
            except ToolReason as e:
                # 🔴 CI-905：零参路径**先免费建 session、后付费取链接**。余额在中间耗尽时，
                # `_billed_json` 那句话里没有 `session_id`，函数也没有别的出口把它交出去
                # ⇒ 用户充值后重问，模型只能再走一次零参路径，**又建一个 session**，
                # 前一个成为孤儿行。把 id 说出来，重试就能复用它。
                # ⚠️ 只在 `built_here` 时补：调用方自己传 id 的那条路上什么都没丢，
                # 多说一句只是噪音。
                if built_here:
                    told = (
                        f"{e} Your report session `{session_id}` was already created "
                        f"before the charge failed — once topped up, call "
                        f'get_audit_report(session_id="{session_id}") to finish it, '
                        f"rather than asking again (that would build a second session)."
                    )
                    # CI-1403：余额耗尽要保住类型，外层才会给出升级卡片。
                    raise (e.retold(told) if isinstance(e, CreditsExhausted)
                           else ToolReason(told)) from e
                raise
            relative = billed["url"]

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
        # CI-892: 与另外 19 个取值型工具一致——`_with_usage` 在文本尾部补 `💳` 那行、
        # 并把 `usage` 放进 structuredContent。后端没计量时它自己 no-op。
        return _with_usage(CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content={
                "session_id": session_id,
                "report_url": full_url,
                "expires_in_seconds": 300,
                "built_from_recent_analyses": bool(built_from_recent),
                "chemicals": built_from_recent or None,
                "chemicals_not_in_report": not_in_report or None,
            },
        ), billed)
    finally:
        # 🔴 把「走的是哪条路径」记下来：这次改动要回答的正是「零参调用有没有被用起来」，
        # 两条路径记成同一个形状的话，下一轮读日志的人读不出答案。
        _log_intent("get_audit_report", built_from_recent or None,
                        _json.dumps({"session_id": session_id,
                                     "built_from_recent": bool(built_from_recent)}),
                    success=success, error_message=error_msg)
