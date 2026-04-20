# Claude for Life Sciences Marketplace Submission

## Submission Paths

### Path A: Submit to `anthropics/life-sciences` (recommended)
1. Fork `github.com/anthropics/life-sciences`
2. Add our entry to `marketplace.json`
3. Submit PR with description below

### Path B: Submit via platform.claude.com
1. Go to `platform.claude.com/plugins/submit`
2. Fill in metadata from `.claude-plugin/marketplace.json`
3. Provide GitHub repo URL

---

## PR Template for anthropics/life-sciences

### Title
`Add MSDS Chain — Chemical Safety Intelligence MCP Server`

### Body

```markdown
## Summary

Adding MSDS Chain as a chemical safety MCP server for the Life Sciences marketplace.

**What it does:** Provides 18 chemical safety reasoning tools — compatibility checks,
hazard analysis, PPE recommendations, storage guidance, waste disposal, mixing order
safety, regulatory compliance (8 jurisdictions), exposure limits, transport classification,
and signed audit reports.

**Why it belongs here:** Researchers using Claude to design experiments need safety
verification integrated into their workflow. MSDS Chain fills the "chemical safety
reasoning" gap — no existing marketplace plugin covers this.

**Target users:** Lab researchers, EHS officers, pharmaceutical R&D teams, lab automation
engineers (Opentrons/Hamilton).

## Plugin Details

- **Repository:** https://github.com/littleblakew/msds-chain-mcp
- **License:** MIT
- **Transport:** stdio (local) + streamable-http (remote)
- **Auth:** API key (local) / OAuth 2.1 (remote)
- **Languages:** 5 (English, Chinese, Japanese, German, Indonesian)
- **Data coverage:** 4,200+ chemicals, 8 regulatory jurisdictions

## Tools (18)

| Tool | Description |
|------|-------------|
| batch_safety_check | One-call comprehensive safety report |
| validate_protocol_chemicals | Extract chemicals from protocol text |
| check_chemical_compatibility | Pairwise compatibility matrix |
| check_mixing_order | Safe addition sequence for reagent pairs |
| get_chemical_risk_warnings | GHS classification + hazard codes |
| get_ppe_recommendation | PPE requirements by chemical |
| get_storage_guidance | Storage class + isolation rules |
| get_waste_disposal | Waste classification + disposal method |
| get_emergency_response | Spill/fire/exposure procedures |
| get_exposure_limits | OEL/TLV/PEL across 5 standards |
| get_transport_classification | UN transport data (ADR/IATA/IMDG) |
| check_regulatory_compliance | Multi-region (EU/US/CN/JP/KR/CA/AU/TW) |
| get_chemical_alternatives | Safer substitutes for restricted chemicals |
| get_sds_section | Query specific SDS sections (1-16) |
| search_chemical_database | Look up by name/synonym/CAS |
| ask_chemical_safety | Natural language catch-all |
| create_audit_session | Full audit with signed PDF |
| get_audit_report | Download signed audit report |

## Testing

Tested with Claude Code + MCP inspector. Example:

```
claude mcp add msds-chain -- python /path/to/server.py
> Check compatibility between acetone, sulfuric acid, and sodium borohydride
→ ⚠️ INCOMPATIBLE: sodium borohydride + sulfuric acid (violent H2 evolution)
```

## Security & Privacy

- No query data stored or used for training
- API key required, transmitted over HTTPS only
- SECURITY.md included in repo
- ISO 27001 certification in progress

## Checklist

- [x] Repository is public
- [x] MIT licensed
- [x] README with setup instructions and examples
- [x] SECURITY.md with data handling policy
- [x] marketplace.json in .claude-plugin/
- [x] Works in stdio mode (no external dependencies for basic use)
- [x] Remote mode available (streamable-http + OAuth 2.1)
```

---

## Pre-submission Checklist

- [x] Independent GitHub repo created (`littleblakew/msds-chain-mcp`)
- [x] README with Quick Start, tool docs, examples
- [x] SECURITY.md with privacy policy
- [x] LICENSE (MIT)
- [x] `.claude-plugin/marketplace.json`
- [x] Remote MCP support (server_remote.py + Dockerfile)
- [x] OAuth 2.1 skeleton (oauth.py)
- [ ] **Deploy remote instance** to production URL (e.g., `mcp.lagentbot.com`)
- [ ] **Verify OAuth flow** end-to-end with Claude Code
- [ ] **Fork anthropics/life-sciences** and submit PR
- [ ] Or submit via platform.claude.com/plugins/submit

## Notes

- The `anthropics/life-sciences` repo currently has: PubMed, BioRender, Synapse,
  Wiley Scholar Gateway, 10x Genomics, and several skills. No chemical safety tool exists.
- We would be the first chemical safety / EHS provider in the marketplace.
- Category should be "life-sciences" (matches existing marketplace structure).
