# MSDS Chain MCP Server

**Chemical safety intelligence for AI-assisted experiment design.**

An [MCP](https://modelcontextprotocol.io) server that gives AI agents (Claude Code, Cursor, Copilot, etc.) access to chemical safety reasoning — compatibility checks, hazard analysis, regulatory compliance, PPE recommendations, storage guidance, and more.

Built for researchers who design experiments with AI and need safety verification integrated into their workflow.

## Why This Exists

When you use Claude to plan a synthesis route or set up an Opentrons protocol, safety validation shouldn't be a separate step. This MCP server lets your AI assistant automatically:

- Check if chemicals on the same deck are compatible
- Flag dangerous mixing orders
- Recommend PPE for the specific chemicals you're handling
- Verify compliance with EU REACH, US OSHA/TSCA, and 6 other jurisdictions
- Generate signed audit reports for GLP/GMP compliance

## Tools (18)

| Tool | Description |
|------|-------------|
| **`batch_safety_check`** | One-call comprehensive report: compatibility + PPE + storage grouping for a chemical list |
| **`get_sds_section`** | Retrieve a specific SDS section (1-16) for a chemical |
| **`get_chemical_alternatives`** | Safer substitutes for restricted or high-risk chemicals |
| **`validate_protocol_chemicals`** | Extract & validate chemical names from protocol text or code |
| **`check_mixing_order`** | Safe addition sequence for reagent pairs (e.g., acid into water) |
| **`get_waste_disposal`** | Waste classification, container type, and disposal procedures |
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

### 3. Add to Claude Code

```bash
claude mcp add msds-chain -- python /path/to/msds-chain-mcp/server.py
```

Or manually edit `~/.claude.json`:

```json
{
  "mcpServers": {
    "msds-chain": {
      "command": "python",
      "args": ["/absolute/path/to/msds-chain-mcp/server.py"],
      "env": {
        "MSDS_API_KEY": "sk-msds-your-key-here"
      }
    }
  }
}
```

Restart Claude Code. You should see `msds-chain` in the MCP tools list.

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

## Data Coverage

- **4,200+ chemicals** with multi-language aliases (EN/ZH/JA)
- **NFPA/GHS classification** for compatibility rules
- **8 regulatory jurisdictions** (EU, US, CN, JP, KR, CA, AU, TW)
- **Occupational exposure limits** from 5 standards (OSHA PEL, ACGIH TLV, EU IOELV, JP OEL, CN GBZ)
- **UN transport data** for 16+ common lab chemicals

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
- [ ] Remote MCP (HTTP SSE) for cloud-hosted access
- [ ] OAuth 2.1 for Claude Marketplace integration

## License

MIT

## About

Built by [LAgentBot](https://lagentbot.com) — AI-powered chemical safety infrastructure.

Part of the [MSDS Chain](https://msdschain.lagentbot.com) platform: the world's first AI Agent-driven chemical safety data trust network.
