# dsh-msds-chain

Chemical safety intelligence for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness).
This bundle mounts the hosted MSDS Chain MCP endpoint through the in-box
`@deepseek-ai/dsh-mcp-client` bridge — no server to run locally.

23 MCP tools covering compatibility and mixing-order checks, GHS hazard analysis, PPE
recommendations, storage and waste-disposal guidance, occupational exposure limits,
transport classification, multi-region regulatory compliance (EU/US/CN/JP/KR/CA/AU/TW/SG),
safer alternatives, protocol text parsing, SDS section lookup and version diffs, and
signed audit reports. Every answer cites the supplier SDS and revision date it is
grounded in. Indexes 4.3M+ chemicals and 535K+ SDS documents, in 5 languages.

## Install

```sh
dsh plugin --profile web add github:littleblakew/msds-chain-mcp#path:/plugins/dsh
```

## Configure

An API key is required — there is no anonymous tier on this endpoint. Create one at
[msdschain.lagentbot.com](https://msdschain.lagentbot.com) → API Keys (free tier:
100 calls/month), then:

```sh
export MSDS_API_KEY=sk-msds-your-key
```

Start `dsh` with that variable set. The tools register on `ctx.tools` under the
`msds-chain` namespace, so the model sees `mcp__msds-chain__ask_chemical_safety`,
`mcp__msds-chain__check_chemical_compatibility`, and so on.

## Verifying it loaded

⚠️ **A missing or invalid key fails silently.** The endpoint answers `401`, no tools
register, and `dsh` prints nothing to the console — the harness starts normally and the
model simply has no `mcp__msds-chain__*` tools. (Measured against dsh `0.1.0-rc.6`.)

So check the key itself rather than watching for an error:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://mcp.lagentbot.com/mcp \
  -H "Authorization: Bearer $MSDS_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

`200` means the key works; `401` means it does not.

This bundle deliberately leaves `failOnStartupError` at its default (`false`). Setting it
to `true` does surface the 401 loudly — but it aborts the whole plugin tree, so `dsh`
refuses to boot at all. A chemical-safety plugin should not be able to take down your
harness because our endpoint had a bad minute.

## Which tool to use

For a broad safety question — hazards, PPE, first aid, spill response, storage,
disposal — call `ask_chemical_safety`. It returns one answer grounded in a specific
supplier SDS. The granular tools are for when you want that one structured field.

## Self-hosting

To point at your own core instead of the hosted endpoint, override the row from a
later layer (profile `cordis.patch.yml` or `--patch`) — see the comment block in
[`cordis.patch.yml`](cordis.patch.yml).

## Links

- MCP server source and tool reference: <https://github.com/littleblakew/msds-chain-mcp>
- Product: <https://msdschain.lagentbot.com>

MIT licensed.
