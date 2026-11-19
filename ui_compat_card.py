"""Inline result cards (MCP Apps / ChatGPT Apps SDK) for the tools listed in `CARD_TOOLS`.

Why it exists: `content` and `structuredContent` are surfaced to the host's model, which may
rewrite, compress or drop them. The tool result's `_meta` is delivered only to this component
and is hidden from the model, so what is placed there (verdicts, regulated-precursor statements,
SDS section text, the source document, the upgrade link) reaches the user verbatim when the
host renders the card.

A host that does not render components ignores all of this; the text answer is unchanged.
Rendering is still the host's decision: ChatGPT renders the component only on results whose
`isError` is false, which is why an empty wallet on these tools is not an errored result
(`calls._credits_exhausted_result`).

The look follows the web app's result cards (card shell, SDS text block, source line, document
row); the colour roles come from the web app's tokens via `msds_mcp/ui_card_tokens.py`, which is
generated, not hand-copied.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from msds_mcp.ui_card_tokens import CARD_TOKENS_CSS

# Any change to the HTML must ship under a new URI: hosts cache the template by URI, so an
# edited template under the old one keeps serving the stale copy. A pinned hash in
# tests/contract/test_compat_card.py goes red when the template changes and the URI does not.
CARD_URI = "ui://msds-chain/result-card-v1.html"
CARD_MIME = "text/html;profile=mcp-app"
# Key under the tool result's `_meta` that carries the card payload.
CARD_META_KEY = "msdschain/compat_card"
# Tools whose definition points at the template. A payload on any other tool's result would
# reach no template, so it is left off.
CARD_TOOLS = frozenset({"check_chemical_compatibility", "get_sds_section", "get_sds_document"})

_MAX_PAIRS = 12
_MAX_PRECURSOR = 8
_MAX_DOCS = 12
# The compatibility card is limited to two calls to action, so at most two documents get a link.
_MAX_LINKED_DOCS = 2
# SDS section text beyond this is cut in the card (the full text is in the answer).
_MAX_SECTION_CHARS = 6000

# Where an upgrade link was shown, so visits to the pricing page can be attributed.
_SOURCE_PARAMS = {"utm_source": "mcp", "utm_campaign": "credits_exhausted"}


def tool_meta() -> dict:
    """`_meta` for the tool definition: links the tool to the card template."""
    return {"ui": {"resourceUri": CARD_URI}, "openai/outputTemplate": CARD_URI}


def resource_meta(sds_link_origin: str, upgrade_origin: str) -> dict:
    """`_meta` for the template resource. The card fetches nothing; its only outbound actions
    are opening a signed SDS link and, when the wallet is empty, the upgrade page. ChatGPT
    allowlists both via `openai/widgetCSP`."""
    return {
        "ui": {"csp": {"connectDomains": [], "resourceDomains": []}},
        "openai/widgetCSP": {"connect_domains": [], "resource_domains": [],
                             "redirect_domains": [sds_link_origin, upgrade_origin]},
        "openai/widgetDescription": "MSDS Chain result with the source SDS it is based on.",
    }


def link_origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _dicts(value) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _str(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _allowed_link(url, allowed_origin: str) -> str | None:
    """A link the card may open: exactly the origin (scheme, host, port) the template's CSP
    allowlists. Anything else (other schemes, other hosts, malformed) is dropped, not opened."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or link_origin(url) != allowed_origin:
        return None
    return url


def allowed_upgrade_link(url, upgrade_origin: str) -> str | None:
    """The upgrade link if it is on the one origin the card may open for it, else None."""
    return _allowed_link(url, upgrade_origin)


