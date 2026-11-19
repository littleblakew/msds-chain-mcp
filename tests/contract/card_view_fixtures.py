"""Hand-written `view` fixtures in the shape the backend's card-view builders produce, for the
safety-tool cards (tests and the local preview page). Public substances only."""
import copy

ORIGIN = "https://api.msdschain.lagentbot.com"


def _it(text, kind="hazard_code", codes=(), rung=None):
    return {"text": text, "kind": kind, "codes": list(codes), "rung": rung}


def _region(key, label, primary=(), pinned=(), also=()):
    return {"key": key, "label": label, "pinned": list(pinned), "primary": list(primary), "also": list(also),
            "total": len(primary) + len(pinned) + len(also),
            "co_primary": {"1": [2]} if key == "gloves" else {}}


def ppe_view(name, cas, level, strong=True):
    g = ("Heavy-duty chemical-resistant gloves (butyl rubber or neoprene)", 1) if strong else ("Chemical-resistant gloves (nitrile)", 4)
    e = ("Full face shield + safety goggles", 1) if strong else ("Safety goggles", 3)
    return {
        "chemical": name, "chemical_detail": None, "cas": cas, "signal_word": "Danger",
        "level": level, "level_label": level.title(), "summary": "Corrosive: protect skin, eyes and airways.",
        "regions": [
            _region("gloves", "Hands", [_it(g[0], codes=["H314"], rung=g[1])],
                    also=[_it("Disposable nitrile gloves", codes=["H315"], rung=6)]),
            _region("eye", "Eyes & face", [_it(e[0], codes=["H314"], rung=e[1])]),
            _region("respiratory", "Respiratory", [], pinned=[
                {"text": "Use only in a fume hood. Avoid breathing mist.", "kind": "sds", "codes": [], "rung": None}]),
            _region("body", "Body", [_it("Chemical-resistant apron + lab coat", codes=["H314"], rung=2)]),
        ],
        "other": [{"text": "Wear protective gloves/protective clothing/eye protection/face protection",
                   "kind": "precaution", "codes": ["P280"], "rung": 91}],
        "other_label": "Other precautions",
        "conflict": None, "notes": ["Section 8 of the SDS lists no respirator type."],
        "status": "sds", "status_label": "From SDS",
    }


PPE_ACID = {"chemical_name": "sulfuric acid", "cas": "7664-93-9",
            "content_source": {"source": "canonical", "supplier": "Supplier X", "revision_date": "2024-05-01", "msds_version": "7.0"},
            "view": ppe_view("Sulfuric acid", "7664-93-9", "maximum")}
PPE_ACETONE = {"chemical_name": "acetone", "cas": "67-64-1",
               "content_source": {"source": "canonical", "supplier": "Supplier Y", "revision_date": "2023-02-10"},
               "physical_form_disclosure": "This SDS describes the liquid form.",
               "view": ppe_view("Acetone", "67-64-1", "enhanced", strong=False)}
PPE_DOCS = [
    {"chemical": "sulfuric acid", "chemical_name": "sulfuric acid", "cas": "7664-93-9", "supplier": "Supplier X",
     "revision_date": "2024-05-01", "sds_document_url": ORIGIN + "/msds/token/acid"},
    {"chemical": "acetone", "chemical_name": "acetone", "cas": "67-64-1", "supplier": "Supplier Y",
     "revision_date": "2023-02-10", "sds_document_url": "https://evil.example/msds/token/x"},
]

COMPLIANCE = {"chemical": "benzene", "cas": "71-43-2", "view": {
    "chemical": "Benzene", "chemical_detail": None, "cas": "71-43-2", "conflict": None,
    "subtitle": "Regulatory check", "details_label": "Details", "inventory_label": "Chemical inventory",
    "rows": [
        {"region": "EU", "status": "restricted", "status_label": "On a restriction list",
         "flags": ["REACH Annex XVII (Entry 5)"],
         "inventory": {"lists": ["REACH registered"], "status": "on", "status_label": "Listed", "note": None, "lookup": None},
         "details": ["Entry 5 restricts specific uses and concentrations."]},
        {"region": "TW", "status": "not_restricted", "status_label": "Not found in the lists we hold", "flags": [],
         "inventory": {"lists": [], "status": "none", "status_label": "No inventory held for this region", "note": None,
                       "lookup": {"name": "Official inventory query", "url": "https://example.org/csnn"}},
         "details": []},
    ],
    "notes": ["\"Not found\" only means it is not in our copy of these lists."],
    "status": "lists", "status_label": "Checked against lists we hold"}}


def _head(name, cas, **kw):
    return {"chemical": name, "chemical_detail": None, "cas": cas, "conflict": None, **kw}


def _line(t, prohibit=False):
    return {"text": t, "prohibit": prohibit}


STORAGE = {"chemical_name": "acetone", "cas": "67-64-1",
           "content_source": {"source": "canonical", "supplier": "Supplier Y", "revision_date": "2023-02-10"},
           "view": {**_head("Acetone", "67-64-1", notes=None), "storage_class": "flammable", "class_label": "Flammable liquids",
                    "cabinet": "Flammable storage cabinet", "cabinet_color": "Red", "temperature": "Store below 30 C",
                    "requirements_sds": [_line("Keep container tightly closed."), _line("Do not store near ignition sources.", True)],
                    "requirements_class": [_line("Ground and bond containers.")], "requirements_general": [],
                    "keep_away": ["Oxidizers", "Strong acids"], "special": [],
                    "precautions": [{"text": "Store in a well-ventilated place.", "kind": "precaution", "codes": ["P403+P235"], "prohibit": False}],
                    "nfpa": {"health": 1, "fire": 3, "reactivity": 0},
                    "peroxide": None, "peroxide_note": "Not on the peroxide-former list we hold; that is not a statement that none forms.",
                    "status": "mixed", "status_label": "SDS + derived"}}

