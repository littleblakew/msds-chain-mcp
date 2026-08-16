#!/usr/bin/env python3
"""Send a cron-failure alert email via Microsoft Graph (client-credentials).

stdlib-only so the alert job needs no `pip install` — keeps the alerter's own
failure surface minimal. Mirrors lagentbot_email.send.GraphMailer but stripped of
branding (no logo attachment): this is an internal ops alert, not a user email.

Config (env, all injected from repo secrets by the workflow):
  MAILER_CLIENT_ID, MAILER_TENANT_ID, MAILER_CLIENT_SECRET

Alert details (env, from github.event.workflow_run.* in the workflow):
  ALERT_WORKFLOW   failed workflow display name
  ALERT_CONCLUSION run conclusion (e.g. "failure")
  ALERT_RUN_URL    html_url of the failed run
  ALERT_RUN_NUMBER run number
  ALERT_TIME       ISO timestamp of the run (updated_at)
  ALERT_REPO       owner/repo of the failing run（可选，缺省留空＝旧行为）。
                   🔴 **新接这个脚本的 workflow 一定要传**：两个仓（msds-chain 与
                   msds-chain-mcp）发同一个 devops@，不传的那封在收件箱里认不出来源。

Exits non-zero on any failure (missing config, token error, non-202) so the alert
workflow shows red. This workflow is deliberately NOT in its own listener list, so a
failed send cannot self-trigger an alert loop.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

RECIPIENT = "devops@lagentbot.com"
FROM = "noreply@lagentbot.com"


def _fail(msg: str) -> None:
    print(f"send_alert_email: {msg}", file=sys.stderr)
    sys.exit(1)


def _access_token(tenant: str, client_id: str, client_secret: str) -> str:
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
    }).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)["access_token"]


def _build_html() -> tuple[str, str]:
    # Generic override: any caller can set ALERT_SUBJECT (+ ALERT_BODY_HTML) to
    # send an arbitrary ops alert through this same mailer (e.g. runner-disk-alert).
    # Falls back to the cron-failure format when ALERT_SUBJECT is unset.
    subject_override = os.environ.get("ALERT_SUBJECT")
    if subject_override:
        return subject_override, os.environ.get("ALERT_BODY_HTML", "<p>(no body)</p>")

    workflow = os.environ.get("ALERT_WORKFLOW", "(unknown workflow)")
    conclusion = os.environ.get("ALERT_CONCLUSION", "failure")
    run_url = os.environ.get("ALERT_RUN_URL", "")
    run_number = os.environ.get("ALERT_RUN_NUMBER", "?")
    when = os.environ.get("ALERT_TIME", "")
    # 🔴 带上仓名：2026-08-16 起 msds-chain 与 msds-chain-mcp 两个仓都往同一个 devops@
    # 发告警。实物确认过（devops 的收件箱）：mcp 那份带 `[littleblakew/msds-chain-mcp]`，
    # 本仓两封（Schema Drift Check / E2E Nightly）**光秃秃没有仓名** —— 而两边都有
    # weekly/nightly 这类同质名字，收件人分不出是哪个仓挂了。
    # 缺省留空 ⇒ 没设 ALERT_REPO 的调用方行为完全不变。
    repo = os.environ.get("ALERT_REPO", "").strip()
    subject = f"🔴 Cron failed: {workflow}"
    html = (
        f"<p>A scheduled workflow concluded <b>{conclusion}</b>.</p>"
        f"<ul>"
        # ⚠️ 下面这组 <li> **不是**条件的——只有上面那行 Repo 是。加括号只为让
        # 「有 repo 才插一行」这个条件表达式能和后面的定长片段拼在一起。
        + (f"<li><b>Repo:</b> {repo}</li>" if repo else "")
        + (
            f"<li><b>Workflow:</b> {workflow}</li>"
            f"<li><b>Run:</b> #{run_number}</li>"
            f"<li><b>When (UTC):</b> {when}</li>"
            f"<li><b>Run log:</b> <a href=\"{run_url}\">{run_url}</a></li>"
            f"</ul>"
            f"<p>Check the run log. If the login step shows empty env vars, it's a "
            f"missing-secret/config issue — see memory <code>cron-missing-admin-secrets</code>.</p>"
        )
    )
    return subject, html


def _with_repo(subject: str) -> str:
    """给标题**加后缀**标出是哪个仓（缺省留空 ⇒ 逐字节沿用旧标题）。

    🔴 为什么在这里加、而不是在 `_build_html` 里：这个脚本有**两条出口**，
    `ALERT_SUBJECT` 覆盖路径会**提前返回**。msds-chain 的 4 个调用方里有 3 个走覆盖
    （`ci-queue-stall-alert` / `runner-disk-alert` / `cold-build-canary`）——而那恰恰是
    真正常响的那批（08-11 一天内 `CI queue stalled` 响了 4 次，`Cron failed` 一周才 4 封）。
    初版只改了默认格式那条，等于**把仓名加在了最不需要的那条路上**（devops 交叉验证抓到）。

    🔴 后缀而非中缀：三条覆盖路径的标题形状各不相同，没有统一的插入点；且收件人若按
    标题前缀设过邮件规则，改前缀会打破它、加后缀不会。
    """
    repo = os.environ.get("ALERT_REPO", "").strip()
    return f"{subject} [{repo}]" if repo else subject


def main() -> None:
    tenant = os.environ.get("MAILER_TENANT_ID")
    client_id = os.environ.get("MAILER_CLIENT_ID")
    client_secret = os.environ.get("MAILER_CLIENT_SECRET")
    if not all([tenant, client_id, client_secret]):
        _fail("MAILER_* env not fully configured")

    try:
        token = _access_token(tenant, client_id, client_secret)
    except (urllib.error.URLError, KeyError, TimeoutError) as exc:
        _fail(f"token request failed: {exc}")

    subject, html = _build_html()
    subject = _with_repo(subject)
    body = json.dumps({
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": RECIPIENT}}],
        },
        "saveToSentItems": False,
    }).encode()
    req = urllib.request.Request(
        f"https://graph.microsoft.com/v1.0/users/{FROM}/sendMail",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        _fail(f"sendMail HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        _fail(f"sendMail failed: {exc}")

    if status != 202:
        _fail(f"sendMail returned {status}, expected 202")
    print(f"send_alert_email: alert sent to {RECIPIENT} for '{os.environ.get('ALERT_WORKFLOW')}'")


if __name__ == "__main__":
    main()