def with_source_params(url: str, medium: str, tool: str) -> str:
    """The upgrade link tagged with where it was shown (`medium`: card or text) and from which
    tool. Parameters already on the link are kept; ours are added only where absent."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for k, v in {**_SOURCE_PARAMS, "utm_medium": medium, "utm_content": tool}.items():
        query.setdefault(k, v)
    return urlunsplit(parts._replace(query=urlencode(query)))


def exhausted_payload(message: str, upgrade_url: str | None, resets_at: str | None,
                      upgrade_origin: str) -> dict:
    """Card payload for an empty wallet: the backend's sentence and an upgrade button. The
    model may drop the link from its reply; this channel does not pass through the model."""
    return {
        "kind": "credits_exhausted",
        "message": message if isinstance(message, str) else "",
        "upgrade_url": _allowed_link(upgrade_url, upgrade_origin),
        "resets_at": resets_at if isinstance(resets_at, str) else None,
    }


def card_payload(data: dict, allowed_origin: str) -> dict:
    """Compatibility card payload from the tool's structuredContent (pairs already renamed to
    `chemical_a` / `chemical_b`), copying only fields the text answer already shows; nothing
    here is new information, it just cannot be rewritten. `omitted` counts what the caps
    left out, so the card can say it is not the whole answer.

    Malformed entries are skipped, never raised on: the card is an extra channel and must not
    take the text answer down with it."""
    all_pairs = _dicts(data.get("pairs"))
    pairs = []
    for p in all_pairs[:_MAX_PAIRS]:
        verdict = p.get("verdict") or p.get("level")
        pairs.append({
            "a": p.get("chemical_a"), "b": p.get("chemical_b"),
            "verdict": verdict.lower() if isinstance(verdict, str) else verdict,
            # The text answer prints this pair's `source` as its basis line.
            "source": p.get("source"),
        })
    all_precursor = [d for d in _dicts(data.get("precursor_disclosure")) if d.get("statement")]
    precursor = [
        {"chemical": d.get("query_name") or d.get("matched_name"), "tier": d.get("tier"),
         "statement": d.get("statement")}
        for d in all_precursor[:_MAX_PRECURSOR]
    ]
    all_docs = _dicts(data.get("documents"))
    documents = []
    linked = 0
    for d in all_docs[:_MAX_DOCS]:
        url = _allowed_link(d.get("sds_document_url"), allowed_origin) \
            if linked < _MAX_LINKED_DOCS else None
        linked += bool(url)
        documents.append({
            "chemical": d.get("chemical_name") or d.get("chemical"),
            "supplier": d.get("supplier"),
            "revision_date": d.get("revision_date"),
            "url": url,
        })
    return {
        "pairs": pairs, "precursor": precursor, "documents": documents,
        "omitted": {"pairs": len(all_pairs) - len(pairs),
                    "precursor": len(all_precursor) - len(precursor),
                    "documents": len(all_docs) - len(documents)},
    }


def _notice_lines(lines: list[str]) -> list[str]:
    """The text answer's disclosure bullets (`- ⚠️ …`) as plain sentences for the card."""
    out = []
    for line in lines:
        text = line.strip().removeprefix("-").strip().removeprefix("⚠️").strip()
        if text:
            out.append(text)
    return out


def sds_section_payload(data: dict, section: int, title: str, notices: list[str]) -> dict:
    """`get_sds_section` card: the section text with the source it came from. Every field is
    one the text answer prints; `notices` are the answer's own disclosure lines."""
    content = _str(data.get("content"))
    unresolved = bool(data.get("unresolved"))
    differs = bool(content and data.get("section_source_differs"))
    csi = data.get("corpus_source_info") if isinstance(data.get("corpus_source_info"), dict) else {}
    text_source = None
    if content and not unresolved and csi:
        who = " · ".join(str(v) for v in (csi.get("supplier"), csi.get("source")) if v)
        if who:
            text_source = {"who": who, "revision_date": _str(csi.get("revision_date"))}
    source = None
    if not unresolved and _str(data.get("supplier")):
        source = {"supplier": data["supplier"], "region": _str(data.get("region")),
                  "revision_date": _str(data.get("revision_date")),
                  "cited_only": differs}
    note = None
    if not content:
        note = _str(data.get("no_section_text_note"))
        if unresolved and data.get("no_section_text_reason") != "unresolved":
            note = None
    text = content[:_MAX_SECTION_CHARS] if content and not unresolved else None
    return {
        "kind": "sds_section",
        "chemical": _str(data.get("chemical")), "cas": _str(data.get("cas")),
        "section": section, "title": title,
        "text": text, "truncated": bool(text and len(content) > len(text)),
        "source_differs_note": _str(data.get("section_source_note")) if differs else None,
        "note": note, "unresolved": unresolved,
        "source": source, "text_source": text_source,
        "notices": notices,
    }


