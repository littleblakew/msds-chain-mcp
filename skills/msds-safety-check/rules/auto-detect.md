# Auto-Detection Rules

## When to Trigger

Scan the user's message for any of these patterns. If one or more match, this skill applies.

### Chemical Identifiers
- **CAS numbers:** Pattern `\d{2,7}-\d{2}-\d` (e.g., 67-64-1, 7647-01-0)
- **Chemical names (English):** Acetone, Hydrochloric acid, Sulfuric acid, Sodium hydroxide, Ethanol, Methanol, Toluene, Benzene, Formaldehyde, Ammonia, Nitric acid, Acetic acid, Hydrogen peroxide, Chloroform, Dichloromethane, Acetonitrile, Dimethylformamide, Isopropanol, Hexane, Xylene, Phenol, Potassium permanganate, and similar
- **Chemical names (Chinese):** 丙酮, 盐酸, 硫酸, 氢氧化钠, 乙醇, 甲醇, 甲苯, 苯, 甲醛, 氨, 硝酸, 乙酸, 双氧水, 氯仿, 二氯甲烷, 乙腈, and similar
- **Chemical names (Japanese):** アセトン, 塩酸, 硫酸, エタノール, メタノール, トルエン, ホルムアルデヒド, and similar
- **Chemical formulas:** HCl, H2SO4, NaOH, H2O2, CH3COOH, NH3, HNO3, etc.

### Context Keywords
- **Lab protocol:** protocol, experiment, reagent, mixture, dilution, preparation, synthesis, reaction, titration, assay, buffer, stock solution, working solution
- **Lab protocol (Chinese):** 实验, 试剂, 配制, 混合, 稀释, 合成, 反应, 滴定, 缓冲液
- **EHS/Safety:** compliance, SDS, MSDS, safety data sheet, hazard, GHS, PPE, exposure limit, OSHA, REACH, CLP, waste disposal, chemical storage, fume hood, spill

### Non-Triggers (Do NOT Activate)
- Generic chemistry education questions without specific chemicals ("what is a covalent bond?")
- Code variable names that happen to match chemical names (check surrounding context)
- Historical/news references to chemicals without lab context

## Behavior When Triggered

1. **Do NOT run tools automatically.** Always prompt the user first:

> I noticed chemicals in your content: **[list detected chemicals]**. Want me to run a safety check? I can assess:
> - Compatibility between these chemicals
> - Required PPE
> - Storage requirements
>
> Just say **"yes"** or tell me what you need.

2. If the user confirms, read [lab-protocol-audit.md](lab-protocol-audit.md) and follow the quick scan workflow (Scenario A, Steps 1-2).

3. If the user declines, do not prompt again for the same set of chemicals in this conversation.

## MCP Not Available

If the `msds-chain` MCP tools are not available when triggered, follow [setup-guide.md](setup-guide.md) for degradation behavior.