EMERGENCY = {"chemical": "Hydrogen fluoride", "cas": "7664-39-3",
             "content_source": {"source": "canonical", "supplier": "Supplier X", "revision_date": "2024-05-01"},
             "view": {**_head("Hydrogen fluoride", "7664-39-3", notes=None), "signal_word": "Danger", "scenario": "exposure",
                      "protocol": [{"text": "Apply calcium gluconate gel to the burn immediately and keep massaging it in."}],
                      "protocol_citation": "Substance protocol; durations vary by source, follow your site procedure.",
                      "steps": [_line("Remove contaminated clothing."), _line("Do not delay flushing for first aid supplies.", True)],
                      "sds": None, "sds_paragraphs": [
                          {"page": False, "label": "If inhaled", "heading": None, "text": "Move to fresh air and call a doctor."},
                          {"page": False, "label": None, "heading": "4.2 Most important symptoms", "text": ""},
                          {"page": True, "label": None, "heading": None, "text": "3 / 24"}],
                      "sds_truncated": True,
                      "hazard_actions": [{"text": "Do NOT use a water jet on the container.", "kind": "hazard_code", "codes": ["H330"], "prohibit": True},
                                         {"text": "Move the person to fresh air.", "kind": "hazard_code", "codes": ["H330"], "prohibit": False}],
                      "precaution_actions": [{"text": "Rinse skin with water.", "kind": "precaution", "codes": ["P303+P361+P353"], "prohibit": False}],
                      "status": "mixed", "status_label": "SDS + derived"}}

TRANSPORT = {"chemical_name": "acetone", "cas": "67-64-1", "content_source": {"source": "canonical", "supplier": "Supplier Y", "revision_date": "2023-02-10"},
             "view": {**_head("Acetone", "67-64-1", notes=["Curated entry."]), "un_number": "UN1090", "un_candidates": None,
                      "ambiguity_reason": None, "shipping_name": "ACETONE", "hazard_class": "3", "subsidiary_risk": None,
                      "packing_group": "II", "modes": [{"key": "road", "text": "ADR 3 / II"}, {"key": "air", "text": "IATA 3 / II"}],
                      "evidence": {"quote": "14.1 UN number: 1090", "truncated": False},
                      "status": "curated", "status_label": "Curated table, not this SDS"}}

WASTE = {"chemical_name": "acetone", "cas": "67-64-1", "content_source": {"source": "canonical", "supplier": "Supplier Y", "revision_date": "2023-02-10"},
         "view": {**_head("Acetone", "67-64-1", notes=None), "codes": ["H225", "H319"],
                  "categories": [{"id": "nonhalogenated", "label": "Non-halogenated solvent waste", "fallback": False,
                                  "method": [_line("Collect in a labelled solvent container;"), _line(" do not pour down the drain.", True)],
                                  "container": [_line("Use a closed, grounded container.")], "regulatory": None}],
                  "sds_paragraphs": [{"page": False, "label": None, "heading": None, "text": "Dispose of contents in accordance with local regulations."}],
                  "sds_truncated": False, "sds_notes": ["Do not mix with oxidizing waste."],
                  "status": "mixed", "status_label": "SDS + derived"}}

EXPOSURE = {"chemical_name": "acetone", "cas": "67-64-1", "content_source": {"source": "canonical", "supplier": "Supplier Y", "revision_date": "2023-02-10"},
            "view": {**_head("Acetone", "67-64-1", notes=None), "region_filter": None, "types": ["TWA", "STEL"],
                     "rows": [
                         {"source": "OSHA PEL", "region": "US", "entry": None, "origin": "regulatory", "notation": None,
                          "url": "https://example.org/osha", "version": "29 CFR 1910.1000", "cells": {"TWA": [{"value": 1000, "unit": "ppm", "strictest": False}]}},
                         {"source": "ACGIH TLV", "region": "INT", "entry": None, "origin": "curated", "notation": None, "url": None, "version": None,
                          "cells": {"TWA": [{"value": 250, "unit": "ppm", "strictest": True}], "STEL": [{"value": 500, "unit": "ppm", "strictest": True}]}}],
                     "excerpt": None, "status": "tables", "status_label": "Regulatory + curated tables"}}

SUBSTITUTION = {"chemical": "Benzene", "cas": "71-43-2",
                "view": {**_head("Benzene", "71-43-2", notes=None), "risk_level": "High: carcinogen (H350)", "risk_tone": "bad",
                         "use_case": None, "alternatives": [
                             {"name": "Ethyl acetate", "cas": "141-78-6", "risk_level": "Moderate", "rationale": "Not classified as a carcinogen.", "trade_offs": None},
                             {"name": "Ethanol", "cas": "64-17-5", "risk_level": None, "rationale": "Lower toxicity.", "trade_offs": None}],
                         "shared_trade_off": "Check solvency for your application.", "generic": False,
                         "disclosure": "Pairings and reasons come from a curated list, not from either substance's SDS data.",
                         "status": "pairing", "status_label": "Curated pairing"}}

ALL_KINDS = {
    "ppe": [PPE_ACID, PPE_ACETONE], "compliance": [COMPLIANCE], "storage": [STORAGE], "emergency": [EMERGENCY],
    "transport": [TRANSPORT], "waste": [WASTE], "exposure": [EXPOSURE], "substitution": [SUBSTITUTION],
}


def without_view(result):
    r = copy.deepcopy(result)
    r.pop("view", None)
    return r
