# MSDS Chain MCP Server

**Chemical safety intelligence for AI-assisted experiment design.**

An [MCP](https://modelcontextprotocol.io) server that gives AI agents (Claude, ChatGPT,
Claude Code, Gemini CLI, Copilot Studio, and any other MCP client) **24** tools for chemical
safety reasoning: compatibility checks, hazard analysis, regulatory compliance, PPE, storage,
transport, and signed audit reports. Every answer names the supplier SDS it came from, with a
link to the original PDF.

MSDS Chain, powered by **ChainSDS**: a verified, always-current chemical safety database.

Built for researchers who design experiments with AI and want safety verification inside the
workflow rather than as a separate step: whether the chemicals on a deck are compatible, which
mixing orders are dangerous, what PPE a specific handling step needs, how a compound is treated
under EU REACH or US OSHA/TSCA, and what a GLP/GMP reviewer will ask for afterwards.

## Quick Start

Hosted endpoint. Nothing to install, nothing to run locally:

```bash
claude mcp add msds-chain --transport http https://mcp.lagentbot.com/mcp
```

Then run `/mcp` in Claude Code and sign in. A browser opens, you enter your email, and you paste
back the code we send you. Your account is provisioned automatically and the free plan includes
a monthly call allowance. There is no API key to create beforehand.

