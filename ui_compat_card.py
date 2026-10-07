"""Inline card for `check_chemical_compatibility` (MCP Apps / ChatGPT Apps SDK).

Why it exists: `content` and `structuredContent` are surfaced to the host's model, which may
rewrite, compress or drop them. The tool result's `_meta` is delivered only to this component
and is hidden from the model, so the verdicts, the regulated-precursor statements and the
source documents placed there reach the user verbatim when the host renders the card.

A host that does not render components ignores all of this; the text answer is unchanged.

Rendering is still the host's decision. This is an extra channel the model cannot edit, not a
guarantee that the user sees it.
"""
from __future__ import annotations

from urllib.parse import urlsplit

# A breaking change to the HTML must ship under a new URI (hosts cache by URI).
CARD_URI = "ui://msds-chain/compat-card-v1.html"
CARD_MIME = "text/html;profile=mcp-app"
# Signed SDS links (`sds_document_url`) are issued on the backend's public domain; it is the
# only redirect target the card opens.
SDS_LINK_ORIGIN = "https://api.msdschain.lagentbot.com"
# Key under the tool result's `_meta` that carries the card payload.
CARD_META_KEY = "msdschain/compat_card"

_MAX_PAIRS = 12
_MAX_PRECURSOR = 8
# The card is limited to two calls to action, so at most two source documents get a link.
_MAX_LINKED_DOCS = 2


def tool_meta() -> dict:
    """`_meta` for the tool definition: links the tool to the card template."""
    return {"ui": {"resourceUri": CARD_URI}, "openai/outputTemplate": CARD_URI}


def resource_meta(sds_link_origin: str) -> dict:
    """`_meta` for the template resource. The card fetches nothing; its only outbound action
    is opening a signed SDS link, which ChatGPT allowlists via `openai/widgetCSP`."""
    return {
        "ui": {"csp": {"connectDomains": [], "resourceDomains": []}},
        "openai/widgetCSP": {"connect_domains": [], "resource_domains": [],
                             "redirect_domains": [sds_link_origin]},
        "openai/widgetDescription": "Compatibility verdicts with the source SDS for each chemical.",
    }


def link_origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _dicts(value) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def card_payload(data: dict) -> dict:
    """Build the card payload from the tool's structuredContent (pairs already renamed to
    `chemical_a` / `chemical_b`), copying only fields the text answer already shows; nothing
    here is new information, it just cannot be rewritten.

    Malformed entries are skipped, never raised on: the card is an extra channel and must not
    take the text answer down with it."""
    pairs = []
    for p in _dicts(data.get("pairs"))[:_MAX_PAIRS]:
        pairs.append({
            "a": p.get("chemical_a"), "b": p.get("chemical_b"),
            "verdict": p.get("verdict") or p.get("level"),
            "citation": p.get("citation"),
        })
    precursor = [
        {"chemical": d.get("query_name") or d.get("matched_name"), "tier": d.get("tier"),
         "statement": d.get("statement")}
        for d in _dicts(data.get("precursor_disclosure"))[:_MAX_PRECURSOR]
        if d.get("statement")
    ]
    documents = []
    for d in _dicts(data.get("documents")):
        documents.append({
            "chemical": d.get("chemical_name") or d.get("chemical"),
            "supplier": d.get("supplier"),
            "revision_date": d.get("revision_date"),
            "url": d.get("sds_document_url") if len(
                [x for x in documents if x.get("url")]) < _MAX_LINKED_DOCS else None,
        })
    return {"pairs": pairs, "precursor": precursor, "documents": documents}


# Plain HTML + one inline script: no build step, no external assets, nothing fetched.
CARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--fg:#141413;--muted:#5e5d59;--line:#e6e4dd;--bad:#b42318;--warn:#b54708;--ok:#067647;--bg:transparent}
@media (prefers-color-scheme:dark){:root{--fg:#f5f4ef;--muted:#a8a69f;--line:#3a3935;--bad:#f97066;--warn:#fdb022;--ok:#47cd89}}
body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--fg);background:var(--bg)}
.card{padding:12px 4px}
h2{font-size:13px;font-weight:600;color:var(--muted);margin:10px 0 6px;text-transform:uppercase;letter-spacing:.04em}
.pair{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--line)}
.v{font-weight:600;white-space:nowrap}.incompatible{color:var(--bad)}.caution{color:var(--warn)}.compatible{color:var(--ok)}
.cite,.doc{color:var(--muted);font-size:12.5px}
.notice{border-left:3px solid var(--warn);padding:4px 0 4px 10px;margin:6px 0;font-size:13px}
button{font:inherit;font-size:12.5px;padding:3px 10px;border-radius:6px;border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer;margin-left:6px}
</style></head>
<body><div class="card" id="root"></div>
<script>
(function(){
  var KEY = "msdschain/compat_card";
  function esc(s){ return String(s == null ? "" : s).replace(/[&<>"]/g, function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c];}); }
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
  function render(p){
    if (!p) return;
    var h = "";
    if (p.pairs && p.pairs.length){
      h += "<h2>Compatibility</h2>";
      p.pairs.forEach(function(x){
        h += '<div class="pair"><div>' + esc(x.a) + " + " + esc(x.b)
          + (x.citation ? '<div class="cite">' + esc(x.citation) + "</div>" : "")
          + '</div><div class="v ' + esc(x.verdict) + '">' + esc(x.verdict) + "</div></div>";
      });
    }
    if (p.precursor && p.precursor.length){
      h += "<h2>Regulated precursor notice</h2>";
      p.precursor.forEach(function(x){
        h += '<div class="notice"><strong>' + esc(x.chemical) + (x.tier ? " (" + esc(x.tier) + ")" : "")
          + "</strong><br>" + esc(x.statement) + "</div>";
      });
    }
    if (p.documents && p.documents.length){
      h += "<h2>Source SDS</h2>";
      p.documents.forEach(function(d, i){
        h += '<div class="doc">' + esc(d.chemical) + ": " + esc(d.supplier || "unknown supplier")
          + (d.revision_date ? ", rev " + esc(d.revision_date) : "")
          + (d.url ? '<button data-i="' + i + '">Open SDS</button>' : "") + "</div>";
      });
    }
    var root = document.getElementById("root");
    root.innerHTML = h;
    root.querySelectorAll("button[data-i]").forEach(function(b){
      b.addEventListener("click", function(){
        var d = p.documents[+b.getAttribute("data-i")];
        if (window.openai && window.openai.openExternal) window.openai.openExternal({href: d.url});
        else window.open(d.url, "_blank", "noopener");
      });
    });
  }
  function fromGlobals(){ return window.openai ? findPayload(window.openai.toolResponseMetadata) : null; }
  render(fromGlobals());
  window.addEventListener("openai:set_globals", function(){ render(fromGlobals()); });
  window.addEventListener("message", function(e){
    var m = e.data; if (!m || m.method !== "ui/notifications/tool-result") return;
    render(findPayload(m.params));
  });
})();
</script></body></html>
"""
