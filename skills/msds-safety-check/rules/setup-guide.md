# Setup Guide

## Check MCP Availability

Before calling any MCP tool, check if the `msds-chain` MCP server is available. The following tool names indicate the server is configured:
- `mcp__msds-chain__search_chemical_database`
- `mcp__msds-chain__batch_safety_check`
- `mcp__msds-chain__check_chemical_compatibility`

If none of these tools are available, follow the installation flow below.

## MCP Server Installation

Prompt the user:

> The MSDS Chain MCP server is not configured yet. To enable chemical safety tools, run:
>
> ```bash
> claude mcp add msds-chain --transport sse --url https://mcp.lagentbot.com/sse
> ```
>
> Then restart Claude Code. This connects to MSDS Chain's cloud API (free tier: 100 calls/day, no API key needed for basic queries).
>
> For local/offline use:
> ```bash
> pip install msds-chain-mcp
> claude mcp add msds-chain -- python -m msds_chain_mcp
> ```

After the user installs, ask them to restart and retry their request.

## Degradation Without MCP

If the user cannot or does not want to install the MCP server:

1. Use Claude's general knowledge to provide basic chemical safety guidance.
2. **Always prefix with a disclaimer:**
   > ⚠ This information is based on general knowledge, not verified against MSDS Chain's database. For authoritative safety data, install the MCP server or consult official SDS documents.
3. Do NOT fabricate specific numeric values (exposure limits, flash points, etc.) — state "consult the official SDS" instead.
4. Suggest the web interface as an alternative: https://msdschain.lagentbot.com

## API Key Upgrade

Some tools require an API key:
- `create_audit_session` — creates persistent audit sessions
- `get_audit_report` — generates signed PDF reports
- `upload_msds_pdf` — uploads and parses MSDS PDFs
- `compare_sds_versions` — compares SDS document versions

When the user triggers one of these tools without an API key, prompt:

> This feature requires an MSDS Chain API key (free: 100 calls/month).
>
> 1. Sign up at https://msdschain.lagentbot.com
> 2. Go to Settings → API Keys → Create Key
> 3. Add the key to your MCP config:
>    ```bash
>    claude mcp remove msds-chain
>    claude mcp add msds-chain --transport sse --url https://mcp.lagentbot.com/sse -e MSDS_API_KEY=sk-msds-your-key
>    ```
> 4. Restart Claude Code

Do NOT repeat this prompt if the user has already been informed in this conversation.

## Language Configuration

The MCP server supports 5 languages: English (en), Chinese (zh), Japanese (ja), German (de), Indonesian (id).

To set the default language:
```bash
claude mcp remove msds-chain
claude mcp add msds-chain --transport sse --url https://mcp.lagentbot.com/sse -e MSDS_LANG=zh
```

However, the skill should match the user's conversation language regardless of this setting.
