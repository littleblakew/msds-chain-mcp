from __future__ import annotations

import httpx

from msds_mcp.app import API_URL, LANG, TIMEOUT, TIMEOUT_MULTI
from msds_mcp.calls import (_billed_json, _headers, _raise_for_status_with_reason, _with_suppliers)
from msds_mcp.params import _normalize_catalog_lang



async def _direct_compat(chemicals: list[str], lang: str | None = None,
                       suppliers: dict[str, str] | None = None) -> dict:
    """POST /api/v2/compatibility/check — direct service layer, bounded LLM fallback."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/compatibility/check",
            json=_with_suppliers(
                {"chemicals": chemicals, "lang": _normalize_catalog_lang(lang or LANG)},
                chemicals, suppliers),
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_risk(chemicals: list[str], lang: str | None = None,
                       suppliers: dict[str, str] | None = None) -> dict:
    """POST /api/v2/risk-warnings — direct service layer, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/risk-warnings",
            json=_with_suppliers(
                {"chemicals": chemicals, "lang": _normalize_catalog_lang(lang or LANG)},
                chemicals, suppliers),
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_batch(chemicals: list[str], lang: str | None = None,
                       suppliers: dict[str, str] | None = None) -> dict:
    """POST /api/v2/batch-safety — combined compat + risk, bounded LLM fallback."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/batch-safety",
            json=_with_suppliers(
                {"chemicals": chemicals, "lang": _normalize_catalog_lang(lang or LANG)},
                chemicals, suppliers),
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_route_review(steps: list[dict], lang: str | None = None,
                               include_handling: bool = False) -> dict:
    """POST /api/v2/route-review: per-step and step-to-step incompatibility screen."""
    body: dict = {"steps": steps, "lang": _normalize_catalog_lang(lang or LANG)}
    if include_handling:
        body["include_handling"] = True
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/route-review",
            json=body,
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_thermal_hazard(inputs: dict, lang: str | None = None) -> dict:
    """POST /api/v2/thermal-hazard: structural screening plus caller-supplied process values."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/thermal-hazard",
            json={**inputs, "lang": _normalize_catalog_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_ppe(chemicals: list[str], lang: str | None = None,
                       suppliers: dict[str, str] | None = None) -> dict:
    """POST /api/v2/ppe-recommendation — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/ppe-recommendation",
            json=_with_suppliers(
                {"chemicals": chemicals, "lang": _normalize_catalog_lang(lang or LANG)},
                chemicals, suppliers),
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_storage(chemicals: list[str], lang: str | None = None,
                       suppliers: dict[str, str] | None = None) -> dict:
    """POST /api/v2/storage-guidance — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/storage-guidance",
            json=_with_suppliers(
                {"chemicals": chemicals, "lang": _normalize_catalog_lang(lang or LANG)},
                chemicals, suppliers),
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_sds_draft(ingredients: list[dict], sections: list[int] | None,
                           region: str | None, lang: str | None = None) -> dict:
    """POST /api/v2/sds-draft — direct, no LLM (CI-1138).

    Takes a list, so it sits on the multi-component budget: 30 ingredients (the
    backend cap) measured ~8s on Prod.
    """
    payload: dict = {"ingredients": ingredients,
                     "lang": _normalize_catalog_lang(lang or LANG)}
    if sections:
        payload["sections"] = sections
    if region and region.strip():
        payload["region"] = region.strip()
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(f"{API_URL}/api/v2/sds-draft", json=payload,
                                headers=_headers())
        return _billed_json(res)


async def _direct_emergency(chemical: str, scenario: str, lang: str | None = None) -> dict:
    """POST /api/v2/emergency-response — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/emergency-response",
            json={"chemical": chemical, "scenario": scenario, "lang": _normalize_catalog_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_compliance(chemical: str, regions: list[str],
                             lang: str | None = None) -> dict:
    """POST /api/v2/compliance — direct rule engine, no LLM.

    🔴 CI-361：`lang` 此前发的是模块级 `LANG`（服务端环境变量，托管网关上**恒 `en`**）
    ⇒ 调用方说什么语言都没用。

    🔴 写法必须逐字是 `_normalize_catalog_lang(lang or LANG)`（CI-1095 起本端点属
    **catalog 族**）—— **归一化要盖住 `LANG` 那一侧**，不是只盖住调用方给的值。
    `MSDS_LANG` 被配成后端不认的值时，**只归一化调用方那一侧、让 `LANG` 直通**的写法
    会把那个非法值原样转发，而别的工具都会把它夹成 `en`。
    ⚠️ 今天 `LANG` 恒 `en` 所以两种写法行为相同 ——**这正是它不会被任何测试抓到的原因**。
    守卫 `test_lang_param.py::test_lang_forwarding_has_exactly_one_spelling`
    钉住只能有这一种拼写。
    🔴 上面那个反例**故意不写成可执行的字面量**：全量替换会命中散文里的反例并把它改成反的，
    而散文那一半不会有任何东西报错（命中代码会立刻炸，命中反例只是那句话从此是假的）。
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/compliance",
            json={"chemical": chemical, "regions": regions,
                  "lang": _normalize_catalog_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_validate_sds(session_id: str, msds_id: int | None,
                               lang: str | None = None) -> dict | None:
    """POST /api/v2/validate-sds (one upload) or /validate-sds/session (all of them).

    Returns None on 403/404: the backend answers "not yours" and "does not exist" the
    same way, and so do we. The session form checks up to 50 documents in one call,
    so it sits on the multi-component budget.
    """
    # Two literal `post(f"{API_URL}/api/v2/...", json={...})` calls rather than one
    # URL in a variable: the CI-822 backend-contract snapshot only sees endpoints
    # (and the parameters sent) when they are written this way.
    if msds_id is None:
        async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
            res = await client.post(
                f"{API_URL}/api/v2/validate-sds/session",
                json={"session_id": session_id, "jurisdiction": "US", "lang": _normalize_catalog_lang(lang or LANG)},
                headers=_headers())
    else:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            res = await client.post(
                f"{API_URL}/api/v2/validate-sds",
                json={"session_id": session_id, "msds_id": msds_id,
                      "jurisdiction": "US", "lang": _normalize_catalog_lang(lang or LANG)},
                headers=_headers())
    # 403 too: a session that is not the caller's is the same answer to them.
    if res.status_code in (403, 404):
        return None
    return _billed_json(res)


async def _direct_online_search(chemical_name: str = "", cas_number: str = "") -> dict:
    """POST /api/v2/online-search — stateless PubChem GHS fallback (SE-19), unmetered."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/online-search",
            json={"chemical_name": chemical_name, "cas_number": cas_number, "lang": LANG},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_exposure(chemicals: list[str], region: str | None = None) -> dict:
    """POST /api/v2/exposure-limits — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        payload: dict = {"chemicals": chemicals, "lang": LANG}
        if region:
            payload["region"] = region
        res = await client.post(
            f"{API_URL}/api/v2/exposure-limits",
            json=payload,
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_transport(chemicals: list[str], suppliers: dict[str, str] | None = None) -> dict:
    """POST /api/v2/transport-classification — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/transport-classification",
            json=_with_suppliers({"chemicals": chemicals, "lang": LANG},
                                  chemicals, suppliers),
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_waste(chemicals: list[str], suppliers: dict[str, str] | None = None) -> dict:
    """POST /api/v2/waste-disposal — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        res = await client.post(
            f"{API_URL}/api/v2/waste-disposal",
            json=_with_suppliers({"chemicals": chemicals, "lang": LANG},
                                  chemicals, suppliers),
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_sds_section(chemical: str, section: int, lang: str | None = None) -> dict:
    """POST /api/v2/sds-section — direct, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/sds-section",
            json={"chemical": chemical, "section": section,
                  "lang": _normalize_catalog_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_compare_sds(chemical: str, supplier: str = "", region: str = "") -> dict:
    """POST /api/v2/compare-sds-versions — direct service layer, no LLM."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/compare-sds-versions",
            json={"chemical": chemical, "supplier": supplier, "region": region},
            headers=_headers(),
        )
        return _billed_json(res)


async def _build_audit_session(experiment_name: str, chemicals: list[str]) -> dict:
    """Create a session, attach the chemicals, run the analysis. Returns the raw parts.

    Extracted so `get_audit_report()` (no session_id — CI-174) builds sessions the
    exact same way `create_audit_session` does. 🔴 Two implementations of a
    three-call sequence is how the two paths start producing different reports.
    """
    # 🔴 TIMEOUT_MULTI 而不是 TIMEOUT：第 3 步是 pairwise 兼容性分析（O(n²) 且带 LLM 兜底），
    # 本来就属于「多组分」预算。此前挂在 15s 上是 `create_audit_session` 一直带着的隐患，
    # 而 CI-174 的零参路径会自动喂进最多 12 个化学品，把它变成常态。
    async with httpx.AsyncClient(timeout=TIMEOUT_MULTI) as client:
        # 1. Create the session (bound to the API key owner)
        res = await client.post(
            f"{API_URL}/sessions",
            json={"experiment_name": experiment_name, "source": "mcp"},
            headers=_headers(),
        )
        _raise_for_status_with_reason(res)
        session_id = res.json()["session_id"]

        # 2. Persist chemicals as MsdsRecord (so the report PDF has data)
        res = await client.post(
            f"{API_URL}/sessions/{session_id}/chemicals",
            json={"chemicals": chemicals},
            headers=_headers(),
        )
        _raise_for_status_with_reason(res)
        chem_result = res.json()

        # 3. Run compatibility + risk analysis (reads CAS from MsdsRecord)
        res = await client.post(
            f"{API_URL}/sessions/{session_id}/compatibility",
            json={},
            headers={**_headers(), "Accept-Language": LANG},
        )
        _raise_for_status_with_reason(res)
        compat = res.json()

    return {"session_id": session_id, "chemicals": chem_result, "compatibility": compat}


async def _direct_alternatives(chemical: str, use_case: str = "", lang: str | None = None) -> dict:
    """POST /api/v2/chemical-alternatives — 确定性 curated 替代表，不走 LLM（CI-137）。"""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/chemical-alternatives",
            json={"chemical": chemical, "use_case": use_case or "general",
                  "lang": _normalize_catalog_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


def _format_alternatives(data: dict, chemical: str) -> str:
    """渲染确定性替代品结果（CI-137）。纯函数，便于逐分支测。

    🔴 三件必须原样出现在文本里，别让它们只留在 structuredContent：
      - `note`：curated 表的边界（「没有硬编码替代品」也是一种答案，不是空）
      - `risk_level`：替代建议的**前提**——不知道原物质多危险，"更安全"就没有意义
      - `source_info`：CI-65 的可追溯性红线（供应商 + 版本）
    """
    alts = data.get("alternatives") or []
    lines = [f"**Safer alternatives — {data.get('chemical') or chemical}**"]
    cas = data.get("cas_number")
    if cas:
        lines.append(f"**CAS:** {cas}")
    else:
        # 🔴 身份没定下来时**必须显著说出来**：curated 表的兜底分支会拿用户原串去撞 CAS 键
        # （`"50" in "50-00-0"` 为真），于是可能返回甲醛的替代品而 `cas_number` 是空的。
        # 不说这句，用户看到的是「一份针对某物质的替代清单」，而我们根本没认出那是什么。
        lines.append(
            "⚠️ **We could not identify this substance** (no CAS resolved). "
            "Anything below is generic hazard-reduction advice, **not** a substitution "
            "recommendation for a confirmed substance — confirm the identity first.")
    if data.get("risk_level"):
        lines.append(f"**Risk level of the original:** {data['risk_level']}")
    src = data.get("source_info") or {}
    if src.get("supplier"):
        lines.append(
            f"**Source:** {src.get('supplier')}"
            + (f" · revision {src['revision_date']}" if src.get("revision_date") else "")
        )
    # 🔴 CI-226 的两条披露：风险等级是**从某一份 SDS 推出来的**，而那份 SDS 可能是替代品
    # 或不同浓度的。丢掉它们，等于把一个有前提的判断说成无条件的。
    if src.get("substitution"):
        lines.append(f"> ⚠️ Risk level derived from a substituted SDS: {src['substitution']}")
    if src.get("concentration_mismatch"):
        lines.append(
            f"> ⚠️ Concentration mismatch vs the SDS used: {src['concentration_mismatch']}")
    lines.append("")
    if alts:
        for a in alts:
            name = a.get("name") or "Unknown"
            a_cas = f" (CAS {a['cas']})" if a.get("cas") else ""
            lines.append(f"- **{name}**{a_cas}")
            # 🔴 字段名照后端抄（`chemical_substitution.py` 的 `alternatives.append`）：
            # `rationale` / `trade_offs` / `risk_level`。初版按想当然写了 `reason`/`trade_off`
            # ——那正是 CI-529 刚栽过的「键名是我编的」，两次都发生在同一天。
            for key, label in (("rationale", "Why safer"),
                               ("risk_level", "Alternative risk"),
                               ("trade_offs", "Trade-offs")):
                if a.get(key):
                    lines.append(f"  - {label}: {a[key]}")
    else:
        # 后端在没有 curated 映射时会给一条通用建议（`name` 形如
        # "No hardcoded substitution available"），所以真正的空列表极少见——
        # 但空了就说空，别渲染成"有建议"。
        lines.append("No alternative could be produced for this chemical.")
    if data.get("note"):
        lines.append(f"\n> {data['note']}")
    return "\n".join(lines)


async def _direct_recent_chemicals(days: int = 7, limit: int = 12) -> dict:
    """GET /api/v2/recent-chemicals — what this caller analysed recently (CI-174)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.get(
            f"{API_URL}/api/v2/recent-chemicals",
            params={"days": days, "limit": limit},
            headers=_headers(),
        )
        return _billed_json(res)


async def _direct_sds_document(chemical: str) -> dict:
    """GET /api/v2/sds-document-url — return signed PDF URL or availability status."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.get(
            f"{API_URL}/api/v2/sds-document-url",
            params={"chemical": chemical},
            headers=_headers(),
        )
        return _billed_json(res)
