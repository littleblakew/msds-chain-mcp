from __future__ import annotations

import base64
import binascii
import httpx
import json as _json
import re
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from request_identity import get_caller_credential
from typing import Annotated, Any

from msds_mcp.app import API_URL, TIMEOUT, mcp
from msds_mcp.calls import (ToolReason, _graceful_timeout, _headers, _intent_params, _log_intent, _raise_for_status_with_reason, _reported, _strip_usage)
from msds_mcp.direct import _build_audit_session, _direct_compare_sds
from msds_mcp.params import Chemical, Intent
from msds_mcp.render import _chemicals_from_response



def _deprecated_version_args_note(ignored: list[str], from_version, to_version,
                                  *, compared: bool) -> str:
    """CI-848：把「参数被静默丢弃」写成一句用户能看懂的披露。

    🔴 措辞的红线：必须说清**没有按他要求做**，再说**实际做了什么**。
    只写「已忽略」不够——读的人（模型或人）会默认结果仍是他要的那份。

    🔴 `compared` 必须由调用方按**它自己分支的那个条件**（`has_newer`）传进来，
    不许在这里拿 `from_version`/`to_version` 的真假去猜：后端可以返回
    `has_newer=True` 而版本号为空，那时表头照样印 `Version None → None`，
    而这里会得出「什么都没比」——**一条自相矛盾的回复**（review 实测复现过）。
    """
    names = " / ".join(f"`{n}`" for n in ignored)
    head = (f"⚠️ {names} was supplied but is no longer supported, and was ignored."
            if len(ignored) == 1 else
            f"⚠️ {names} were supplied but are no longer supported, and were ignored.")
    if not compared:
        return (f"{head} This tool compares only the two most recent revisions on record, "
                f"and no comparison was made here — so the pair you asked for was not evaluated.")
    if from_version and to_version:
        return (f"{head} This tool compares only the two most recent revisions on record, "
                f"so the comparison above is {from_version} → {to_version}, "
                f"not the pair you asked for.")
    return (f"{head} This tool compares only the two most recent revisions on record, "
            f"so the comparison above is between those two, not the pair you asked for.")


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
    )] = "",
    version_old: Annotated[Any, Field(
        description='DEPRECATED and ignored. This tool always compares the two most recent '
                    'revisions on record; it cannot compare an arbitrary pair. Accepted only so '
                    'that clients holding an older tool snapshot are told their request was not '
                    'honoured, instead of silently receiving a different comparison.',
    )] = None,
    version_new: Annotated[Any, Field(
        description='DEPRECATED and ignored. See version_old.',
    )] = None, intent: Intent = None,
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
        version_old / version_new: DEPRECATED and ignored — an arbitrary version pair
            cannot be compared. Supplying them adds an explicit note to the reply saying
            the request was not honoured; they never change what is compared.
    """
    # CI-848：这两个参数在改走 `/api/v2` 直连时被删掉，但**外部客户端拿的是工具面的快照**，
    # 目录型分发渠道上的那份可能长期落后于我们发布的版本。旧客户端照旧传这两个参数，
    # 而 pydantic 对多余入参**静默丢弃**：用户要求「比较 v3 和 v5」，拿到的是「最近两版」的
    # 对比 —— 一份看起来完全正常的答案。这比报错贵得多。
    # ⇒ 收回来当**已弃用**接住，只为把静默损失变成显式披露。
    # 🔴 别拿它去实现任意版本对比——那是另一件事，要改后端。
    # 🔴 类型是 `Any` 且判据只看「有没有给」，故意的：这两个参数**唯一的职责是不让旧客户端失败**。
    # 收窄成 `str` 会把 `{"version_old": null}`（旧客户端填未用字段的常见写法）变成
    # ValidationError ⇒ 改之前它被静默丢弃、调用还能成功，改之后整条失败——
    # 那正好打在这个改动要保护的那批人身上（review 实测复现过）。
    ignored = [n for n, v in (("version_old", version_old), ("version_new", version_new))
               if v is not None and str(v).strip() != ""]

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
            payload = _strip_usage(data)
            if ignored:
                note = _deprecated_version_args_note(ignored, None, None, compared=False)
                text = f"{text}\n\n{note}"
                payload = {**payload, "deprecated_parameters_ignored": ignored,
                           "deprecated_parameters_note": note}
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                structured_content=payload,
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
        payload = _strip_usage(data)
        if ignored:
            note = _deprecated_version_args_note(
                ignored, data.get("from_version"), data.get("to_version"), compared=True)
            lines.append(f"\n{note}")
            payload = {**payload, "deprecated_parameters_ignored": ignored,
                       "deprecated_parameters_note": note}
        return CallToolResult(
            content=[TextContent(type="text", text="\n".join(lines))],
            structured_content=payload,
        )
    finally:
        _log_intent("compare_sds_versions", [chemical],
                        _intent_params({"chemical": chemical, "supplier": supplier, "region": region,
                                        **({"deprecated_args": ignored} if ignored else {})}, intent),
                    success=success, error_message=error_msg)


# ---------------------------------------------------------------------------
# CI-169: upload_msds_pdf resolves `pdf_source` on the machine running THIS
# server. For the hosted core that is our container — never the caller's laptop
# and never the client's sandbox — so os.path.isfile() can only ever fail for a
# remote client, i.e. for the callers this path exists to serve. Before this fix
# the call landed here silently: it did no work, wrote nothing, and still reported
# success with an empty error_message — invisible on our side and unexplained on
# the caller's. The reply must therefore name the constraint and give a next step
# the caller can actually take. Background: CI-101 / CI-169.
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
        "1. **Public link** — if the PDF has a publicly reachable HTTPS URL (supplier "
        "site, or a share link that needs no login), call `upload_msds_pdf` again with "
        "that URL and it will be fetched and parsed right here.\n"
        "2. **Send the bytes inline (small files only)** — base64-encode the PDF and "
        "call `upload_msds_pdf` again with that string, or with a "
        "`data:application/pdf;base64,<...>` data URI. Max 10 MB decoded. The whole "
        "encoded string must fit in one call (~4/3 the file size, so a typical 150 KB "
        "SDS is ~200,000 characters); if you cannot emit all of it, use 1 or 3.\n"
        "3. **Web upload** — go to https://msdschain.lagentbot.com, sign in with the "
        "same account, and upload the PDF there. This works for any local file.\n\n"
        "Either way the PDF is parsed into structured safety data (chemical name, CAS, "
        "GHS classification, H-codes, PPE, storage, incompatibilities) and stored under "
        "your account, so the other tools here can use it. Contributing an SDS we do "
        "not already hold also earns credits."
    )


@mcp.tool(annotations=ToolAnnotations(title="Upload & Parse MSDS PDF", read_only_hint=False, destructive_hint=False, open_world_hint=True), structured_output=False)
@_reported
async def upload_msds_pdf(
    pdf_source: Annotated[str, Field(
        description="The PDF itself, in one of three forms, tried in this order: "
                    "(1) a publicly reachable HTTPS URL, fetched server-side; "
                    "(2) inline base64 of the raw PDF bytes — either a data URI "
                    "(`data:application/pdf;base64,<...>`) or a bare base64 string, max "
                    "10 MB decoded — only if you can emit the WHOLE encoded string in this "
                    "one call (base64 is ~4/3 the file size, so a typical 150 KB SDS is "
                    "~200,000 characters; if you cannot hold it all, ask the user for a "
                    "public link or send them to the web uploader instead); "
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
                         2. **Inline file bytes, base64-encoded** — only when
                            you can put the ENTIRE encoded string into this one
                            call. base64 is ~4/3 the file size (a typical 150 KB
                            SDS is ~200,000 characters) and cannot be split
                            across calls, so a PDF attached to a chat in a
                            remote client usually does not fit; prefer a public
                            link (1) or the web uploader. Pass either a data URI
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

                         Rule of thumb: a public HTTPS link is the most reliable
                         route; inline base64 only for files small enough to pass
                         whole; never a local path that only exists on the user's
                         machine, not the server's.
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
                # 🔴 **这条 URL 是调用方给的、指向别人的主机 ⇒ 绝不能走
                # `_raise_for_status_with_reason`**（[[CI-868]] review 抓到）。
                # 那个函数里的 401/403 话术断言的是**我们自己账号的授权状态**；供应商门户
                # 的 403（Cloudflare / 需登录 / 签名链接过期）会因此收到一句
                # 「你的 MSDS Chain 账号没有权限，重连也没用」—— **自信、具体、而且错**，
                # 正是本票要消灭的那种失败，只是换了个地方发生。
                # ⚠️ 而且它比改之前**更差**：裸 `raise_for_status()` 虽然不友好，
                # 至少带着那个外部 URL，成因还追得回来。
                # 🔴 这里说清「是源站拒绝的，不是我们」，并且**不给任何关于我们账号的建议**。
                if resp.status_code >= 400:
                    raise ToolReason(
                        f"Could not download that URL (HTTP {resp.status_code} from the "
                        f"source host, not from MSDS Chain). The link may require a login, "
                        f"be expired, or be blocked by the host. Ask the user for a direct "
                        f"PDF link, or have them upload the file contents instead."
                    )
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
            # CI-1444: the stored record's id — the handle `validate_sds` needs. Absent
            # when nothing was stored (failed parse, a chemical list rather than an SDS).
            if r.get("msds_id") is not None:
                lines.append(f"   msds_id: {r['msds_id']}")
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
        if any(isinstance(r, dict) and r.get("msds_id") is not None for r in results):
            lines.append(
                f"\nTo check an uploaded SDS for contradictions, gaps, age and US OSHA "
                f"format, call `validate_sds(session_id=\"{sid}\", msds_id=<msds_id above>)`, "
                f"or omit `msds_id` to check the session's uploads (the most recent 50)."
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
                    "msds_id": r.get("msds_id"),
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


# ---------------------------------------------------------------------------
# CI-1135：批量分析的**副产物** —— 会话容器
# ---------------------------------------------------------------------------
# 同一个「会话＝分析容器」在 Web 上是主力（90 天 1200 个 / 26 人），在 MCP 上是 0：
# 外部真人多化学品分析 206 次 / 25 人，而 `create_audit_session` **一次都没有**。
#
# 🔴 **前两次尝试的共同形状是「还要求用户多走一步」**，两次都零效果：
#   ① day-1：在返回里挂一句「想要签名报告就调 `create_audit_session`」⇒ 6 个外部用户、0 个会话
#   ② [[CI-174]]：`get_audit_report()` 零参也能调，自动从最近分析建会话 ⇒ 至今外部 1 次
# ⇒ 本票**取消这一步**：会话是批量分析的副产物，不是一个要用户去点的下一步。
# **在 MCP 里编排权永远在 host 的 agent，任何要求多走一步的设计都会被它省掉。**
#
# 🔴 **三条硬约束，缺一条这个设计就有害**：
#   · **并发，不串行**：会话那条路与 batch 各自最多吃 `TIMEOUT_MULTI`；串起来等于把这个
#     工具的延迟预算翻倍。两者互不依赖 ⇒ `asyncio.gather`。
#   · **失败绝不影响答案**：会话是副产物，它超时/报错时用户照样要拿到安全数据。
#     ⇒ 整段裹在 `except Exception` 里，只丢掉副产物。
#   · **只在有调用方凭证时落**：`POST /sessions` 把会话绑到 key 的所有者；匿名调用建出来的
#     会话没有归属、用户也取不到报告，只会往 `demo.sessions` 里堆垃圾行。
#
# 🔴 **它今天不额外收费**（`create_audit_session` 在后端是 `_zero_priced("session_create")`），
# 而那是**配置不是代码** —— 后端那段注释明写「把 `VALUE_CREDITS[result_type]` 改成非 0
# 就开始收费，不需要改任何代码」。守卫 `test_batch_session_side_effect.py` 钉住这个前提：
# 定价那天必须先决定「批量分析可不可以静默产生一笔费用」，**而不是让它静默发生**。
async def _session_from_batch(chemicals: list[str]) -> str | None:
    """把这次批量分析落成一个会话，返回 session_id；拿不到就返回 None（绝不抛）。"""
    if not get_caller_credential():
        return None
    try:
        built = await _build_audit_session(
            experiment_name=f"Batch safety check — {len(chemicals)} chemicals",
            chemicals=chemicals,
        )
        sid = (built or {}).get("session_id")
        return str(sid) if sid else None
    except Exception:  # noqa: BLE001 —— 副产物，绝不许拖垮答案
        return None
