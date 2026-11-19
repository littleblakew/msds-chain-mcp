"""Stylesheet and script for the safety-tool cards inside the shared result-card template
(`ui_compat_card.card_html`): PPE, regulatory check, storage, emergency, transport, waste,
exposure limits and alternatives.

The structure, labels, chips and tones follow the web app's cards (`copilot/cards/*Card.tsx`
and `safetyCardParts.tsx`); class names mirror those parts. Every sentence on these cards is
a string the backend's `view` already holds: this code only lays it out, it never rewrites,
reorders or merges safety text. The few words that are interface (section headings, "listed",
"Source") are English, as on the rest of the card.

Both strings are raw: the script is injected unchanged into the template's IIFE and relies on
`esc`, `action`, `supplier`, `strips`, `more`, `safeUrl` and `ALLOWED_ORIGIN` defined there.
"""

SAFETY_CARDS_CSS = r"""
.vc+.vc{margin-top:8px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip.wrap{white-space:normal}
.strip p{margin:0}
.vnotes{margin-top:10px;display:grid;gap:4px;color:rgb(var(--s-label-2))}.vnotes p{margin:0}
.inset{margin-top:12px;border-radius:10px;background:rgb(var(--s-inset));padding:10px 12px}
.row2{display:flex;flex-wrap:wrap;align-items:flex-start;gap:10px}
.lbl{color:rgb(var(--s-label))}.dim{color:rgb(var(--s-label-3))}
.mini{font-size:10px;line-height:14px;letter-spacing:.04em;text-transform:uppercase;color:rgb(var(--s-label-3))}
.pro{color:rgb(var(--s-bad-ink));font-weight:500}.pro b{font-weight:600}
.vl{margin:0;padding:0;list-style:none;display:grid;gap:4px;line-height:1.35}
.vl.cap{font-size:12px;line-height:16px}.vl.sm{font-size:12px;line-height:16px}
.vl.num li{display:grid;grid-template-columns:16px 1fr;gap:6px}.vl.num .n{font-size:10px;text-align:right;padding-top:2px;color:rgb(var(--s-label-3))}
.codes{display:inline-flex;flex-wrap:wrap;gap:4px;margin-left:6px;vertical-align:middle}
.code{display:inline-block;white-space:nowrap;font:10px/16px ui-monospace,SFMono-Regular,Menlo,monospace;color:rgb(var(--s-label-3));background:rgb(var(--s-field));border-radius:4px;padding:0 4px}
.pin{padding:0 6px;border-radius:4px;background:rgb(var(--s-brand-ink)/.15);color:rgb(var(--s-brand-ink));font-size:10px;line-height:16px;font-weight:600}
.brandbox{margin-top:12px;border-radius:10px;background:rgb(var(--s-brand-ink)/.10);padding:10px 12px;display:grid;gap:6px;color:rgb(var(--s-label))}
.brandbox p{margin:0;line-height:1.35}
.disc>summary{cursor:pointer;display:flex;align-items:center;gap:6px;list-style:none}.disc>summary::-webkit-details-marker{display:none}
.disc>summary::after{content:"";flex:none;width:6px;height:6px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:rotate(45deg);margin-left:2px;opacity:.55}
.disc[open]>summary::after{transform:rotate(-135deg)}
.disc.inl>summary{display:inline-flex;flex-wrap:wrap}.disc>.dbody{margin-top:6px}
.dtop{margin-top:10px;padding-top:6px;border-top:1px solid rgb(var(--s-line))}
.vtag{display:inline-flex;align-items:center;gap:6px;font-size:12px;line-height:16px;color:rgb(var(--s-label-2));font-weight:500}
.vtag i{font-style:normal;padding:0 6px;border-radius:4px;background:rgb(var(--c-info)/.10);color:rgb(var(--s-info-ink));font-weight:600;font-size:10px;line-height:16px}
.sh{padding-top:4px;font-size:12px;line-height:16px;font-weight:600;color:rgb(var(--s-label-2))}.sh span{font-weight:400}
.sp{margin:0;color:rgb(var(--s-label));line-height:1.6}
.grid{display:grid;gap:6px}
pre.q{margin:6px 0 0;white-space:pre-wrap;font:12px/1.6 inherit;color:rgb(var(--s-label))}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:8px}
.tile{border-radius:10px;background:rgb(var(--s-inset));padding:10px 12px;min-width:0;display:flex;flex-direction:column}
.th{display:flex;align-items:center;gap:8px;font-size:10px;line-height:14px;letter-spacing:.04em;text-transform:uppercase;color:rgb(var(--s-label-3))}
.th b{font-weight:600}.th .n{margin-left:auto;text-transform:none;letter-spacing:0}.gl{flex:none}
.pinbox{margin-top:8px;border-radius:8px;background:rgb(var(--s-brand-ink)/.10);padding:8px 10px;color:rgb(var(--s-label))}
.pinbox .ph{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:4px}.pinbox p{margin:0;line-height:1.35}
.prim{margin-top:8px;font-size:15px;line-height:1.35;font-weight:500;color:rgb(var(--s-label))}
.prim+.prim{padding-top:8px;border-top:1px dashed rgb(var(--s-line))}
.also{margin-top:auto;padding-top:8px}.also>div{padding-top:6px;border-top:1px solid rgb(var(--s-line))}
.oth{margin:0;padding:0;list-style:none;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:4px 16px;font-size:12px;line-height:16px;color:rgb(var(--s-label-2))}
.oth li{display:flex;gap:6px;align-items:baseline;min-width:0}
.mx-d{display:none}.mx-m{display:block}
@media (min-width:640px){.mx-d{display:block}.mx-m{display:none}}
.mxg{display:grid;column-gap:10px}
.mxh{padding:0 10px 6px;font-size:10px;line-height:14px;letter-spacing:.04em;text-transform:uppercase;color:rgb(var(--s-label-3))}
.mxh span{display:flex;align-items:center;gap:6px}
.mxs{border-radius:10px;background:rgb(var(--s-brand-ink)/.10);padding:10px}
.mxr{padding:10px;border-top:1px solid rgb(var(--s-line))}
.mxc{display:grid;gap:4px;min-width:0;color:rgb(var(--s-label))}.mxc>div{line-height:1.35}
.mxb{border-radius:10px;padding:10px}.mxb.st{background:rgb(var(--s-brand-ink)/.10)}.mxb:not(.st){background:rgb(var(--s-inset))}
.mxb+.mxb{margin-top:10px}.mx2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.mxn{font-weight:600;color:rgb(var(--s-label));margin-bottom:6px}
.mxf{font-size:10px;line-height:14px;color:rgb(var(--s-label-3))}
.srcs{margin-top:12px;padding-top:10px;border-top:1px solid rgb(var(--s-line));display:grid;gap:4px}.srcs .src{margin:0;padding:0;border:0}
.srcs .nm{color:rgb(var(--s-label-2));font-weight:500}
.sw{flex:none;width:36px;height:36px;border-radius:6px;box-shadow:inset 0 0 0 1px rgba(0,0,0,.15)}
.sw-bad{background:rgb(var(--c-bad))}.sw-warn{background:rgb(var(--c-warn))}.sw-info{background:rgb(var(--c-info))}.sw-ok{background:rgb(var(--c-ok))}
.sw-ring{background:rgb(var(--s-surface));box-shadow:inset 0 0 0 2px rgb(var(--s-label-3))}
.sw-rbad{background:rgb(var(--s-surface));box-shadow:inset 0 0 0 2px rgb(var(--c-bad))}
.cls{display:flex;align-items:flex-start;gap:12px}.cls .t{font-size:15px;line-height:1.47;font-weight:600;color:rgb(var(--s-label))}
.cls .t span{margin-left:8px;font-size:12px;font-weight:400;color:rgb(var(--s-label-3))}
.nfpa{display:inline-flex;flex-wrap:wrap;align-items:center;gap:4px;overflow-wrap:normal}
.nfpa>b{font:10px/14px inherit;color:rgb(var(--s-label-3));margin-right:2px}
.nf{display:inline-flex;padding:0 6px;border-radius:4px;font-size:10px;line-height:20px;font-weight:600;white-space:nowrap}
.nf-h{background:rgb(var(--c-info)/.15);color:rgb(var(--s-info-ink))}.nf-f{background:rgb(var(--c-bad)/.10);color:rgb(var(--s-bad-ink))}.nf-r{background:rgb(var(--c-warn)/.10);color:rgb(var(--s-warn-ink))}
.plate{margin-top:12px;border-radius:10px;background:rgb(var(--s-inset));padding:12px;display:flex;flex-wrap:wrap;align-items:center;gap:12px}
.un{border:2px solid rgb(var(--s-label));border-radius:8px;padding:6px 12px;text-align:center;background:rgb(var(--s-surface))}
.un .v{font:700 20px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;color:rgb(var(--s-label))}
.fld .v{font:600 15px/1.47 ui-monospace,SFMono-Regular,Menlo,monospace;color:rgb(var(--s-label))}
.fields{display:flex;flex-wrap:wrap;gap:8px 20px}
.cand{display:inline-block;padding:0 8px;margin:2px 6px 0 0;border-radius:4px;background:rgb(var(--s-field));font:13px/20px ui-monospace,SFMono-Regular,Menlo,monospace;color:rgb(var(--s-label))}
.modes{margin-top:8px;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.mode{border-radius:8px;background:rgb(var(--s-inset));padding:6px 10px}.mode .v{font:13px/18px ui-monospace,SFMono-Regular,Menlo,monospace;color:rgb(var(--s-label))}
.wc{border-radius:10px;background:rgb(var(--s-inset));padding:10px 12px;display:grid;gap:4px}.wc+.wc{margin-top:8px}.wc.rest{background:rgb(var(--s-inset)/.6)}
.wc .t{font-weight:600;color:rgb(var(--s-label))}.wc.first .t{font-size:15px}.wc .t span{margin-left:8px;font-size:12px;font-weight:400;color:rgb(var(--s-label-3))}
.wc .kv{display:grid;grid-template-columns:72px 1fr;gap:8px}.wc .kv>span:first-child{color:rgb(var(--s-label-3))}.wc .kv>span:last-child{color:rgb(var(--s-label))}
.cds{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px}
.ext{margin-top:12px;border-radius:10px;background:rgb(var(--s-inset));padding:8px 10px;overflow-x:auto}
.exg{display:grid;column-gap:12px;align-items:baseline;min-width:420px}
.exh{padding-bottom:6px;font-size:10px;line-height:14px;letter-spacing:.04em;text-transform:uppercase;color:rgb(var(--s-label-3))}
.exr{padding:6px 0;border-top:1px solid rgb(var(--s-line))}
.exr .s{font-weight:500;color:rgb(var(--s-label))}.exr .s span{margin-left:6px;font-size:10px;color:rgb(var(--s-label-3))}
.exr .s em{font-style:normal;margin-left:6px;padding:0 4px;border-radius:4px;background:rgb(var(--s-field));font-size:10px;color:rgb(var(--s-label-3))}
.num2{font:13px/18px ui-monospace,SFMono-Regular,Menlo,monospace;color:rgb(var(--s-label));width:fit-content}
.num2.hi{padding:0 4px;border-radius:4px;background:rgb(var(--s-brand-ink)/.15);color:rgb(var(--s-brand-ink));font-weight:600}
.alts{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:8px}
.alt{border-radius:10px;background:rgb(var(--s-inset));padding:10px 12px;min-width:0;display:grid;gap:4px;align-content:start}
.alt .t{font-size:15px;line-height:1.47;font-weight:600;color:rgb(var(--s-label))}
.reg{padding:8px 0;border-top:1px solid rgb(var(--s-line))}.reg:first-child{border-top:0;padding-top:2px}.reg:last-child{padding-bottom:2px}
.reg .rc{display:inline-block;width:28px;font-weight:600;color:rgb(var(--s-label))}
.reg ul{margin:4px 0 0;padding:0;list-style:none;display:grid;gap:2px;color:rgb(var(--s-label));line-height:1.35}
.reg .inv{margin-top:4px;font-size:12px;line-height:16px;color:rgb(var(--s-label-2))}.reg .inv b{font-weight:500}
.scn{display:inline-flex;align-items:center;padding:0 8px;border-radius:6px;font-size:12px;line-height:20px;font-weight:600;white-space:nowrap}
.sc-fire{background:rgb(var(--c-bad)/.10);color:rgb(var(--s-bad-ink))}.sc-spill{background:rgb(var(--c-warn)/.10);color:rgb(var(--s-warn-ink))}.sc-exposure{background:rgb(var(--c-info)/.10);color:rgb(var(--s-info-ink))}
.rf{display:inline-flex;align-items:center;padding:0 8px;border-radius:6px;font-size:12px;line-height:20px;font-weight:600;background:rgb(var(--s-field));color:rgb(var(--s-label-2))}
"""

