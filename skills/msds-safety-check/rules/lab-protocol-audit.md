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

Call once to get compatibility + PPE + storage in a single response:
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
get_emergency_response(chemical="Hydrofluoric acid", scenario="skin contact")
```
Valid scenarios: `spill`, `fire`, `inhalation`, `skin contact`, `eye contact`, `ingestion`.

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
