# MSDS Chain MCP Server

**Chemical safety intelligence for AI-assisted experiment design.**

Powered by **ChainSDS** — a verified, always-current chemical safety database. *Verified. Current. Growing.*

An [MCP](https://modelcontextprotocol.io) server that gives AI agents (Claude Code, Cursor, Copilot, pi, etc.) access to chemical safety reasoning — compatibility checks, hazard analysis, regulatory compliance, PPE recommendations, storage guidance, and more.

Built for researchers who design experiments with AI and need safety verification integrated into their workflow.

## Why This Exists

When you use Claude to plan a synthesis route or set up an Opentrons protocol, safety validation shouldn't be a separate step. This MCP server lets your AI assistant automatically:

- Check if chemicals on the same deck are compatible
- Flag dangerous mixing orders
- Recommend PPE for the specific chemicals you're handling
- Verify compliance with EU REACH, US OSHA/TSCA, and 7 other jurisdictions
- Generate signed audit reports for GLP/GMP compliance

## Tools (23)

| Tool | Description |
|------|-------------|
| **`batch_safety_check`** | One-call comprehensive report: compatibility + PPE + storage grouping for a chemical list |
| **`check_regulatory_lists`** | Cross-reference a chemical against 23 regulatory watch lists across 10 regions |
| **`get_sds_section`** | Retrieve a specific SDS section (1-16) for a chemical |
| **`get_sds_document`** | Signed download URL (~5 min) for the original SDS/MSDS PDF; includes source provenance |
| **`get_chemical_alternatives`** | Safer substitutes for restricted or high-risk chemicals |
| **`validate_protocol_chemicals`** | Extract & validate chemical names from protocol text or code |
| **`check_mixing_order`** | Safe addition sequence for reagent pairs (e.g., acid into water) |
| **`get_waste_disposal`** | Waste classification, container type, and disposal procedures |
| **`upload_msds_pdf`** | Upload MSDS PDF for AI-powered parsing and data extraction (requires API key) |
| **`compare_sds_versions`** | Structured 7-dimension diff between SDS versions of a chemical |
| `check_chemical_compatibility` | Pairwise compatibility for 2+ chemicals |
| `get_chemical_risk_warnings` | GHS classification, H-codes, signal words, flash point |
| `get_ppe_recommendation` | Gloves, eye protection, respiratory, body protection |
| `get_storage_guidance` | Storage class, cabinet type, temperature, isolation rules |
| `get_emergency_response` | Spill, fire, or exposure emergency procedures |
| `get_exposure_limits` | OEL/TLV/PEL/MAC across US, EU, JP, CN, INT |
| `get_transport_classification` | UN number, hazard class, packing group, ADR/IATA/IMDG |
| `check_regulatory_compliance` | Multi-region: EU, US, CN, JP, KR, CA, AU, TW, SG |
| `search_chemical_database` | Look up chemicals by name, synonym, or CAS number |
| `ask_chemical_safety` | Natural language catch-all for any safety question |
| `create_audit_session` | Full audit with signed PDF report (requires API key) |
| `get_audit_report` | Download link for the signed audit PDF |

## Quick Start

### 1. Get an API key

Sign up at [msdschain.lagentbot.com](https://msdschain.lagentbot.com) → **API Keys** tab → create a key.

### 2. Install

```bash
git clone https://github.com/littleblakew/msds-chain-mcp.git
cd msds-chain-mcp
pip install -r requirements.txt
```

### 3. Add to your AI coding agent

> **Architecture note:** the hosted endpoint `https://mcp.lagentbot.com` sits behind a
> distribution **gateway** that terminates OAuth 2.1 (PKCE + DCR) and serves
> `/.well-known/oauth-authorization-server`. The **OAuth / browser sign-in flow described
> below applies only to that hosted gateway.** This repo's bare core server
> (`python server_remote.py`, or the Docker image) does *not* implement OAuth or serve any
> `/.well-known` endpoint — it only accepts `Authorization: Bearer sk-msds-...` or
> `X-Api-Key` passthrough. If you self-host the core directly, use a static API key; there
> is no browser sign-in step.

**Claude Code (Remote — recommended):**
```bash
claude mcp add msds-chain --transport http https://mcp.lagentbot.com/mcp
```
Then run `/mcp` in Claude Code and authenticate — a browser opens to sign in (email
code); your account is provisioned automatically and calls are metered to you (free
plan includes a monthly free quota). The legacy SSE endpoint
(`--transport sse https://mcp.lagentbot.com/sse`) still works but streamable
HTTP is preferred.

**Claude Code (Plugin — includes skill + MCP):**
```bash
/plugin install https://github.com/littleblakew/msds-chain-mcp.git
```

**Claude Code (npm — remote, shim registers the hosted endpoint):**
```bash
claude mcp add msds-chain -- npx -y msds-chain-mcp@latest
```

**claude.ai (Web):**
Search "msds-chain" in Settings > Plugins (already published).

**Manual config** (Claude Code `~/.claude.json`):

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

Restart Claude Code, run `/mcp`, and authenticate (OAuth). You should then see
`msds-chain` in the MCP tools list. (Prefer a static key instead of OAuth? Pass it as
a header — `--header "Authorization: Bearer sk-msds-your-key"` — for headless clients.)

## Claude Code Skill

The `/msds-safety-check` skill provides auto-detection and guided audit workflows.

**Plugin install (includes skill + MCP automatically):**
```bash
/plugin install https://github.com/littleblakew/msds-chain-mcp.git
```

**Manual install (skill only, if MCP already configured):**
```bash
git clone https://github.com/littleblakew/msds-chain-mcp.git /tmp/msds-chain-mcp
cp -r /tmp/msds-chain-mcp/skills/msds-safety-check .agents/skills/msds-safety-check
ln -s ../../.agents/skills/msds-safety-check .claude/skills/msds-safety-check
```

### What the Skill Does
- **Auto-detects** chemicals in your conversations and offers safety checks
- **`/msds-safety-check`** — guided audit for lab protocols or EHS compliance
- **Freemium** — basic queries work without API key, audit reports require free registration

## Usage Examples

### Experiment Protocol Review

```
User: I'm planning a Grignard reaction with magnesium turnings, diethyl ether,
      and bromobenzene. Check if this setup is safe.

Claude:
  → calls batch_safety_check(["magnesium", "diethyl ether", "bromobenzene"])
  → Returns: compatibility matrix, PPE requirements, storage grouping
```

### Opentrons Deck Safety Audit

```
User: My Opentrons deck has these in different slots:
      Slot 1: Acetone, Slot 3: Concentrated H2SO4, Slot 5: Methanol,
      Slot 7: Sodium borohydride. Any safety issues?

Claude:
  → calls check_chemical_compatibility(["acetone", "sulfuric acid", "methanol", "sodium borohydride"])
  → ⚠️ INCOMPATIBLE: Sodium borohydride + sulfuric acid (violent reaction, H2 gas evolution)
  → ⚠️ CAUTION: Acetone + sulfuric acid (exothermic)
  → Recommendation: Move sodium borohydride to maximum distance from acids
```

### Compliance Check Before Shipping

```
User: We need to ship toluene and dichloromethane to our Japan lab.
      What transport regulations apply?

Claude:
  → calls get_transport_classification(["toluene", "dichloromethane"])
  → calls check_regulatory_compliance(["toluene", "dichloromethane"], regions=["JP"])
  → Returns: UN numbers, IATA packing instructions, Japan-specific regulations
```

### Generate Signed Audit Report

```
User: Create a safety audit report for our quarterly review.
      Chemicals: acetone, methanol, ethanol, isopropanol, hexane.

Claude:
  → calls create_audit_session("Q2 2026 Solvent Cabinet Review", ["acetone", "methanol", "ethanol", "isopropanol", "hexane"])
  → calls get_audit_report("SESSION-ID")
  → Returns: Signed PDF URL (Ed25519 signature, suitable for GLP/GMP compliance)
```

### Try These Prompts (one per tool)

Each prompt reliably triggers the named tool. Use them to evaluate the connector end-to-end.

| Tool | Example prompt | What a good response shows |
|------|----------------|----------------------------|
| `search_chemical_database` | "Look up the chemical with CAS 67-64-1." | Acetone identified with synonyms + CAS |
| `ask_chemical_safety` | "Is it safe to store bleach next to ammonia?" | Plain-language hazard answer (toxic chloramine gas) |
| `check_chemical_compatibility` | "Are acetone and hydrogen peroxide compatible?" | Pairwise verdict + reaction risk |
| `get_chemical_risk_warnings` | "What are the GHS hazards of toluene?" | H-codes, signal word, flash point |
| `get_ppe_recommendation` | "What PPE do I need to handle concentrated sulfuric acid?" | Gloves/eye/respiratory/body guidance |
| `get_storage_guidance` | "How should I store sodium hydroxide and nitric acid?" | Storage class, cabinet, isolation rules |
| `get_emergency_response` | "What do I do if I spill chloroform?" | Step-by-step spill procedure |
| `get_exposure_limits` | "What's the OEL for benzene in the EU?" | OEL/TLV/PEL values by region |
| `get_transport_classification` | "How is acetone classified for air freight?" | UN number, hazard class, packing group |
| `check_regulatory_compliance` | "Is dichloromethane restricted in Japan and the EU?" | Per-region compliance status |
| `check_regulatory_lists` | "Which regulatory watch lists include formaldehyde?" | Matched lists across regions |
| `get_waste_disposal` | "How do I dispose of waste acetonitrile?" | Waste class, container, procedure |
| `check_mixing_order` | "What's the safe order to mix sulfuric acid and water?" | Correct addition sequence + why |
| `get_chemical_alternatives` | "Is there a safer substitute for hexane in extraction?" | Lower-hazard alternatives |
| `validate_protocol_chemicals` | "Validate the chemicals in this protocol: [paste text]" | Extracted + verified chemical list |
| `get_sds_section` | "Show me section 4 (first aid) of the SDS for methanol." | Requested SDS section content |
| `compare_sds_versions` | "What changed between SDS versions for acetone?" | 7-dimension structured diff |
| `batch_safety_check` | "Run a full safety check on acetone, methanol, and hexane." | Compatibility + PPE + storage report |
| `upload_msds_pdf` | "Parse this MSDS PDF and extract its hazard data." *(API key)* | Structured extraction from the PDF |
| `create_audit_session` | "Create a signed audit report for our solvent cabinet." *(API key)* | Audit session + signed PDF |
| `get_audit_report` | "Give me the download link for that audit report." *(API key)* | Signed PDF URL |

## Privacy & Data Handling

- **Privacy Policy:** [msdschain.lagentbot.com/privacy](https://msdschain.lagentbot.com/privacy)
- Read-only tools send only the chemical names / queries you provide; no personal data is required.
- API-key tools (`upload_msds_pdf`, `create_audit_session`, `get_audit_report`) associate activity with your account for audit traceability.
- Uploaded MSDS PDFs are processed for data extraction and contribute to the verified ChainSDS database per your account's data-sharing settings.

## Third-Party AI Platform Integration

Connect to the hosted MSDS Chain MCP server from any AI platform that supports MCP.

**Server URL (preferred):** `https://mcp.lagentbot.com/mcp` (streamable HTTP)

The hosted endpoint serves **both** transports — streamable HTTP (`/mcp`, preferred) and SSE (`/sse`, for clients that only speak SSE: 悟空, Dify, Coze, etc.). Calls are metered per authenticated user.

| Transport | Endpoint | Notes |
|-----------|----------|-------|
| Streamable HTTP | `https://mcp.lagentbot.com/mcp` | Preferred — broadest, most robust |
| SSE | `https://mcp.lagentbot.com/sse` | For SSE-only clients |

**Authentication:** either OAuth (the client opens a browser sign-in on first connect) or a static header — `Authorization: Bearer sk-msds-your-key` — for headless clients.

### 悟空 (Wukong)

1. 设置 → MCP 服务 → + 添加
2. 类型：**SSE**
3. 名称：`msds-chain`
4. URL：`https://mcp.lagentbot.com/sse`
5. HTTP Headers：`Authorization` : `Bearer sk-msds-your-key`
6. 点击「添加」

### Dify / Coze / other platforms

General steps:
1. Find MCP server / tool integration settings
2. Select **SSE** transport type
3. Set URL to `https://mcp.lagentbot.com/sse`
4. Add authentication header: `Authorization: Bearer sk-msds-your-key`

> **Get an API key:** Sign up at [msdschain.lagentbot.com](https://msdschain.lagentbot.com) → API Keys tab → Create Key.

---

## Remote Mode (HTTP)

For cloud deployment or shared team access, run as an HTTP server:

```bash
MSDS_API_KEY=sk-msds-xxx python server_remote.py
```

A single `server_remote.py` process always serves **both** transports concurrently —
Streamable HTTP at `/mcp` (recommended for Claude Code 2026+) and SSE at `/sse`
(compatibility). The `MSDS_MCP_TRANSPORT` env var is kept for backward compatibility
only; it is ignored at runtime.

Connect from Claude Code:

```bash
claude mcp add msds-chain --transport http https://your-server.example.com/mcp
```

### Docker

```bash
docker build -t msds-chain-mcp .
docker run -p 8080:8080 -e MSDS_API_KEY=sk-msds-xxx msds-chain-mcp
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MSDS_API_KEY` | *(required)* | API key from the MSDS Chain dashboard |
| `MSDS_API_URL` | Production URL | For development use only |
| `MSDS_LANG` | `en` | Response language: `en`, `zh`, `ja`, `de`, `id` |

## Use Cases

### Lab Automation (Opentrons / Hamilton / Tecan)
- Pre-run protocol safety audit
- Deck layout compatibility verification
- Automated run compliance reports

### Electronic Lab Notebooks (Benchling / LabArchives)
- Safety annotations on experiment entries
- Chemical registration with auto-flagging

### Pharmaceutical R&D
- Synthesis route safety screening
- Multi-region regulatory pre-checks for new compounds
- GMP-ready audit trail with signed receipts

## Data Coverage — ChainSDS

Industry-sourced, AI-verified, and cryptographically signed.

- **4,000,000+ chemical records** with multi-language aliases (EN/ZH/JA)
- **NFPA/GHS classification** for compatibility rules
- **23 regulatory watch lists across 10 regions** (structured compliance for EU, US, CN, JP, KR, CA, AU, TW, SG)
- **Occupational exposure limits** from 5 standards (OSHA PEL, ACGIH TLV, EU IOELV, JP OEL, CN GBZ)
- **UN transport data** for 16+ common lab chemicals
- **Version tracking** with 7-dimension SDS diff for regulatory updates

## Architecture

```
Your AI Agent                  MSDS Chain MCP Server
┌──────────────────┐           ┌─────────────────────────┐
│ Claude Code      │           │ 23 Safety Tools         │
│ Cursor / pi      │──MCP────▶│   ↓                     │
│ Any MCP client   │           │ ChainSDS Platform       │
└──────────────────┘           └─────────────────────────┘
```

The MCP server connects to the ChainSDS platform for verified safety reasoning.

## Development

Test locally with the MCP inspector:

```bash
export MSDS_API_KEY=sk-msds-your-key
npx @modelcontextprotocol/inspector python server.py
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `401 Unauthorized` on connect | Missing or invalid API key | Verify the `Authorization: Bearer sk-msds-...` header (or `MSDS_API_KEY` env var). Generate a fresh key at [msdschain.lagentbot.com](https://msdschain.lagentbot.com) → API Keys. |
| Tools don't appear in the client | MCP server not loaded | Fully restart the client after adding the server. Confirm `msds-chain` shows in the MCP/tools list. |
| Connection drops or times out | Wrong transport / endpoint | Prefer the streamable HTTP endpoint `https://mcp.lagentbot.com/mcp` (SSE sessions can drop when the server redeploys). Check `GET https://mcp.lagentbot.com/health` returns `{"status":"ok"}` — that is the gateway; the core's tool count lives on an internal endpoint that is not publicly reachable, so a missing `tools` field is expected. |
| `Quota exceeded` / 429 | Monthly call limit hit | Free keys are limited; upgrade your plan or wait for the monthly reset. |
| Empty results for a known chemical | Name not matched | Retry with the CAS number or a common synonym; `search_chemical_database` accepts name, synonym, or CAS. |
| OAuth login fails in a browser client | OAuth metadata unreachable (hosted gateway only — self-hosted core has no OAuth) | Confirm `GET https://mcp.lagentbot.com/.well-known/oauth-authorization-server` returns 200; the client should auto-discover the authorize/token endpoints. If self-hosting `server_remote.py` directly, skip OAuth and use a static `Authorization: Bearer` key instead. |

Still stuck? Email **contact@lagentbot.com** or open an issue at [github.com/littleblakew/msds-chain-mcp/issues](https://github.com/littleblakew/msds-chain-mcp/issues).

## Roadmap

- [x] `get_waste_disposal` — waste classification and disposal guidance
- [x] `check_mixing_order` — safe addition sequence for reagent pairs
- [x] `get_chemical_alternatives` — safer substitutes for restricted chemicals
- [x] Remote MCP (HTTP SSE / Streamable HTTP) for cloud-hosted access
- [x] OAuth 2.1 for Claude Marketplace integration (served by the distribution gateway in front of `mcp.lagentbot.com`, not by this core server)

## License

MIT

## About

Built by [LAgentBot](https://lagentbot.com) — AI-powered chemical safety infrastructure.

Part of the [MSDS Chain](https://msdschain.lagentbot.com) platform — the world's first AI Agent-driven chemical safety data trust network, powered by [ChainSDS](https://msdschain.lagentbot.com): verified, current, and growing.
