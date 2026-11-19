"""Card payloads for the safety tools whose per-chemical results carry a backend `view`
(PPE, regulatory check, storage, emergency, transport, waste, exposure limits, alternatives).

The `view` is built by the same backend builders the web app's cards use and is passed
through untouched: this module only checks its shape, adds the source line the result
already carries (`content_source` or `source_info` plus the signed document link in `documents`) and the
result's own form / preparation disclosures. A result without a usable `view` (older
backend, builder failure) gets no card; the text answer is unaffected.
"""
from __future__ import annotations

import ui_compat_card as card
from msds_mcp.disclosures import _form_disclosure_lines

_MAX_ITEMS = 12
_ANSWERING_SDS = "**Answering SDS:**"

# card kind -> list-valued keys the layout relies on (same checks as the web app's view guards)
_LISTS = {
    "ppe": ("regions", "other"),
    "compliance": ("rows",),
    "storage": ("requirements_sds", "requirements_class", "keep_away", "precautions"),
    "emergency": ("protocol", "steps", "hazard_actions", "precaution_actions"),
    "transport": ("modes",),
    "waste": ("categories", "sds_notes", "codes"),
    "exposure": ("types", "rows"),
    "substitution": ("alternatives",),
}


def _usable(kind: str, view) -> bool:
    if not isinstance(view, dict) or not isinstance(view.get("status"), str):
        return False
    if not all(isinstance(view.get(k), list) for k in _LISTS[kind]):
        return False
    if kind == "ppe":
        return all(isinstance(r, dict) and all(isinstance(r.get(k), list) for k in ("primary", "pinned", "also"))
                   for r in view["regions"])
    if kind == "compliance":
        return bool(view["rows"])
    return True


def _document_for(item: dict, documents: list[dict]) -> dict | None:
    """The document behind this item, or None. When both sides carry a CAS they must be equal
    (a shared name does not make two records the same substance); a name match is used only
    when a side has no CAS, and only if exactly one document matches."""
    cas = str(item.get("cas") or item.get("cas_number") or "").strip()
    if cas:
        same = [d for d in documents if str(d.get("cas") or "").strip() == cas]
        if same:
            return same[0]
    names = {str(k).casefold() for k in (item.get("chemical_name"), item.get("chemical")) if k}
    hits = [d for d in documents
            if not (cas and str(d.get("cas") or "").strip())
            and names & {str(k).casefold() for k in (d.get("chemical_name"), d.get("chemical")) if k}]
    return hits[0] if len(hits) == 1 else None


def _source(item: dict, documents: list[dict], allowed_origin: str) -> dict | None:
    """The source line: supplier, version, revision date, and the supplier name as the link to
    the original when that document is on the allowed origin and is the same supplier's."""
    cs = next((item[k] for k in ("content_source", "source_info") if isinstance(item.get(k), dict)), {})
    doc = _document_for(item, documents) or {}
    supplier = card._str(cs.get("supplier")) or card._str(doc.get("supplier"))
    same = not (cs.get("supplier") and doc.get("supplier")
                and str(cs["supplier"]).casefold() != str(doc["supplier"]).casefold())
    url = card._allowed_link(doc.get("sds_document_url"), allowed_origin) if same else None
    out = {"supplier": supplier, "url": url, "version": card._str(cs.get("msds_version")),
           "revision_date": card._str(cs.get("revision_date")) or (card._str(doc.get("revision_date")) if same else None)}
    return out if any(out.values()) else None


def safety_payload(kind: str, results, documents, allowed_origin: str) -> dict | None:
    """Payload for one card kind from the tool's per-chemical results, or None when no result
    has a usable `view`."""
    docs = card._dicts(documents)
    items = []
    for r in card._dicts(results):
        if not _usable(kind, r.get("view")):
            continue
        source = _source(r, docs, allowed_origin)
        notices = card.notice_lines(_form_disclosure_lines(r))
        if source:  # the answer's "Answering SDS" line repeats what the source line shows
            notices = [n for n in notices if not n.startswith(_ANSWERING_SDS)]
        items.append({"view": r["view"], "source": source, "notices": notices})
    if not items:
        return None
    return {"kind": kind, "items": items[:_MAX_ITEMS], "omitted": max(0, len(items) - _MAX_ITEMS)}
