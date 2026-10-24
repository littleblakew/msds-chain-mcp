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

import json
from urllib.parse import urlsplit

# Any change to the HTML must ship under a new URI: hosts cache the template by URI, so an
# edited template under the old one keeps serving the stale copy. A pinned hash in
# tests/contract/test_compat_card.py goes red when the template changes and the URI does not.
CARD_URI = "ui://msds-chain/compat-card-v3.html"
CARD_MIME = "text/html;profile=mcp-app"
# Signed SDS links (`sds_document_url`) are issued on the backend's public origin, which the
# caller derives from its API base URL; it is the only target the card opens.
# Key under the tool result's `_meta` that carries the card payload.
CARD_META_KEY = "msdschain/compat_card"

_MAX_PAIRS = 12
_MAX_PRECURSOR = 8
_MAX_DOCS = 12
# The card is limited to two calls to action, so at most two source documents get a link.
_MAX_LINKED_DOCS = 2


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
        "openai/widgetDescription": "Compatibility verdicts with the source SDS for each chemical.",
    }


def link_origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _dicts(value) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


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
    """Build the card payload from the tool's structuredContent (pairs already renamed to
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


# Plain HTML + one inline script: no build step, no external assets, nothing fetched.
# `__ALLOWED_ORIGIN__` / `__UPGRADE_ORIGIN__` are replaced by `card_html()`: SDS links may open
# only on the first, the upgrade button only on the second.
_CARD_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<style>
:root{color-scheme:light dark;--fg:#141413;--muted:#5e5d59;--line:#e6e4dd;--bad:#b42318;--warn:#b54708;--ok:#067647;--bg:transparent}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--fg:#f5f4ef;--muted:#a8a69f;--line:#3a3935;--bad:#f97066;--warn:#fdb022;--ok:#47cd89}}
:root[data-theme=dark]{color-scheme:dark;--fg:#f5f4ef;--muted:#a8a69f;--line:#3a3935;--bad:#f97066;--warn:#fdb022;--ok:#47cd89}
:root[data-theme=light]{color-scheme:light}
body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--fg);background:var(--bg)}
.card{padding:12px 4px}
h2{font-size:13px;font-weight:600;color:var(--muted);margin:10px 0 6px;text-transform:uppercase;letter-spacing:.04em}
.pair{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--line)}
.v{font-weight:600;white-space:nowrap}.incompatible{color:var(--bad)}.caution{color:var(--warn)}.compatible{color:var(--ok)}
.cite,.doc,.more{color:var(--muted);font-size:12.5px}
.notice{border-left:3px solid var(--warn);padding:4px 0 4px 10px;margin:6px 0;font-size:13px}
button{font:inherit;font-size:12.5px;padding:3px 10px;border-radius:6px;border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer;margin-left:6px}
</style></head>
<body><div class="card" id="root"></div>
<script>
(function(){
  var KEY = "msdschain/compat_card";
  var ALLOWED_ORIGIN = __ALLOWED_ORIGIN__;
  var UPGRADE_ORIGIN = __UPGRADE_ORIGIN__;
  var inHost = window.parent && window.parent !== window;
  var nextId = 1, pending = {};
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
  function renderExhausted(p){
    var up = safeUrl(p.upgrade_url, UPGRADE_ORIGIN);
    var root = document.getElementById("root");
    root.innerHTML = "<h2>Out of credits</h2>"
      + (p.message ? '<div class="notice">' + esc(p.message) + "</div>" : "")
      + '<div class="doc">This check was not run, so there is no MSDS Chain result for it.'
      + (up ? '<button data-up="1">Upgrade</button>' : "") + "</div>";
    reportSize();
    var b = root.querySelector("button[data-up]");
    if (b) b.addEventListener("click", function(){ openUrl(up); });
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
  function more(n, what){ return n > 0 ? '<div class="more">+ ' + esc(n) + " more " + what + " not shown here; see the full answer.</div>" : ""; }
  function render(p){
    if (!p) return;
    if (p.kind === "credits_exhausted") return renderExhausted(p);
    var om = p.omitted || {};
    var h = "";
    if (p.pairs && p.pairs.length){
      h += "<h2>Compatibility</h2>";
      p.pairs.forEach(function(x){
        h += '<div class="pair"><div>' + esc(x.a) + " + " + esc(x.b)
          + (x.source ? '<div class="cite">Basis: ' + esc(x.source) + "</div>" : "")
          + '</div><div class="v ' + esc(x.verdict) + '">' + esc(x.verdict) + "</div></div>";
      });
      h += more(om.pairs, "pairs");
    }
    if (p.precursor && p.precursor.length){
      h += "<h2>Regulated precursor notice</h2>";
      p.precursor.forEach(function(x){
        h += '<div class="notice"><strong>' + esc(x.chemical) + (x.tier ? " (" + esc(x.tier) + ")" : "")
          + "</strong><br>" + esc(x.statement) + "</div>";
      });
      h += more(om.precursor, "notices");
    }
    if (p.documents && p.documents.length){
      h += "<h2>Source SDS</h2>";
      p.documents.forEach(function(d, i){
        h += '<div class="doc">' + esc(d.chemical) + ": " + esc(d.supplier || "unknown supplier")
          + (d.revision_date ? ", rev " + esc(d.revision_date) : "")
          + (safeUrl(d.url, ALLOWED_ORIGIN) ? '<button data-i="' + i + '">Open SDS</button>' : "") + "</div>";
      });
      h += more(om.documents, "documents");
    }
    var root = document.getElementById("root");
    root.innerHTML = h;
    reportSize();
    root.querySelectorAll("button[data-i]").forEach(function(b){
      b.addEventListener("click", function(){
        var url = safeUrl(p.documents[+b.getAttribute("data-i")].url, ALLOWED_ORIGIN);
        if (!url) return;
        openUrl(url);
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
  rpc("ui/initialize", {appInfo: {name: "msds-chain-compat-card", version: "1"}, appCapabilities: {}},
      function(m){
        applyTheme(((m.result || {}).hostContext || {}).theme);
        notify("ui/notifications/initialized");
      });
})();
</script></body></html>
"""


def card_html(allowed_origin: str, upgrade_origin: str) -> str:
    return (_CARD_HTML_TEMPLATE.replace("__ALLOWED_ORIGIN__", json.dumps(allowed_origin))
            .replace("__UPGRADE_ORIGIN__", json.dumps(upgrade_origin)))