SAFETY_CARDS_JS = r"""
  // ---- safety-tool cards: laid out from the backend `view`, text untouched ----
  var SAFETY = {};
  var STATUS_TONE = {sds: "info", mixed: "info", tables: "info", regulatory: "info", listed: "info",
                     protocol: "info", derived: "pending", insufficient: "warn"};
  var LEVEL_TONE = {maximum: "bad", enhanced: "warn", basic: "pending"};
  var REGION_TONE = {restricted: "bad", detected: "warn", not_restricted: "pending", unverified: "pending", unsupported: "pending"};
  var SW = {flammable: "bad", oxidizer: "warn", corrosive_acid: "ring", corrosive_base: "ring", corrosive_unspecified: "ring",
            toxic: "info", general: "ok", explosive: "rbad", self_reactive: "rbad", pyrophoric: "rbad",
            pyrophoric_water_reactive: "rbad", water_reactive: "rbad", self_heating: "rbad"};
  var MODE = {road: "Road", air: "Air", sea: "Sea"};
  var REGION_EMPTY = "No recommendation on file. This does not mean none is needed; check SDS section 8.";
  var GL = {
    gloves: '<path d="M8 13V5.6a1.5 1.5 0 0 1 3 0V11"/><path d="M11 10.5V4.4a1.5 1.5 0 0 1 3 0V11"/><path d="M14 10.6V6a1.5 1.5 0 0 1 3 0v7.5c0 4-2.6 7-6.2 7-2.6 0-4.1-1.3-5.4-3.3l-1.7-3a1.5 1.5 0 0 1 2.4-1.7L8 14.6"/>',
    eye: '<path d="M3 10.2C3 8.4 4.3 7 6 7h12c1.7 0 3 1.4 3 3.2v1.6C21 13.6 19.7 15 18 15h-3l-1.6-2h-2.8L9 15H6c-1.7 0-3-1.4-3-3.2z"/><path d="M3 10.5H1.5M21 10.5h1.5"/>',
    respiratory: '<path d="M5 9.2c0-1.2.9-2.2 2-2.4L12 6l5 .8c1.1.2 2 1.2 2 2.4v3.2c0 3.2-3 6-7 6.6-4-.6-7-3.4-7-6.6z"/><path d="M5 10 2 8M19 10l3-2"/><circle cx="9" cy="12.5" r="1.6"/><circle cx="15" cy="12.5" r="1.6"/>',
    body: '<path d="M9 3.5 12 5.5l3-2 4.2 2.2 1.8 6.6-3 1v7.2H6v-7.2l-3-1 1.8-6.6z"/><path d="M12 5.5v15.2"/>'
  };
  function arr(x){ return Array.isArray(x) ? x : []; }
  function uniq(a){ return a.filter(function(x, i){ return a.indexOf(x) === i; }); }
  // House style: no em dash in text the user reads (same rules as the web app and the gateways).
  function fixLine(line){
    if (line.indexOf("—") < 0 || /^\s*\|/.test(line)) return line;
    var re = /[ \t ]*—{1,2}[ \t ]*/g, out = "", pos = 0, m;
    while ((m = re.exec(line))){
      out += line.slice(pos, m.index); pos = m.index + m[0].length;
      if (!out.trim() || !line.slice(pos).trim()) continue;
      var cjk = m[0].indexOf("——") >= 0 || /[　-鿿＀-￯]$/.test(out);
      var colon = out.indexOf(":") >= 0 || out.indexOf("：") >= 0;
      out += colon ? (cjk ? "；" : "; ") : (cjk ? "：" : ": ");
    }
    return out + line.slice(pos);
  }
  function sx(x){ return x == null ? "" : String(x).split("\n").map(fixLine).join("\n"); }
  function tx(x){ return esc(sx(x)); }
  // Backend sentences carry `**emphasis**` only; no links or images.
  function md(x){ return tx(x).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>"); }
  function codeChips(cs){ cs = arr(cs); return cs.map(function(c){ return '<span class="code">' + esc(c) + "</span>"; }).join(""); }
  function codes(cs){ return arr(cs).length ? '<span class="codes">' + codeChips(cs) + "</span>" : ""; }
  function glyph(key, px){
    return GL[key] ? '<svg class="gl" viewBox="0 0 24 24" width="' + px + '" height="' + px + '" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' + GL[key] + "</svg>" : "";
  }
  function toneChip(tone, label, wrap){ return '<span class="chip t-' + tone + (wrap ? " wrap" : "") + '">' + tx(label) + "</span>"; }
  function sigTone(w){ return /danger|危险|危險|gefahr|bahaya|危険|위험|опасно|peligro/i.test(w) ? "bad" : "warn"; }
  function statusChips(signal, v, extra){
    return (signal ? toneChip(sigTone(signal), signal) : "") + (extra || "") + toneChip(STATUS_TONE[v.status] || "pending", v.status_label);
  }
  function vhead(v, sub, chips){
    var name = sx(v.chemical || v.cas || "");
    return '<div class="head"><div style="min-width:0"><div class="name">' + esc(name)
      + (v.cas && v.cas !== v.chemical ? '<span class="cas">CAS ' + esc(v.cas) + "</span>" : "") + "</div>"
      + (v.chemical_detail ? '<div class="sub">' + tx(v.chemical_detail) + "</div>" : "")
      + '<div class="sub">' + tx(sub) + '</div></div><div class="chips">' + chips + "</div></div>";
  }
  function sec(t){ return '<div class="sec">' + tx(t) + "</div>"; }
  function vnotes(list){ list = arr(list); return list.length ? '<div class="vnotes">' + list.map(function(n){ return "<p>" + md(n) + "</p>"; }).join("") + "</div>" : ""; }
  function conflicts(list, prefix){
    list = arr(list);
    return list.length ? '<div class="strip">' + list.map(function(c){ return "<p>" + (prefix ? "<b>" + tx(prefix) + "</b> · " : "") + md(c) + "</p>"; }).join("") + "</div>" : "";
  }
  function lineText(l){
    return "<span" + (l.prohibit ? ' class="pro"' : "") + ">" + (l.prohibit ? '<b aria-hidden="true">✕ </b>' : "") + md(l.text) + codes(l.codes) + "</span>";
  }
  function plain(lines, cls){
    lines = arr(lines);
    return lines.length ? '<ul class="vl ' + (cls || "") + '">' + lines.map(function(l){ return "<li>" + lineText(l) + "</li>"; }).join("") + "</ul>" : "";
  }
  function textLines(list){ return arr(list).map(function(t){ return {text: t}; }); }
  function numbered(lines){
    lines = arr(lines);
    return lines.length ? '<ol class="vl num">' + lines.map(function(l, i){ return '<li><span class="n">' + (i + 1) + "</span>" + lineText(l) + "</li>"; }).join("") + "</ol>" : "";
  }
  function disc(summary, body, open, cls){
    return '<details class="disc ' + (cls || "") + '"' + (open ? " open" : "") + "><summary>" + summary + '</summary><div class="dbody">' + body + "</div></details>";
  }
  function sdsTag(title){ return '<span class="vtag"><i>SDS</i>' + tx(title) + "</span>"; }
  // Supplier SDS text (section 4 / 13 ...) in the original order; page-furniture paragraphs are not shown.
  function sdsBlock(paras, truncated, src, title, open){
    paras = arr(paras).filter(function(p){ return !p.page; });
    if (!paras.length) return "";
    var h = paras.map(function(p){
      if (p.heading) return '<div class="sh">' + tx(p.heading) + (p.text ? "<span> " + tx(p.text) + "</span>" : "") + "</div>";
      return '<p class="sp">' + (p.label ? "<b>" + tx(p.label) + ": </b>" : "") + tx(p.text) + "</p>";
    }).join("");
    if (truncated){
      var u = src && src.url ? safeUrl(src.url, ALLOWED_ORIGIN) : null, lab = "truncated · open the original SDS";
      h += '<div><span class="cut">' + (u ? '<button class="sup" data-a="' + action(u, ALLOWED_ORIGIN) + '">' + esc(lab) + "</button>" : esc(lab)) + "</span></div>";
    }
    return '<div class="inset">' + disc(sdsTag(title), '<div class="grid">' + h + "</div>", open) + "</div>";
  }
  function provenance(s){
    var u = s.url ? safeUrl(s.url, ALLOWED_ORIGIN) : null;
    return ((s.supplier || u) ? supplier(s.supplier || "Original SDS", u) : "")
      + (s.version ? "<span>Version: " + esc(s.version) + "</span>" : "")
      + (s.revision_date ? "<span>Revision date: " + esc(s.revision_date) + "</span>" : "");
  }
  function srcFooter(it){
    return (it.source ? '<div class="src"><span>Source</span>' + provenance(it.source) + "</div>" : "") + mdStrips(it.notices);
  }
  // One source line per record on multi-chemical cards.
  function srcLines(rows){
    var seen = {}, out = "";
    rows.forEach(function(r){
      var s = r.it.source; if (!s) return;
      var id = [s.supplier, s.revision_date, s.version, s.url].join("|");
      if (seen[id]) return; seen[id] = 1;
      out += '<div class="src row"><span class="nm">' + esc(r.name) + "</span>" + provenance(s) + "</div>";
    });
    var notes = rows.map(function(r){ return mdStrips(r.it.notices); }).join("");
    return (out ? '<div class="srcs">' + out + "</div>" : "") + notes;
  }
  function mdStrips(list){ return arr(list).map(function(t){ return '<div class="strip">' + md(t) + "</div>"; }).join(""); }
  function shell(inner){ return '<div class="shell vc">' + inner + "</div>"; }
  function stack(p, fn){ return arr(p.items).map(fn).join("") + more(p.omitted, "chemicals"); }

  // ---- PPE ----
  function pinTag(it){ return '<span class="pin">' + (it.kind === "substance_note" ? "Substance note" : "SDS text") + "</span>"; }
  function wrappable(t){ return sx(t).split("/").map(esc).join("/<wbr>"); }
  function ppeTile(r){
    var also = arr(r.also), h = '<div class="tile"><div class="th">' + glyph(r.key, 18) + "<b>" + tx(r.label) + "</b>"
      + (r.total > 0 ? '<span class="n">' + esc(r.total) + " listed</span>" : "") + "</div>";
    if (r.total === 0) h += '<div class="cap" style="margin-top:8px">' + esc(REGION_EMPTY) + "</div>";
    arr(r.pinned).forEach(function(it){
      h += '<div class="pinbox"><div class="ph">' + pinTag(it) + codeChips(it.codes) + "</div><p>" + tx(it.text) + "</p></div>";
    });
    arr(r.primary).forEach(function(it){ h += '<div class="prim">' + tx(it.text) + codes(it.codes) + "</div>"; });
    if (also.length){
      var ac = uniq([].concat.apply([], also.map(function(a){ return arr(a.codes); })));
      h += '<div class="also"><div>' + disc('<span class="cap">Also listed · ' + also.length + "</span>" + codeChips(ac),
        '<ul class="vl sm">' + also.map(function(it){ return "<li>" + tx(it.text) + codes(it.codes) + "</li>"; }).join("") + "</ul>", false, "inl") + "</div></div>";
    }
    return h + "</div>";
  }
  function ppeCard(it){
    var v = it.view, hasItems = arr(v.regions).some(function(r){ return r.total > 0; });
    var h = vhead(v, "PPE recommendation", statusChips(v.signal_word, v));
    if (v.level_label || v.summary)
      h += '<div class="inset row2">' + (v.level_label ? toneChip(LEVEL_TONE[v.level] || "pending", v.level_label) : "")
        + (v.summary ? '<span class="lbl">' + tx(v.summary) + "</span>" : "") + "</div>";
    if (v.status === "insufficient" || !hasItems) h += vnotes(v.notes);
    h += conflicts(v.conflict);
    if (hasItems) h += sec("What to wear · strongest item per region") + '<div class="tiles">' + arr(v.regions).map(ppeTile).join("") + "</div>";
    if (arr(v.other).length)
      h += sec(v.other_label) + '<ul class="oth">' + v.other.map(function(o){ return "<li>" + codeChips(o.codes) + "<span>" + tx(o.text) + "</span></li>"; }).join("") + "</ul>";
    if (v.status !== "insufficient" && hasItems) h += vnotes(v.notes);
    return shell(h + srcFooter(it));
  }
  // The strictest item per region across chemicals: lowest backend `rung`, plus the backend's co-primary rungs.
  function strictest(rows, keys){
    var out = {};
    keys.forEach(function(key){
      var cands = [], co = {};
      rows.forEach(function(r){
        var reg = arr(r.view.regions).filter(function(x){ return x.key === key; })[0];
        if (reg && reg.co_primary) co = reg.co_primary;
        arr(reg && reg.primary).forEach(function(item){ if (typeof item.rung === "number") cands.push({item: item, name: r.name}); });
      });
      if (!cands.length){ out[key] = []; return; }
      var min = Math.min.apply(null, cands.map(function(c){ return c.item.rung; }));
      var top = [min].concat(arr(co[String(min)])), merged = [], idx = {};
      cands.filter(function(c){ return top.indexOf(c.item.rung) >= 0; }).forEach(function(c){
        var k = c.item.text.toLowerCase();
        if (idx[k] != null){ if (merged[idx[k]].from.indexOf(c.name) < 0) merged[idx[k]].from.push(c.name); }
        else { idx[k] = merged.length; merged.push({item: c.item, from: [c.name]}); }
      });
      out[key] = merged;
    });
    return out;
  }
  function notCompared(rows, keys){
    var out = {};
    keys.forEach(function(key){
      out[key] = [];
      rows.forEach(function(r){
        var reg = arr(r.view.regions).filter(function(x){ return x.key === key; })[0];
        if (arr(reg && reg.primary).some(function(it){ return typeof it.rung === "number"; })) return;
        var why = r.view.status === "insufficient" ? "insufficient data" : arr(reg && reg.pinned).length ? "SDS text only, see row" : "nothing listed";
        out[key].push(r.name + " (" + why + ")");
      });
    });
    return out;
  }
  function strictCell(entries, skipped){
    var h = '<div class="mxc">' + entries.map(function(e){
      return "<div><b>" + wrappable(e.item.text) + "</b>" + codes(e.item.codes) + '<div class="mxf">' + tx(e.from.join(" · ")) + "</div></div>";
    }).join("");
    if (skipped.length) h += '<div class="mxf">Not compared: ' + tx(skipped.join(", ")) + "</div>";
    return h + "</div>";
  }
  function mxCell(region){
    if (!region) return "";
    var h = '<div class="mxc">';
    if (!arr(region.pinned).length && !arr(region.primary).length) h += '<div class="cap">' + esc(REGION_EMPTY) + "</div>";
    arr(region.pinned).forEach(function(it){ h += "<div>" + pinTag(it) + " " + wrappable(it.text) + codes(it.codes) + "</div>"; });
    arr(region.primary).forEach(function(it){ h += "<div>" + wrappable(it.text) + codes(it.codes) + "</div>"; });
    return h + "</div>";
  }
  function rowNotes(list){ list = arr(list); return list.length ? '<div class="cap" style="margin-top:4px">' + list.map(function(n){ return "<p style=\"margin:0\">" + md(n) + "</p>"; }).join("") + "</div>" : ""; }
  function ppeMatrix(p){
    var rows = arr(p.items).map(function(it){ return {name: sx(it.view.chemical || it.view.cas || ""), view: it.view, it: it}; });
    var regions = arr(rows[0].view.regions).map(function(r){ return {key: r.key, label: r.label}; });
    var keys = regions.map(function(r){ return r.key; });
    var strict = strictest(rows, keys), skipped = notCompared(rows, keys);
    function regionOf(row, k){ return arr(row.view.regions).filter(function(r){ return r.key === k; })[0]; }
    var cols = "grid-template-columns:minmax(148px,.8fr) repeat(" + keys.length + ",minmax(0,1fr))";
    var d = '<div class="mx-d" style="margin-top:12px"><div class="mxg mxh" style="' + cols + '"><span></span>'
      + regions.map(function(r){ return "<span>" + glyph(r.key, 16) + tx(r.label) + "</span>"; }).join("") + "</div>"
      + '<div class="mxg mxs" style="' + cols + '"><span class="lbl" style="font-weight:600">Strictest across all</span>'
      + keys.map(function(k){ return strictCell(strict[k], skipped[k]); }).join("") + "</div>"
      + rows.map(function(row){
        var v = row.view;
        return '<div class="mxg mxr" style="' + cols + '"><div style="min-width:0"><div class="lbl" style="font-weight:600">' + esc(row.name) + "</div>"
          + (v.chemical_detail ? '<div class="mxf">' + tx(v.chemical_detail) + "</div>" : "")
          + '<div class="chips" style="margin-top:4px">' + statusChips(v.signal_word, v) + "</div>" + rowNotes(v.notes) + "</div>"
          + keys.map(function(k){ return mxCell(regionOf(row, k)); }).join("") + "</div>";
      }).join("") + "</div>";
    var blocks = [{name: "Strictest across all", st: true}].concat(rows.map(function(r){ return {name: r.name, row: r}; }));
    var m = '<div class="mx-m" style="margin-top:12px">' + blocks.map(function(b){
      return '<div class="mxb' + (b.st ? " st" : "") + '"><div class="mxn">' + esc(b.name) + "</div>" + (b.row ? rowNotes(b.row.view.notes) : "")
        + '<div class="mx2">' + regions.map(function(r){
          return "<div><div class=\"mini\" style=\"display:flex;align-items:center;gap:4px;margin-bottom:2px\">" + glyph(r.key, 14) + tx(r.label) + "</div>"
            + (b.st ? strictCell(strict[r.key], skipped[r.key]) : mxCell(regionOf(b.row, r.key))) + "</div>";
        }).join("") + "</div></div>";
    }).join("") + "</div>";
    var cf = rows.map(function(r){ return conflicts(r.view.conflict, r.name); }).join("");
    return shell('<div class="name">PPE for ' + rows.length + ' chemicals</div><div class="sub">PPE recommendation</div>' + d + m + cf + srcLines(rows))
      + more(p.omitted, "chemicals");
  }
  SAFETY.ppe = function(p){ return arr(p.items).length > 1 ? ppeMatrix(p) : stack(p, ppeCard); };

  // ---- regulatory check ----
  function regionRow(r, v){
    var inv = r.inventory, detail = arr(r.details).concat(inv && inv.note ? [inv.note] : []);
    var h = '<div class="reg" data-status="' + esc(r.status) + '"><span class="rc">' + tx(r.region) + "</span>" + toneChip(REGION_TONE[r.status] || "pending", r.status_label);
    if (arr(r.flags).length) h += "<ul>" + r.flags.map(function(f){ return "<li>" + tx(f) + "</li>"; }).join("") + "</ul>";
    if (inv){
      // The official lookup page is on another origin than the card may open, so it is shown, not linked.
      h += '<div class="inv">' + (arr(inv.lists).length ? tx(inv.lists.join(", ")) : tx(v.inventory_label)) + " · <b>" + tx(inv.status_label) + "</b>"
        + (inv.lookup ? " · " + tx(inv.lookup.name) + (inv.lookup.url ? " " + esc(inv.lookup.url) : "") : "") + "</div>";
    }
    if (detail.length) h += disc('<span class="mini">' + tx(v.details_label) + "</span>", '<div class="grid cap">' + detail.map(function(d){ return "<p style=\"margin:0\">" + md(d) + "</p>"; }).join("") + "</div>", false, "inl");
    return h + "</div>";
  }
  SAFETY.compliance = function(p){
    return stack(p, function(it){
      var v = it.view;
      return shell(vhead(v, v.subtitle, statusChips(null, v)) + '<div class="inset" style="padding:8px 12px">' + arr(v.rows).map(function(r){ return regionRow(r, v); }).join("") + "</div>" + vnotes(v.notes) + srcFooter(it));
    });
  };

  // ---- storage ----
  function nfpa(n){
    if (!n) return "";
    var cells = [["health", "Health", "nf-h"], ["fire", "Fire", "nf-f"], ["reactivity", "Reactivity", "nf-r"]].filter(function(c){ return typeof n[c[0]] === "number"; });
    return '<span class="nfpa"><b>NFPA</b>' + cells.map(function(c){ return '<span class="nf ' + c[2] + '">' + c[1] + " " + esc(n[c[0]]) + "</span>"; }).join("") + "</span>";
  }
  function peroxide(v){
    var h = "";
    if (v.peroxide) h += '<div class="strip"><b>Peroxide former' + (v.peroxide.class_label ? " · " + tx(v.peroxide.class_label) : "") + "</b>"
      + plain(textLines(v.peroxide.guidance)) + (v.peroxide.citation ? '<div class="cap">' + tx(v.peroxide.citation) + "</div>" : "") + "</div>";
    if (v.peroxide_note) h += '<p class="cap" style="margin:8px 0 0">' + tx(v.peroxide_note) + "</p>";
    return h;
  }
  SAFETY.storage = function(p){
    return stack(p, function(it){
      var v = it.view, insufficient = v.status === "insufficient";
      var h = vhead(v, "Storage", statusChips(null, v));
      if (v.class_label){
        h += '<div class="inset cls">' + (SW[v.storage_class] ? '<span class="sw sw-' + SW[v.storage_class] + '" aria-hidden="true"></span>' : "")
          + '<div style="min-width:0;display:grid;gap:2px"><div class="t">' + tx(v.class_label) + (v.cabinet_color ? "<span>" + tx(v.cabinet_color) + "</span>" : "") + "</div>"
          + (v.cabinet ? '<div class="lbl">' + tx(v.cabinet) + "</div>" : "")
          + (v.temperature ? '<div><span class="dim">Temperature: </span>' + lineText({text: v.temperature}) + "</div>" : "") + nfpa(v.nfpa) + "</div></div>";
      }
      if (insufficient) h += vnotes(v.notes);
      h += conflicts(v.conflict) + peroxide(v);
      if (arr(v.keep_away).length) h += sec("Keep away from") + plain(textLines(v.keep_away));
      var fromSds = arr(v.requirements_sds).concat(arr(v.special));
      if (fromSds.length) h += sec("From this SDS") + plain(fromSds);
      if (arr(v.precautions).length) h += sec("GHS storage statements") + plain(v.precautions, "sm");
      if (arr(v.requirements_class).length)
        h += '<div class="dtop">' + disc('<span class="cap">' + v.requirements_class.length + " general rules for this storage class, not this SDS</span>", plain(v.requirements_class), false, "inl") + "</div>";
      if (arr(v.requirements_general).length) h += sec("General practice, not specific to this chemical") + plain(v.requirements_general);
      if (!insufficient) h += vnotes(v.notes);
      return shell(h + srcFooter(it));
    });
  };

  // ---- emergency ----
  SAFETY.emergency = function(p){
    return stack(p, function(it){
      var v = it.view, gen = v.status === "insufficient" || v.status === "general";
      var scn = v.scenario ? '<span class="scn sc-' + esc(v.scenario) + '">' + esc(v.scenario.charAt(0).toUpperCase() + v.scenario.slice(1)) + "</span>" : "";
      var h = vhead(v, "Emergency response", statusChips(v.signal_word, v, scn));
      if (arr(v.protocol).length)
        h += '<div class="brandbox">' + v.protocol.map(function(x){ return "<p>" + md(x.text) + "</p>"; }).join("")
          + (v.protocol_citation ? '<div class="mini" style="text-transform:none;letter-spacing:0"><b>Protocol source: </b>' + tx(v.protocol_citation) + "</div>" : "") + "</div>";
      if (gen) h += vnotes(v.notes);
      h += conflicts(v.conflict);
      if (arr(v.steps).length) h += sec("Do now") + numbered(v.steps);
      h += sdsBlock(v.sds_paragraphs, v.sds_truncated, it.source, "Supplier SDS · Section 4", true);
      if (arr(v.sds).length) h += sec("From this SDS") + plain(v.sds);
      if (arr(v.precaution_actions).length) h += sec("GHS response statements") + plain(v.precaution_actions, "cap");
      var ha = arr(v.hazard_actions), donts = ha.filter(function(a){ return a.prohibit; }), generic = ha.filter(function(a){ return !a.prohibit; });
      if (donts.length) h += sec("Do not") + plain(donts);
      if (generic.length) h += '<div class="dtop">' + disc('<span class="cap">' + generic.length + " generic actions from hazard codes, not this SDS</span>", plain(generic), false, "inl") + "</div>";
      if (!gen) h += vnotes(v.notes);
      return shell(h + srcFooter(it));
    });
  };

  // ---- transport ----
  function field(label, value){ return value ? '<div class="fld"><div class="mini">' + esc(label) + '</div><div class="v">' + tx(value) + "</div></div>" : ""; }
  SAFETY.transport = function(p){
    return stack(p, function(it){
      var v = it.view, h = vhead(v, "Transport classification", statusChips(null, v));
      if (v.un_number || arr(v.un_candidates).length){
        h += '<div class="plate">' + (v.un_number
          ? '<div class="un"><div class="mini">UN number</div><div class="v">' + tx(v.un_number) + "</div></div>"
          : '<div style="min-width:0"><div class="mini">UN number candidates (none chosen)</div>'
            + v.un_candidates.map(function(c){ return '<span class="cand">' + tx(c) + "</span>"; }).join("")
            + (v.ambiguity_reason ? '<div class="cap" style="margin-top:4px">' + tx(v.ambiguity_reason) + "</div>" : "") + "</div>")
          + '<div style="min-width:0;display:grid;gap:8px">' + (v.shipping_name ? '<div class="lbl" style="font-weight:500">' + tx(v.shipping_name) + "</div>" : "")
          + '<div class="fields">' + field("Class", v.hazard_class) + field("Subsidiary risk", v.subsidiary_risk) + field("Packing group", v.packing_group) + "</div></div></div>";
      }
      if (arr(v.modes).length)
        h += '<div class="modes">' + v.modes.map(function(m){ return '<div class="mode"><div class="mini">' + esc(MODE[m.key] || m.key) + '</div><div class="v">' + tx(m.text) + "</div></div>"; }).join("") + "</div>";
      h += conflicts(v.conflict) + vnotes(v.notes);
      if (v.evidence) h += '<div class="inset">' + disc(sdsTag("Supplier SDS · Section 14"), '<pre class="q">' + tx(v.evidence.quote) + "</pre>", false) + "</div>";
      return shell(h + srcFooter(it));
    });
  };

  // ---- waste ----
  function segs(list){
    list = arr(list);
    return list.map(function(g, i){ return (i ? " " : "") + lineText({text: String(g.text).replace(/^\s+/, ""), prohibit: g.prohibit}); }).join("");
  }
  function wasteCat(c, first){
    function kv(label, v){ return arr(v).length ? '<div class="kv"><span>' + label + "</span><span>" + segs(v) + "</span></div>" : ""; }
    return '<div class="wc ' + (first ? "first" : "rest") + '"><div class="t">' + tx(c.label) + (c.fallback ? "<span>fallback category</span>" : "") + "</div>"
      + kv("Method", c.method) + kv("Container", c.container) + kv("Regulation", c.regulatory) + "</div>";
  }
  SAFETY.waste = function(p){
    return stack(p, function(it){
      var v = it.view, h = vhead(v, "Waste disposal", statusChips(null, v));
      if (arr(v.codes).length) h += '<div class="cds">' + codeChips(v.codes) + "</div>";
      if (v.status === "insufficient") h += vnotes(v.notes);
      h += conflicts(v.conflict);
      if (arr(v.categories).length) h += '<div style="margin-top:12px">' + v.categories.map(function(c, i){ return wasteCat(c, i === 0); }).join("") + "</div>";
      if (arr(v.sds_notes).length) h += sec("From this SDS") + plain(textLines(v.sds_notes));
      h += sdsBlock(v.sds_paragraphs, v.sds_truncated, it.source, "Supplier SDS · Section 13", false);
      if (v.status !== "insufficient") h += vnotes(v.notes);
      return shell(h + srcFooter(it));
    });
  };

  // ---- exposure limits ----
  SAFETY.exposure = function(p){
    return stack(p, function(it){
      var v = it.view, types = arr(v.types), cols = "grid-template-columns:minmax(120px,1.2fr) repeat(" + types.length + ",minmax(0,1fr))";
      var anyStrict = arr(v.rows).some(function(r){ return Object.keys(r.cells || {}).some(function(k){ return arr(r.cells[k]).some(function(c){ return c.strictest; }); }); });
      var h = vhead(v, "Occupational exposure limits", statusChips(null, v, v.region_filter ? '<span class="rf">' + tx(v.region_filter) + "</span>" : "")) + conflicts(v.conflict);
      if (arr(v.rows).length){
        h += '<div class="ext"><div class="exg exh" style="' + cols + '"><span>Standard</span>' + types.map(function(t){ return "<span>" + tx(t) + "</span>"; }).join("") + "</div>"
          + v.rows.map(function(r){
            var src = (r.version || r.url) ? disc('<span class="mini">Version and link</span>', '<div class="cap">' + (r.version ? "<div>" + tx(r.version) + "</div>" : "") + (r.url ? '<div style="word-break:break-all">' + esc(r.url) + "</div>" : "") + "</div>", false, "inl") : "";
            return '<div class="exg exr" style="' + cols + '"><div style="min-width:0"><div class="s">' + tx(r.source) + (r.region ? "<span>" + tx(r.region) + "</span>" : "")
              + (v.status === "tables" && r.origin === "curated" ? "<em>curated table</em>" : "") + "</div>"
              + (r.entry ? '<div class="cap">' + tx(r.entry) + "</div>" : "") + (r.notation ? '<div class="mini" style="text-transform:none;letter-spacing:0">' + tx(r.notation) + "</div>" : "") + src + "</div>"
              + types.map(function(t){
                return '<div style="min-width:0;display:grid;gap:2px">' + arr((r.cells || {})[t]).map(function(c){ return '<span class="num2' + (c.strictest ? " hi" : "") + '">' + esc((c.value + " " + (c.unit || "")).trim()) + "</span>"; }).join("") + "</div>";
              }).join("") + "</div>";
          }).join("")
          + (anyStrict ? '<div class="mini" style="padding-top:6px;text-transform:none;letter-spacing:0">Highlighted: lowest value of each type in the same unit (ppm and mg/m³ are not compared)</div>' : "") + "</div>";
      }
      if (v.excerpt)
        h += '<div class="inset">' + sdsTag("Supplier SDS · Section 8.1") + '<pre class="q">' + tx(v.excerpt.quote) + "</pre>"
          + (v.excerpt.truncated ? '<div class="cap" style="color:rgb(var(--s-warn-ink))">truncated · open the original SDS</div>' : "") + "</div>";
      return shell(h + vnotes(v.notes) + srcFooter(it));
    });
  };

  // ---- safer alternatives ----
  SAFETY.substitution = function(p){
    return stack(p, function(it){
      var v = it.view, h = vhead(v, "Safer alternatives", statusChips(null, v));
      if (v.risk_level) h += '<div class="inset row2" style="align-items:center"><span class="cap">Current substance</span>' + toneChip(v.risk_tone || "pending", v.risk_level, true) + "</div>";
      h += conflicts(v.conflict);
      if (v.disclosure) h += '<div class="strip">' + tx(v.disclosure) + "</div>";
      if (arr(v.alternatives).length){
        h += sec(v.generic ? "General approach" : "Candidates · " + v.alternatives.length) + '<div class="alts">' + v.alternatives.map(function(a){
          return '<div class="alt"><div class="t">' + tx(a.name) + (a.cas ? '<span class="cas">CAS ' + esc(a.cas) + "</span>" : "") + "</div>"
            + (a.risk_level ? "<div>" + toneChip("pending", a.risk_level, true) + "</div>" : "")
            + (a.rationale ? '<div class="lbl"><span class="dim">Why: </span>' + tx(a.rationale) + "</div>" : "")
            + (a.trade_offs ? '<div><span class="dim">Trade-offs: </span>' + tx(a.trade_offs) + "</div>" : "") + "</div>";
        }).join("") + "</div>";
        if (v.shared_trade_off) h += '<div class="cap" style="margin-top:8px"><span class="dim">Trade-offs: </span>' + tx(v.shared_trade_off) + "</div>";
      }
      return shell(h + vnotes(v.notes) + srcFooter(it));
    });
  };
"""