On another platform? See [Platform Setup](#platform-setup) below.

## Tools (24)

| Tool | Description |
|------|-------------|
| **`batch_safety_check`** | One-call report for a chemical list: pairwise compatibility + per-chemical risk warnings |
| **`check_regulatory_lists`** | Cross-reference a chemical against 23 regulatory watch lists across 8 jurisdictions + 3 international conventions |
| **`get_sds_section`** | Retrieve a specific SDS section (1-16) for a chemical |
| **`get_sds_document`** | Signed download URL (~5 min) for the original SDS/MSDS PDF; includes source provenance |
| **`get_chemical_alternatives`** | Safer substitutes for restricted or high-risk chemicals |
| **`validate_protocol_chemicals`** | Extract & validate chemical names from protocol text or code |
| **`check_mixing_order`** | Safe addition sequence for reagent pairs (e.g., acid into water) |
| **`get_waste_disposal`** | Waste classification, container type, and disposal procedures |
| **`upload_msds_pdf`** | Upload MSDS PDF for AI-powered parsing and data extraction (requires API key) |
| **`draft_sds_sections`** | Draft Sections 8 and 15 of an SDS for your own mixture from its ingredient list; every line cites its source, and missing ingredients are listed rather than guessed |
| **`compare_sds_versions`** | Hazard-change diff between two SDS versions (H-code additions/removals + whether they change a verdict) |
| `check_chemical_compatibility` | Pairwise compatibility for 2+ chemicals |
| `get_chemical_risk_warnings` | GHS classification, H-codes, signal words, flash point |
| `get_ppe_recommendation` | Gloves, eye protection, respiratory, body protection |
| `get_storage_guidance` | Storage class, cabinet type, temperature, isolation rules |
| `get_emergency_response` | Spill, fire, or exposure emergency procedures |
| `get_exposure_limits` | OEL/TLV/PEL/MAC across US, EU, JP, CN, INT |
| `get_transport_classification` | UN number, hazard class, packing group, ADR/IATA/IMDG |
| `check_regulatory_compliance` | Multi-region compliance status, list-backed for EU, US, CN, JP, CA, AU, SG |
| `search_chemical_database` | Look up chemicals by name, synonym, or CAS number |
| `ask_chemical_safety` | Natural language catch-all for any safety question |
| `create_audit_session` | Full audit with signed PDF report (requires API key) |
| `get_audit_report` | Download link for the signed audit PDF |

## Usage Examples

Experiment protocol review:

```
User: I'm planning a Grignard reaction with magnesium turnings, diethyl ether,
      and bromobenzene. Check if this setup is safe.

Agent:
  → calls batch_safety_check(["magnesium", "diethyl ether", "bromobenzene"])
  → Returns: compatibility matrix + per-chemical risk warnings (with source SDS)
```

Signed audit report:

```
User: Create a safety audit report for our quarterly review.
      Chemicals: acetone, methanol, ethanol, isopropanol, hexane.

Agent:
  → calls create_audit_session("Q2 2026 Solvent Cabinet Review", [...])
  → calls get_audit_report("SESSION-ID")
  → Returns: signed PDF URL (Ed25519 signature, suitable for GLP/GMP compliance)
```

## Platform Setup

One hosted server, two transports. Pick the row your client supports:

| Transport | Endpoint | Notes |
|-----------|----------|-------|
| Streamable HTTP | `https://mcp.lagentbot.com/mcp` | Preferred: broadest and most robust |
| SSE | `https://mcp.lagentbot.com/sse` | For clients that only speak SSE |

Authentication is either OAuth (the client opens a browser sign-in on first connect) or a static
header, `Authorization: Bearer sk-msds-your-key`, for headless clients. Create a key at
[msdschain.lagentbot.com](https://msdschain.lagentbot.com) > API Keys.

### Claude (web, desktop, mobile)

Settings > Connectors, search **msds-chain**, and enable it. Sign-in happens on first use.

### Claude Code

Three routes, any one is enough:

```bash
# 1. Remote server only
claude mcp add msds-chain --transport http https://mcp.lagentbot.com/mcp

# 2. Plugin from the official Claude plugin marketplace (server + skill)
/plugin install msds-chain@claude-plugins-official

# 3. npm shim (registers the same hosted endpoint)
claude mcp add msds-chain -- npx -y msds-chain-mcp@latest
```

Manual config in `~/.claude.json`:

```json
{
  "mcpServers": {
    "msds-chain": {
      "type": "http",
      "url": "https://mcp.lagentbot.com/mcp"
    }
  }
}
```

Routes 2 and 3 of the plugin also install the `/msds-safety-check` skill: it spots chemicals in
your conversation and offers a check, and runs a guided audit for lab protocols or EHS compliance.

### ChatGPT

Listed in the OpenAI Plugin Directory. Search **MSDS Chain** in ChatGPT, enable it, and sign in
when prompted. Works on web, desktop, and mobile.

### Gemini CLI

Listed in the [Gemini CLI extensions gallery](https://geminicli.com/extensions/):

```bash
gemini extensions install https://github.com/littleblakew/msds-chain-mcp
```

Gemini asks you to confirm twice: once to trust the workspace, once to review the extension and
the skill it carries. Your first chemical question starts the sign-in automatically; the server
supports Dynamic Client Registration, so Gemini registers itself and completes the OAuth exchange
on its own. Check the connection with `gemini mcp list`, then just ask in chat:

```
Can acetone and sodium hypochlorite be stored in the same cabinet?
```

### OpenAI Codex CLI

```bash
codex marketplace add https://github.com/littleblakew/msds-chain-mcp
```

### Microsoft Copilot Studio and Power Platform

MSDS Chain ships a certified Power Platform connector, so there is nothing to configure by hand.
In Copilot Studio, add a tool from the connector directory and search **MSDS Chain**; the
connector handles the OAuth exchange against the same hosted endpoint.

### SSE-only clients (悟空 / Wukong, Dify, Coze)

These clients do not speak streamable HTTP yet, so point them at `/sse` and pass a static key:

1. Open the MCP server or tool integration settings
2. Transport type: **SSE**
3. URL: `https://mcp.lagentbot.com/sse`
4. Header: `Authorization` : `Bearer sk-msds-your-key`

### Any other MCP client

Use the endpoint table above. Clients that implement OAuth 2.1 with Dynamic Client Registration
need no pre-registration; everything else uses the static `Authorization` header.

## Privacy & Data Handling

- **Privacy Policy:** [msdschain.lagentbot.com/privacy](https://msdschain.lagentbot.com/privacy)
- Read-only tools send only the chemical names and queries you provide; no personal data is required.
- API-key tools (`upload_msds_pdf`, `create_audit_session`, `get_audit_report`) associate activity
  with your account for audit traceability.
- Uploaded MSDS PDFs are processed for data extraction and contribute to the verified ChainSDS
  database per your account's data-sharing settings.

## Data Coverage

Industry-sourced, AI-verified, and cryptographically signed.

- **4,000,000+ chemical records** with multi-language aliases (EN/ZH/JA)
- **NFPA/GHS classification** for compatibility rules
- **23 regulatory watch lists** across major jurisdictions (EU, US, CN, JP, KR, CA, AU, SG)
  plus international conventions
- **Occupational exposure limits** from 5 standards (OSHA PEL, ACGIH TLV, EU IOELV, JP OEL, CN GBZ)
- **UN transport data** for 16+ common lab chemicals
- **Version tracking:** detects a newer SDS and diffs its hazard classification against the one
  you relied on

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `401 Unauthorized` on connect | Missing or invalid API key | Check the `Authorization: Bearer sk-msds-...` header. Generate a fresh key at [msdschain.lagentbot.com](https://msdschain.lagentbot.com) > API Keys. |
| Tools don't appear in the client | Server not loaded | Fully restart the client after adding the server, then confirm `msds-chain` shows in the MCP/tools list. |
| Connection drops or times out | Wrong transport | Prefer `https://mcp.lagentbot.com/mcp`; SSE sessions can drop when the server redeploys. `GET https://mcp.lagentbot.com/health` should return `{"status":"ok"}`. |
| `Quota exceeded` / 429 | Monthly call limit hit | Free plans are capped; upgrade or wait for the monthly reset. |
| Empty results for a known chemical | Name not matched | Retry with the CAS number or a common synonym; `search_chemical_database` accepts name, synonym, or CAS. |
| OAuth sign-in fails | OAuth metadata unreachable | Confirm `GET https://mcp.lagentbot.com/.well-known/oauth-authorization-server` returns 200; the client should auto-discover the authorize and token endpoints. |

Still stuck? Email **contact@lagentbot.com** or open an issue at
[github.com/littleblakew/msds-chain-mcp/issues](https://github.com/littleblakew/msds-chain-mcp/issues).

## License

MIT

## About

Built by [LAgentBot](https://lagentbot.com), AI-powered chemical safety infrastructure.

Part of the [MSDS Chain](https://msdschain.lagentbot.com) platform: the world's first AI
Agent-driven chemical safety data trust network, powered by ChainSDS.
