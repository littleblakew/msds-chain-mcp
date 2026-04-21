# Output Formats

## When to Use Which

| Trigger | Format | Rationale |
|---------|--------|-----------|
| Auto-detect (user confirms quick check) | Concise Summary | Don't interrupt workflow with walls of text |
| `/msds-safety-check` manual trigger | Detailed Report | User explicitly wants thorough analysis |
| Follow-up drill-down | Inline Detail | Add to existing context without repeating |

## Concise Summary

Used after `batch_safety_check` in auto-detect mode. Template:

```
Safety Brief: [N] chemicals
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Compatibility: [summary — e.g., "1 INCOMPATIBLE pair (X + Y — reason)"]
PPE: [merged list — e.g., "Nitrile gloves, safety goggles, fume hood"]
Storage: [grouping — e.g., "2 groups: acids (Cabinet A) / bases (Cabinet B)"]

[If any INCOMPATIBLE pairs exist:]
⚠ WARNING: [Chemical A] and [Chemical B] are incompatible — [reason]. Store separately and do not mix.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
→ "details" for full report | "mixing order" for addition sequence | "audit" for signed PDF
```

Keep it under 15 lines. No narrative. Just facts.

## Detailed Report

Used in `/msds-safety-check` manual mode. Structure:

### Header
```
# Safety Assessment: [Experiment Name or Chemical List]
Date: [YYYY-MM-DD]
Chemicals: [list]
Regions: [if compliance mode]
```

### Per-Chemical Section
For each chemical, include:
- **Identity:** Name, CAS, molecular formula
- **GHS Classification:** Hazard category, signal word, H-codes, P-codes
- **Key Hazards:** Flash point, toxicity route, target organs
- **PPE Required:** Specific glove material, eye protection type, respiratory protection
- **Storage:** Temperature range, cabinet type, incompatible materials

### Cross-Chemical Analysis
- **Compatibility Matrix:** Pairwise results, highlight INCOMPATIBLE pairs with reason
- **Merged PPE:** Combined requirements (worst-case from all chemicals)
- **Storage Grouping:** Which chemicals can share storage, which must be separated

### Compliance (if applicable)
- **Regulatory Status:** Per region, per chemical
- **Exposure Limits:** Table with OSHA PEL / ACGIH TLV / region-specific limits
- **Transport:** UN number, hazard class, packing group

### Recommendations
- Narrative summary of key safety concerns
- Specific action items for the user
- Link to signed PDF report (if audit session was created)

## Inline Detail

When the user asks a follow-up (e.g., "what about mixing order for H2SO4 and water?"), respond with just the relevant detail — don't repeat the full brief or report.

## Language

- Match the user's language. If they write in Chinese, respond in Chinese.
- Chemical names should always include the English name and CAS number for unambiguous identification.
- Use `MSDS_LANG` environment variable as a hint, but user's conversation language takes precedence.
