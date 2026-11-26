# Setup Guide

## Check MCP Availability

If this skill reached you from the MSDS Chain MCP server itself (a connector or a server added with `claude mcp add`), the server is connected: skip installation and use its tools.

Otherwise, look for the server's tools by their base names, whatever prefix the client adds (for example `check_chemical_compatibility`, `batch_safety_check`, `search_chemical_database`). In Claude Code they appear as `mcp__msds-chain__check_chemical_compatibility`; in claude.ai the prefix names the connector instead.

If none of these tools are available, follow the installation flow below.

## MCP Server Installation

Prompt the user:

> The MSDS Chain MCP server is not configured yet. To enable chemical safety tools, run:
>
> ```bash
> claude mcp add msds-chain --transport http https://mcp.lagentbot.com/mcp
> ```
>
> Then restart Claude Code. This connects to MSDS Chain's cloud API (free plan available, no API key needed for basic queries). For older clients that only speak SSE, use `--transport sse https://mcp.lagentbot.com/sse` instead.

There is no `pip install` package and no offline mode: every tool call reaches MSDS Chain's backend.

After the user installs, ask them to restart and retry their request.

## Degradation Without MCP

If the user cannot or does not want to install the MCP server:

1. Use Claude's general knowledge to provide basic chemical safety guidance.
2. **Always prefix with a disclaimer:**
   > ⚠ This information is based on general knowledge, not verified against MSDS Chain's database. For authoritative safety data, install the MCP server or consult official SDS documents.
3. Do NOT fabricate specific numeric values (exposure limits, flash points, etc.) — state "consult the official SDS" instead.
4. Suggest the web interface as an alternative: https://msdschain.lagentbot.com

## Sign-in and Credits

The hosted server signs the user in when they connect it (OAuth in claude.ai, Claude Code and other clients), so every tool, including `create_audit_session`, `get_audit_report`, `upload_msds_pdf` and `compare_sds_versions`, runs on that user's account. Do not ask the user for an API key or tell them to re-add the server with a key header.

- If a tool returns an authentication error, ask the user to reconnect the MSDS Chain connector (claude.ai: Settings → Connectors) or, in Claude Code, run `/mcp` and re-authenticate `msds-chain`.
- If a tool says the account is out of credits, pass on the upgrade link the tool returns; do not invent one.
- Self-hosted installs (running the open-source server with `MSDS_API_KEY`) are configured by whoever runs them; point those users to https://msdschain.lagentbot.com for a key.
