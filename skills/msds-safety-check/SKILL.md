---
name: msds-safety-check
description: Chemical safety intelligence for lab protocols and EHS compliance — auto-detects chemicals in context, runs safety checks via MSDS Chain MCP tools
metadata:
  tags: chemistry, safety, msds, sds, ghs, compliance, lab, ppe, hazard, mcp
---

# MSDS Safety Check

Chemical safety intelligence powered by [MSDS Chain](https://msdschain.lagentbot.com). Provides compatibility checks, PPE recommendations, storage guidance, regulatory compliance, and signed audit reports for lab workflows.

## When to Use

**Automatically triggered** when the user's message contains:
- Chemical names (e.g., Acetone, HCl, Sulfuric acid, 硫酸, アセトン)
- CAS numbers (e.g., 67-64-1, 7647-01-0)
- Lab protocol keywords (protocol, experiment, reagent, mixture, dilution, 配制, 混合, 稀释)
- EHS keywords (compliance, SDS, MSDS, hazard, PPE, safety data sheet)

**Manually triggered** via `/msds-safety-check` for full guided audit.

## How It Works

This skill orchestrates MSDS Chain's 20 MCP tools. It requires the `msds-chain` MCP server to be configured. If not configured, it will guide the user through setup (see [rules/setup-guide.md](rules/setup-guide.md)).

### Two Modes

1. **Auto-detect mode** — When chemicals are detected in context, prompt the user: "Detected chemicals in your content. Run a safety check?" If confirmed, run a quick scan and output a concise safety brief. Details in [rules/auto-detect.md](rules/auto-detect.md).

2. **Full audit mode** (`/msds-safety-check`) — Guide the user through chemical input, check type selection, and produce a detailed report. Two workflows:
   - **Lab Protocol Audit** (for researchers): [rules/lab-protocol-audit.md](rules/lab-protocol-audit.md)
   - **EHS Compliance Review** (for compliance officers): [rules/compliance-review.md](rules/compliance-review.md)

### Output Formats

Output adapts to context — concise for auto-detect, detailed for manual audit. See [rules/output-formats.md](rules/output-formats.md).

### Tool Call Principles

- **Minimum calls first:** Use `batch_safety_check` for one-shot results (compatibility + PPE + storage). Don't call individual tools unless the user asks to drill down.
- **Progressive disclosure:** Only call `get_emergency_response`, `check_mixing_order`, or `get_chemical_alternatives` when the user asks follow-up questions.
- **Freemium upgrade:** Audit tools (`create_audit_session`, `get_audit_report`, `upload_msds_pdf`) require an API key. Prompt for registration only when the user requests audit functionality, not during basic queries.

## Rules

- [rules/auto-detect.md](rules/auto-detect.md) — Auto-detection patterns and prompt behavior
- [rules/lab-protocol-audit.md](rules/lab-protocol-audit.md) — Researcher workflow
- [rules/compliance-review.md](rules/compliance-review.md) — EHS compliance workflow
- [rules/output-formats.md](rules/output-formats.md) — Output format definitions
- [rules/setup-guide.md](rules/setup-guide.md) — MCP server setup and degradation behavior
