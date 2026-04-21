# EHS Compliance Review Workflow

For EHS compliance officers conducting regulatory audits. Triggered when the user runs `/msds-safety-check` and selects "compliance review" or when compliance-specific keywords are detected.

## Guided Flow

### Step 1: Gather Inputs

Ask the user:
1. **Chemicals:** List of chemicals to audit (or a protocol/document to extract from)
2. **Regions:** Target regulatory regions. Available: `EU`, `US`, `CN`, `JP`, `KR`, `CA`, `AU`, `TW`. Default to `US` and `EU` if not specified.

If the user provides a protocol text, extract chemicals first:
```
validate_protocol_chemicals(protocol_text="<protocol text>")
```

### Step 2: Regulatory Compliance Check

```
check_regulatory_compliance(chemicals=["Chemical A", "Chemical B"], regions=["EU", "US"])
```
Returns: REACH/CLP status (EU), OSHA/TSCA status (US), and equivalents for other regions.

### Step 3: Exposure Limits

```
get_exposure_limits(chemicals=["Chemical A", "Chemical B"], region="US")
```
Returns: OSHA PEL, ACGIH TLV, and region-specific occupational exposure limits.

### Step 4: Full Risk Assessment

Run these in sequence:
```
get_chemical_risk_warnings(chemicals=["Chemical A", "Chemical B"])
```
Returns: GHS classification, signal words, H-codes, P-codes, flash point.

```
get_transport_classification(chemicals=["Chemical A", "Chemical B"])
```
Returns: UN number, proper shipping name, hazard class, packing group.

```
get_waste_disposal(chemicals=["Chemical A", "Chemical B"])
```
Returns: Waste classification, disposal method, incompatible waste streams.

### Step 5: Generate Audit Report (Requires API Key)

```
create_audit_session(experiment_name="<user-provided name>", chemicals=["Chemical A", "Chemical B"])
```
Then:
```
get_audit_report(session_id="<returned session_id>")
```

Format all results as a **detailed report** (see [output-formats.md](output-formats.md#detailed-report)).

If API key is not configured, present the gathered data in narrative form and suggest registering for signed reports (see [setup-guide.md](setup-guide.md#api-key-upgrade)).

## SDS Version Comparison (On Request)

If the user needs to compare SDS versions:
```
compare_sds_versions(chemical="Acetone", version_old="2023-01", version_new="2025-06")
```
Returns: 7-dimension structured diff (hazard, H-codes, PPE, exposure limits, regulatory, storage, transport changes).

## SDS Section Lookup (On Request)

If the user needs a specific SDS section:
```
get_sds_section(chemical="Acetone", section=8)
```
Sections: 1 (Identification), 2 (Hazards), 3 (Composition), 4 (First Aid), 5 (Firefighting), 6 (Spill), 7 (Handling/Storage), 8 (Exposure/PPE), 9 (Physical/Chemical), 10 (Stability), 11 (Toxicology), 12 (Ecology), 13 (Disposal), 14 (Transport), 15 (Regulatory), 16 (Other).
