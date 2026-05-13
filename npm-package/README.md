# @lagentbot/msds-chain-mcp

Chemical safety intelligence for AI-assisted experiment design. Powered by **ChainSDS** — verified, current, and growing.

This is a remote MCP (Model Context Protocol) server providing **20 tools** for chemical safety reasoning — compatibility checks, hazard analysis, PPE recommendations, storage guidance, waste disposal, mixing order safety, exposure limits, transport classification, regulatory compliance, and signed audit reports.

## Quick Start

**Claude Code:**
```bash
claude mcp add msds-chain --transport sse --url https://mcp.lagentbot.com/sse
```

**claude.ai (Web):** Search "msds-chain" in Settings > Plugins.

**Any MCP client (manual config):**
```json
{
  "mcpServers": {
    "msds-chain": {
      "type": "sse",
      "url": "https://mcp.lagentbot.com/sse"
    }
  }
}
```

## API Key

Get a free API key (100 calls/month) at [msdschain.lagentbot.com](https://msdschain.lagentbot.com) to unlock all tools.

## Tools (20)

| Tool | Description |
|------|-------------|
| `batch_safety_check` | One-call comprehensive report: compatibility + PPE + storage |
| `validate_protocol_chemicals` | Extract chemical names from protocol text or code |
| `check_chemical_compatibility` | Pairwise compatibility matrix |
| `check_mixing_order` | Safe addition sequence for reagent pairs |
| `get_chemical_risk_warnings` | GHS classification, H-codes, signal words |
| `get_ppe_recommendation` | Gloves, eye, respiratory, body protection |
| `get_storage_guidance` | Storage class, cabinet type, isolation rules |
| `get_waste_disposal` | Waste classification and disposal procedures |
| `get_emergency_response` | Spill, fire, or exposure procedures |
| `get_exposure_limits` | OEL/TLV/PEL across US, EU, JP, CN, INT |
| `get_transport_classification` | UN number, hazard class, packing group |
| `check_regulatory_compliance` | EU, US, CN, JP, KR, CA, AU, TW |
| `get_chemical_alternatives` | Safer substitutes for restricted chemicals |
| `get_sds_section` | Query specific SDS sections (1-16) |
| `search_chemical_database` | Look up by name, synonym, or CAS number |
| `ask_chemical_safety` | Natural language catch-all |
| `create_audit_session` | Full audit with signed PDF report |
| `get_audit_report` | Download signed audit report |
| `upload_msds_pdf` | Upload PDF or URL, extract & parse MSDS |
| `compare_sds_versions` | Structured SDS version diff (7 dimensions) |

## Coverage — ChainSDS

- 28,000+ chemicals with multi-language aliases (EN/ZH/JA)
- 8 regulatory jurisdictions (EU/US/CN/JP/KR/CA/AU/TW)
- 5 exposure limit standards (OSHA/ACGIH/EU/JP/CN)
- 5 languages (EN/ZH/JA/DE/ID)
- Version tracking with 7-dimension SDS diff

## Links

- [GitHub](https://github.com/littleblakew/msds-chain-mcp) — Full source code (Python MCP server)
- [Smithery](https://smithery.ai/servers/lagentbot/msds-chain) — Smithery registry listing
- [Homepage](https://msdschain.lagentbot.com) — Web app & API key management

## License

MIT