def sds_document_payload(structured: dict, notices: list[str], allowed_origin: str) -> dict:
    """`get_sds_document` card: the original document as one row that opens it, with the
    price of opening it when there is one. Every field is one the text answer prints."""
    cost = structured.get("open_cost_credits")
    return {
        "kind": "sds_document",
        "available": bool(structured.get("available")),
        "chemical": _str(structured.get("chemical_name")), "cas": _str(structured.get("cas")),
        "supplier": _str(structured.get("supplier")),
        "region": _str(structured.get("region")),
        "revision_date": _str(structured.get("revision_date")),
        "reference_sheet": structured.get("document_kind") == "reference_sheet",
        "product": structured.get("record_kind") == "product",
        "url": _allowed_link(structured.get("pdf_url"), allowed_origin),
        "open_cost_credits": cost if isinstance(cost, (int, float)) and cost > 0 else None,
        "message": _str(structured.get("message")),
        "notices": notices,
    }


# Plain HTML + one inline script: no build step, no external assets, nothing fetched.
# `__ALLOWED_ORIGIN__` / `__UPGRADE_ORIGIN__` / `__TOKENS__` are replaced by `card_html()`:
# SDS links may open only on the first origin, the upgrade button only on the second.
# Class names mirror the web app's result-card parts so the two can be compared side by side:
# `.shell` CardShell, `.head` CardHead, `.chip` ToneChip, `.sds` SdsTextBlock, `.src`
# SourceRefLine, `.strip` AmberStrip, `.doc` SdsDocumentCard row.
_CARD_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<style>
__TOKENS__
:root{color-scheme:dark}
@media (prefers-color-scheme:light){:root:not([data-theme=dark]){color-scheme:light}}
:root[data-theme=light]{color-scheme:light}:root[data-theme=dark]{color-scheme:dark}
body{margin:0;background:transparent;font:13px/18px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Hiragino Sans","Noto Sans CJK SC",sans-serif;color:rgb(var(--s-label-2))}
.shell{border:1px solid rgb(var(--s-line));border-radius:12px;background:rgb(var(--s-surface));padding:12px 16px;min-width:0;overflow-wrap:anywhere}
.head{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:10px}
.name{font-size:15px;line-height:1.47;font-weight:600;color:rgb(var(--s-label))}
.cas{margin-left:6px;font-size:12px;font-weight:400;color:rgb(var(--s-label-3));font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.sub,.cap{font-size:12px;line-height:16px;color:rgb(var(--s-label-3))}
.chip{display:inline-flex;align-items:center;padding:0 8px;border-radius:6px;font-size:12px;line-height:20px;font-weight:600;white-space:nowrap}
.t-bad{background:rgb(var(--c-bad)/.10);color:rgb(var(--s-bad-ink))}.t-warn{background:rgb(var(--c-warn)/.10);color:rgb(var(--s-warn-ink))}
.t-info{background:rgb(var(--c-info)/.10);color:rgb(var(--s-info-ink))}.t-pending{background:rgb(var(--c-pending)/.10);color:rgb(var(--s-pending-ink))}
.sec{display:flex;align-items:center;gap:8px;margin:14px 0 6px;font-size:12px;line-height:16px;color:rgb(var(--s-label-3))}
.sec::after{content:"";flex:1;height:1px;background:rgb(var(--s-line))}
.pair{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:6px 0;border-bottom:1px solid rgb(var(--s-line))}
.pair:last-child{border-bottom:0}.pair .who{color:rgb(var(--s-label))}
.strip{margin-top:8px;border-radius:8px;background:rgb(var(--c-warn)/.10);color:rgb(var(--s-warn-ink));padding:6px 10px;font-size:12px;line-height:16px}
.strip+.strip{margin-top:6px}.strip b{font-weight:600}
.sds{margin-top:12px;border-radius:10px;background:rgb(var(--s-inset));padding:10px 12px}
.sds .tag{display:inline-flex;align-items:center;gap:6px;font-size:12px;line-height:16px;color:rgb(var(--s-label-2));font-weight:500}
.sds .tag i{font-style:normal;padding:0 6px;border-radius:4px;background:rgb(var(--c-info)/.10);color:rgb(var(--s-info-ink));font-weight:600;font-size:10px;line-height:16px}
.sds .body{margin-top:6px;white-space:pre-wrap;color:rgb(var(--s-label));line-height:1.6}
.cut{display:inline-flex;margin-top:6px;padding:0 6px;border-radius:4px;background:rgb(var(--c-warn)/.10);color:rgb(var(--s-warn-ink));font-size:12px;font-weight:500}
.src{margin-top:12px;padding-top:10px;border-top:1px solid rgb(var(--s-line));color:rgb(var(--s-label-3));display:flex;flex-wrap:wrap;gap:2px 8px;align-items:baseline}
.src+.src{margin-top:4px;padding-top:0;border-top:0}.src .sup{color:rgb(var(--s-brand-ink))}
.doc{display:flex;align-items:center;gap:12px;width:100%;box-sizing:border-box;text-align:left;font:inherit;border:1px solid rgb(var(--s-line));border-radius:12px;background:rgb(var(--s-surface));padding:12px;color:inherit}
button.doc{cursor:pointer}button.doc:hover{background:rgb(var(--s-field))}
.doc .ic{flex:none;width:40px;height:40px;border-radius:8px;background:rgb(var(--s-brand-500)/.10);color:rgb(var(--s-brand-ink));display:flex;align-items:center;justify-content:center}
.doc .tx{flex:1;min-width:0}.doc .tt{font-size:15px;line-height:1.47;font-weight:500;color:rgb(var(--s-label))}
.doc .mt{color:rgb(var(--s-label-3))}
.go{display:inline-flex;align-items:center;margin-top:10px;font:inherit;font-weight:600;border:0;border-radius:8px;padding:6px 14px;background:rgb(var(--s-brand-500));color:#fff;cursor:pointer}
.lnk{font:inherit;font-size:12px;border:1px solid rgb(var(--s-line));border-radius:6px;background:transparent;color:rgb(var(--s-brand-ink));padding:0 8px;line-height:20px;cursor:pointer}
.more{margin-top:6px;font-size:12px;color:rgb(var(--s-label-3))}
</style></head>
<body><div id="root"></div>
<script>
(function(){
  var KEY = "msdschain/compat_card";
  var ALLOWED_ORIGIN = __ALLOWED_ORIGIN__;
  var UPGRADE_ORIGIN = __UPGRADE_ORIGIN__;
  // Same table as the web app's compatTone: green never appears in a compatibility verdict.
  var TONE = {incompatible: "bad", caution: "warn", no_known_incompatibility: "info",
              compatible: "info", unverified: "pending", pending_upload: "pending"};
  var FILE = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8M16 17H8M10 9H8"/></svg>';
  var inHost = window.parent && window.parent !== window;
  var nextId = 1, pending = {}, actions = [];
  function esc(s){ return String(s == null ? "" : s).replace(/[&<>"]/g, function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c];}); }
  function rpc(method, params, onResult){
    if (!inHost) return;
    var id = nextId++; if (onResult) pending[id] = onResult;
    window.parent.postMessage({jsonrpc: "2.0", id: id, method: method, params: params || {}}, "*");
  }
  function notify(method, params){
    if (inHost) window.parent.postMessage({jsonrpc: "2.0", method: method, params: params || {}}, "*");
  }
  function safeUrl(u, origin){
    if (!origin) return null;
    try { var x = new URL(u); return (/^https?:$/.test(x.protocol) && x.origin === origin) ? x.href : null; }
    catch (e) { return null; }
  }
  function openUrl(url){
    if (window.openai && window.openai.openExternal) window.openai.openExternal({href: url});
    else if (inHost) rpc("ui/open-link", {url: url});
    else window.open(url, "_blank", "noopener");
  }
  // Buttons are wired by index after render; the URL is re-checked at click time.
  function action(url, origin){ actions.push({url: url, origin: origin}); return actions.length - 1; }
  function strips(list){ return (list || []).map(function(t){ return '<div class="strip">' + esc(t) + "</div>"; }).join(""); }
  function head(name, cas, sub, chips){
    return '<div class="head"><div style="min-width:0"><div class="name">' + esc(name || cas || "")
      + (cas && cas !== name ? '<span class="cas">CAS ' + esc(cas) + "</span>" : "") + "</div>"
      + (sub ? '<div class="sub">' + esc(sub) + "</div>" : "") + "</div>"
      + (chips ? "<div>" + chips + "</div>" : "") + "</div>";
  }
  function more(n, what){ return n > 0 ? '<div class="more">+ ' + esc(n) + " more " + what + " not shown here; see the full answer.</div>" : ""; }
  function renderExhausted(p){
    var up = safeUrl(p.upgrade_url, UPGRADE_ORIGIN);
    var h = '<div class="shell">' + head("Out of credits", null, "This request was not run, so there is no MSDS Chain result for it.", "")
      + (p.message ? '<div class="strip">' + esc(p.message) + "</div>" : "")
      + (up ? '<button class="go" data-a="' + action(up, UPGRADE_ORIGIN) + '">Upgrade</button>' : "")
      + "</div>";
    return h;
  }
  function renderCompat(p){
    var om = p.omitted || {}, h = "";
    if (p.pairs && p.pairs.length){
      h += '<div class="sec">Compatibility</div>';
      p.pairs.forEach(function(x){
        var tone = TONE[x.verdict] || "pending";
        h += '<div class="pair"><div><div class="who">' + esc(x.a) + " + " + esc(x.b) + "</div>"
          + (x.source ? '<div class="cap">Basis: ' + esc(x.source) + "</div>" : "")
          + '</div><span class="chip t-' + tone + '">' + esc(String(x.verdict || "").replace(/_/g, " ")) + "</span></div>";
      });
      h += more(om.pairs, "pairs");
    }
    if (p.precursor && p.precursor.length){
      h += '<div class="sec">Regulated precursor notice</div>';
      p.precursor.forEach(function(x){
        h += '<div class="strip"><b>' + esc(x.chemical) + (x.tier ? " (" + esc(x.tier) + ")" : "") + "</b> · " + esc(x.statement) + "</div>";
      });
      h += more(om.precursor, "notices");
    }
    if (p.documents && p.documents.length){
      h += '<div class="sec">Source SDS</div>';
      p.documents.forEach(function(d){
        var u = safeUrl(d.url, ALLOWED_ORIGIN);
        h += '<div class="src" style="margin-top:4px;padding-top:0;border-top:0"><span>' + esc(d.chemical) + '</span><span class="sup">' + esc(d.supplier || "unknown supplier") + "</span>"
          + (d.revision_date ? "<span>Revision date: " + esc(d.revision_date) + "</span>" : "")
          + (u ? '<button class="lnk" data-a="' + action(u, ALLOWED_ORIGIN) + '">Open SDS</button>' : "") + "</div>";
      });
      h += more(om.documents, "documents");
    }
    return h ? '<div class="shell">' + h + "</div>" : "";
  }
  function srcLine(label, s){
    return '<div class="src"><span>' + esc(label) + '</span><span class="sup">' + esc(s.supplier || s.who) + (s.region ? " · " + esc(s.region) : "") + "</span>"
      + (s.revision_date ? "<span>Revision date: " + esc(s.revision_date) + "</span>" : "") + "</div>";
  }
  function renderSection(p){
    var title = "Section " + p.section + ": " + p.title;
    var h = '<div class="shell">' + head(p.chemical, p.cas, "SDS " + title, "");
    if (p.source_differs_note) h += '<div class="strip">' + esc(p.source_differs_note) + "</div>";
    h += strips(p.notices);
    if (p.text){
      h += '<div class="sds"><span class="tag"><i>SDS</i>' + esc(title) + '</span><div class="body">' + esc(p.text) + "</div>"
        + (p.truncated ? '<span class="cut">Shortened here; the full section is in the answer.</span>' : "") + "</div>";
    } else if (p.note){
      h += '<div class="strip">' + esc(p.note) + "</div>";
    }
    if (p.source) h += srcLine(p.source.cited_only ? "Cited record (not the source of the text above)" : "Source", p.source);
    if (p.text_source) h += srcLine("Text source", p.text_source);
    return h + "</div>";
  }
  function renderDocument(p){
    if (!p.available){
      return '<div class="shell">' + head(p.chemical, p.cas, null, "") + (p.message ? '<div class="cap" style="margin-top:4px">' + esc(p.message) + "</div>" : "") + "</div>";
    }
    var u = safeUrl(p.url, ALLOWED_ORIGIN);
    var kind = p.reference_sheet ? "Reference document (not a supplier SDS)" : "Supplier SDS (original PDF)";
    var meta = [p.supplier, p.region, p.revision_date ? "Revision date: " + p.revision_date : null].filter(Boolean).join(" · ");
    var tx = '<span class="ic">' + FILE + '</span><span class="tx"><div class="tt">' + esc(kind) + ": " + esc(p.chemical || p.cas) + "</div>"
      + '<div class="mt">' + esc(meta) + "</div>"
      + (p.open_cost_credits ? '<div class="mt">Opening it costs ' + esc(p.open_cost_credits) + (p.open_cost_credits === 1 ? " credit" : " credits") + "; opening it again the same day (UTC) is free.</div>" : "")
      + "</span>";
    var h = u ? '<button class="doc" data-a="' + action(u, ALLOWED_ORIGIN) + '">' + tx + "</button>"
              : '<div class="doc">' + tx + "</div>";
    var notes = (p.product ? ["Formulated product: its classification applies to the mixture as a whole, not to any single ingredient."] : []).concat(p.notices || []);
    return h + strips(notes);
  }
  function findPayload(meta){
    if (!meta) return null;
    var cands = [meta, meta.call_tool_result, meta.mcp_tool_result];
    for (var i = 0; i < cands.length; i++){
      var c = cands[i]; if (!c) continue;
      var m = c._meta || c.meta || c;
      if (m && m[KEY]) return m[KEY];
    }
    return null;
  }
  // The host's theme wins over the OS preference: an iframe whose colour scheme differs from
  // the page around it gets an opaque canvas, so light-on-light text would be unreadable.
  function applyTheme(t){
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  }
  function reportSize(){
    var r = document.getElementById("root").getBoundingClientRect();
    var h = Math.ceil(r.bottom);
    notify("ui/notifications/size-changed", {width: Math.ceil(r.right), height: h});
    if (window.openai && typeof window.openai.notifyIntrinsicHeight === "function") window.openai.notifyIntrinsicHeight(h);
  }
  function render(p){
    if (!p) return;
    actions = [];
    var html = p.kind === "credits_exhausted" ? renderExhausted(p)
      : p.kind === "sds_section" ? renderSection(p)
      : p.kind === "sds_document" ? renderDocument(p)
      : renderCompat(p);
    var root = document.getElementById("root");
    root.innerHTML = html;
    reportSize();
    root.querySelectorAll("[data-a]").forEach(function(b){
      b.addEventListener("click", function(){
        var a = actions[+b.getAttribute("data-a")];
        var url = a && safeUrl(a.url, a.origin);
        if (url) openUrl(url);
      });
    });
  }
  function fromGlobals(){
    if (!window.openai) return null;
    applyTheme(window.openai.theme);
    return findPayload(window.openai.toolResponseMetadata);
  }
  render(fromGlobals());
  window.addEventListener("openai:set_globals", function(){ render(fromGlobals()); });
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.getElementById("root"));
  window.addEventListener("message", function(e){
    if (e.source !== window.parent) return;
    var m = e.data; if (!m || m.jsonrpc !== "2.0") return;
    if (m.id != null && pending[m.id]){ var f = pending[m.id]; delete pending[m.id]; f(m); return; }
    if (m.method === "ui/notifications/tool-result") render(findPayload(m.params));
    if (m.method === "ui/notifications/host-context-changed") applyTheme((m.params || {}).theme);
  });
  // MCP Apps: the host sends tool-result only after the view has initialized.
  rpc("ui/initialize", {appInfo: {name: "msds-chain-result-card", version: "1"}, appCapabilities: {}},
      function(m){
        applyTheme(((m.result || {}).hostContext || {}).theme);
        notify("ui/notifications/initialized");
      });
})();
</script></body></html>
"""


def card_html(allowed_origin: str, upgrade_origin: str) -> str:
    return (_CARD_HTML_TEMPLATE.replace("__TOKENS__", CARD_TOKENS_CSS)
            .replace("__ALLOWED_ORIGIN__", json.dumps(allowed_origin))
            .replace("__UPGRADE_ORIGIN__", json.dumps(upgrade_origin)))
