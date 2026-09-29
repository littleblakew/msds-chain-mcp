# Lab Protocol Audit Workflow

For lab researchers writing or reviewing experimental protocols.

## Quick Scan (Auto-detect Follow-up)

When the user confirms a safety check from auto-detect, run these steps:

### Step 1: Extract Chemicals

If chemicals are already identified from auto-detect, use those. Otherwise:

- If the user provides a block of protocol text, call:
  ```
  validate_protocol_chemicals(protocol_text="<the protocol text>")
  ```
  This extracts chemical names and validates them against the database.

- If the user lists chemicals directly, use those names as-is.

### Step 2: Batch Safety Check

Call once for the pairwise picture — compatibility matrix + key risk warnings with the
source SDS for each. 🔴 It does **not** return PPE or storage grouping; those are
separate calls (`get_ppe_recommendation` / `get_storage_guidance`), so do not present
them as already covered:
```
batch_safety_check(chemicals=["Chemical A", "Chemical B", "Chemical C"])
```

Format the output as a **concise safety brief** (see [output-formats.md](output-formats.md#concise-summary)).

### Step 3: Offer Follow-ups

After presenting the safety brief, offer:
> Want me to go deeper? I can check:
> - **Mixing order** — safe addition sequence for specific pairs
> - **Alternatives** — safer substitutes for hazardous chemicals
> - **Emergency response** — spill/fire/exposure procedures
> - **Full audit** — create a signed session with PDF report (requires API key)

## Deep Dive (On Request)

### Mixing Order
When the user asks about mixing order for a specific pair:
```
check_mixing_order(chemical_a="Sulfuric acid", chemical_b="Water", context="dilution for titration")
```

### Safer Alternatives
When the user asks for alternatives:
```
get_chemical_alternatives(chemical="Chloroform", use_case="DNA extraction solvent")
```

### Emergency Response
When the user asks about emergency procedures:
```
# 用户说的是「HF 溅到手上」⇒ 人沾到了 ⇒ exposure（不是 spill，也不是 skin contact）
get_emergency_response(chemical="Hydrofluoric acid", scenario="exposure")
```
Valid scenarios: **exactly three** — `spill`, `fire`, `exposure`. Anything else is
rejected by the tool, so map the incident before calling.

🔴 **PERSON FIRST (CI-1020):** if the material reached a person, use `exposure` even if
the incident is also a spill and the user said "spilled". Skin contact, splash,
inhalation, eye contact and ingestion all map to `exposure`; a burn *on a person* is
`exposure`, not `fire`. Only `exposure` returns the substance-specific first-aid
protocol (e.g. calcium gluconate for HF) — picking `spill` for a contact incident
silently returns cleanup guidance instead of the antidote.

### Full Audit Session (Requires API Key)
When the user requests a formal audit:
```
create_audit_session(experiment_name="PCR Buffer Preparation", chemicals=["Tris", "HCl", "EDTA", "KCl"])
```
Then:
```
get_audit_report(session_id="<returned session_id>")
```
This generates a signed PDF report. If API key is not configured, follow [setup-guide.md](setup-guide.md#api-key-upgrade).
