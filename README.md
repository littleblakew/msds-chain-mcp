# MSDS Chain MCP Server

**Chemical safety intelligence for AI-assisted experiment design.**

Powered by **ChainSDS** — a verified, always-current chemical safety database. *Verified. Current. Growing.*

An [MCP](https://modelcontextprotocol.io) server that gives AI agents (Claude Code, Cursor, Copilot, etc.) access to chemical safety reasoning — compatibility checks, hazard analysis, regulatory compliance, PPE recommendations, storage guidance, and more.

Built for researchers who design experiments with AI and need safety verification integrated into their workflow.

## Why This Exists

When you use Claude to plan a synthesis route or set up an Opentrons protocol, safety validation shouldn't be a separate step. This MCP server lets your AI assistant automatically:

- Check if chemicals on the same deck are compatible
- Flag dangerous mixing orders
- Recommend PPE for the specific chemicals you're handling
- Verify compliance with EU REACH, US OSHA/TSCA, and 6 other jurisdictions
- Generate signed audit reports for GLP/GMP compliance

## Tools (20)

| Tool | Description |
|------|-------------|
| **`batch_safety_check`** | One-call comprehensive report: compatibility + PPE + storage grouping for a chemical list |
| **`get_sds_section`** | Retrieve a specific SDS section (1-16) for a chemical |
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
| `check_regulatory_compliance` | Multi-region: EU, US, CN, JP, KR, CA, AU, TW |
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

**Claude Code (Remote — recommended):**
```bash
claude mcp add msds-chain --transport sse --url https://mcp.lagentbot.com/sse
```

**Claude Code (Plugin — includes skill + MCP):**
```bash
/plugin install https://github.com/littleblakew/msds-chain-mcp.git
```

**Claude Code (npm — local):**
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
      "type": "sse",
      "url": "https://mcp.lagentbot.com/sse",
      "env": {
        "MSDS_API_KEY": "sk-msds-your-key-here"
      }
    }
  }
}
```

Restart Claude Code. You should see `msds-chain` in the MCP tools list.

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

## Third-Party AI Platform Integration

Connect to the hosted MSDS Chain MCP server from any AI platform that supports MCP.

**Server URL:** `https://mcp.lagentbot.com`

| Transport | Endpoint | When to use |
|-----------|----------|-------------|
| SSE | `https://mcp.lagentbot.com/sse` | Most third-party platforms (悟空, Dify, Coze, etc.) |
| Streamable HTTP | `https://mcp.lagentbot.com/mcp` | Claude Code 2026+, newer MCP clients |

**Authentication:** Add an HTTP header — `Authorization: Bearer sk-msds-your-key`

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
# Streamable HTTP (recommended for Claude Code 2026+)
MSDS_API_KEY=sk-msds-xxx python server_remote.py

# Or SSE mode
MSDS_MCP_TRANSPORT=sse MSDS_API_KEY=sk-msds-xxx python server_remote.py
```

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
| `MSDS_API_URL` | Production URL | Override to point at dev/self-hosted instance |
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

- **28,000+ chemicals** with multi-language aliases (EN/ZH/JA)
- **NFPA/GHS classification** for compatibility rules
- **8 regulatory jurisdictions** (EU, US, CN, JP, KR, CA, AU, TW)
- **Occupational exposure limits** from 5 standards (OSHA PEL, ACGIH TLV, EU IOELV, JP OEL, CN GBZ)
- **UN transport data** for 16+ common lab chemicals
- **Version tracking** with 7-dimension SDS diff for regulatory updates

## Architecture

```
Your AI Agent (Brain)          MSDS Chain MCP (Hands)
┌──────────────────┐           ┌─────────────────────────┐
│ Claude Code      │──MCP────▶│ server.py (stdio/SSE)   │
│ Cursor           │           │   ↓                     │
│ Any MCP client   │           │ MSDS Chain Backend API  │
└──────────────────┘           │   ↓                     │
                               │ Safety Reasoning Engine │
                               │ (Rules + LLM fallback)  │
                               └─────────────────────────┘
```

The MCP server is a thin client — all safety reasoning happens on the MSDS Chain backend (rule engine + Azure OpenAI GPT-4o fallback for edge cases).

## Development

Test locally with the MCP inspector:

```bash
export MSDS_API_KEY=sk-msds-your-key
npx @modelcontextprotocol/inspector python server.py
```

## Roadmap

- [x] `get_waste_disposal` — waste classification and disposal guidance
- [x] `check_mixing_order` — safe addition sequence for reagent pairs
- [x] `get_chemical_alternatives` — safer substitutes for restricted chemicals
- [x] Remote MCP (HTTP SSE / Streamable HTTP) for cloud-hosted access
- [x] OAuth 2.1 for Claude Marketplace integration (skeleton — needs Redis/DB for production)

## License

MIT

## About

Built by [LAgentBot](https://lagentbot.com) — AI-powered chemical safety infrastructure.

Part of the [MSDS Chain](https://msdschain.lagentbot.com) platform — the world's first AI Agent-driven chemical safety data trust network, powered by [ChainSDS](https://msdschain.lagentbot.com): verified, current, and growing.
