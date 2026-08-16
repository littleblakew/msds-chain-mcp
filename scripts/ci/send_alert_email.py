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
    # 🔴 带上仓名：2026-08-16 起 msds-chain 与 msds-chain-mcp 两个仓都往同一个
    # devops@ 发告警，只写 workflow 名的话收件人分不清是哪个仓挂了——而两边都有
    # 「weekly」「nightly」这类同质名字。缺省留空是为了与 msds-chain 那份保持兼容。
    repo = os.environ.get("ALERT_REPO", "")
    subject = f"🔴 Cron failed{f' [{repo}]' if repo else ''}: {workflow}"
    html = (
        f"<p>A scheduled workflow concluded <b>{conclusion}</b>.</p>"
        f"<ul>"
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
