from __future__ import annotations



from msds_mcp.calls import _expose
from msds_mcp.disclosures import _form_disclosure_lines



def _unresolved_block(data: dict, *, trailing_newline: bool = False) -> list[str]:
    """CI-470 phase 3b: render WHY each name was unresolved, not just that it was.

    The backend has always known the difference between "we hold no record",
    "a lookup step failed so this absence is not trustworthy" and "the name and
    the CAS you gave contradict each other, so we refused" — and until now this
    surface flattened all three into one line of bare names. A model reading
    `**Unresolved:** hydrofluoric acid` reasonably concludes we have no data and
    tells the user so; that conclusion is one we are not entitled to when the
    real cause was a failed lookup.

    🔴 Falls back to the plain name list whenever the backend did not send
    `unresolved_detail` (older backend, or an endpoint that does not build it
    yet). Silence must never turn into an invented reason.
    """
    names = data.get("unresolved") or []
    if not names:
        return []
    tail = "\n" if trailing_newline else ""
    detail = {d.get("query"): d for d in (data.get("unresolved_detail") or [])
              if isinstance(d, dict)}
    if not detail:
        return [f"**Unresolved:** {', '.join(names)}{tail}"]
    lines = ["**Unresolved:**"]
    for name in names:
        d = detail.get(name)
        # English surface: this server renders in English (see the tool
        # descriptions); `reason_en` is the backend's own English sentence, and
        # `reason` (zh) is deliberately NOT used as a fallback — a Chinese
        # sentence dropped into an English answer reads as a rendering bug.
        why = (d or {}).get("reason_en")
        lines.append(f"- **{name}**" + (f" — {why}" if why else ""))
        lines.extend(_identity_candidate_lines(d, indent="  "))
    return lines + ([""] if trailing_newline else [])


def _identity_candidate_lines(detail: dict | None, *, indent: str = "") -> list[str]:
    """Render `unresolved_detail.identity_confirmation`: the names the backend would
    ask the user to choose between.

    Without this the text surface says "please confirm which one you mean" and then
    offers nothing to choose from — the candidates reach structuredContent only, and
    the model reads the text.

    🔴 `for_confirmation_only` means exactly that: these go to the USER as a question,
    never into the answer as the resolved chemical. The heading says so in words,
    because a bulleted CAS list reads as a result otherwise.
    🔴 Order is the order the backend considered them, not a ranking — do not mark
    the first one as best, and do not sort.
    Key absent ⇒ nothing to ask about; render nothing rather than invent a choice.
    """
    conf = detail.get("identity_confirmation") if isinstance(detail, dict) else None
    cands = [c for c in ((conf.get("candidates") if isinstance(conf, dict) else None) or [])
             if isinstance(c, dict) and (c.get("cas") or c.get("name"))]
    if not cands:
        return []
    out = [f"{indent}Possible matches (unranked) — ask the user which one they mean; "
           "do NOT treat any of these as the identified chemical:"]
    for c in cands:
        label = c.get("name") or c.get("cas")
        if c.get("cas") and c.get("name"):
            label += f" (CAS {c['cas']})"
        if c.get("matched_name"):
            label += f", matched as \"{c['matched_name']}\""
        why = c.get("why_not_auto")
        out.append(f"{indent}- {label}" + (f" — not auto-selected: `{why}`" if why else ""))
    return out


def _rejected_products_block(data: dict) -> list[str]:
    """CI-277: render the backend's explicit product/mixture refusals.

    🔴 Why this must never be silently dropped: the backend removes formulated
    products from BOTH `name_to_cas` and `unresolved` and reports them only in
    `rejected_products`. A renderer that ignores the key shows "2 chemicals
    submitted, 0 unresolved, 0 incompatible pairs" for
    `["Windex", "hydrochloric acid"]` — an unearned all-clear for a pair that
    was never evaluated at all. Absence of a verdict must read as absence, not
    as safety.
    """
    rejected = data.get("rejected_products") or []
    if not rejected:
        return []
    lines = [
        "**⚠️ Not evaluated — formulated products (mixtures):**",
    ]
    for r in rejected:
        name = r.get("query") or "(unnamed)"
        lines.append(
            f"- **{name}** — a formulated product. Its hazard classification "
            f"applies to the whole formulation, so it is NOT accepted as an input "
            f"to compatibility or batch safety checks, and it was NOT substituted "
            f"by any of its components. Its SDS is retrievable with "
            f"`get_sds_document(\"{name}\")`."
        )
    lines.append(
        "These were **excluded from the results below** — treat any conclusion "
        "here as covering only the other inputs.\n"
    )
    return lines


def _no_hazard_basis_block(data: dict) -> list[str]:
    """CI-666: render the backend's "we matched a record but it has no hazard data" note.

    🔴 **Without this the fix does not reach the consumer that produced the bug report.**
    The Prod repro was `get_chemical_risk_warnings(["carbon disulfide"])` over MCP:
    the response was fully-formed and completely empty, and the only thing the model
    saw in `TextContent` was `"No risk warnings found for the given chemicals."` — which
    reads as "we checked, there are none". The backend now publishes a top-level
    `no_hazard_basis` list saying *why*; `_expose()` carries it into structuredContent
    for free, **but the model reads TextContent**, which is assembled field-by-field.
    Same shape of miss as CI-553/CI-562 for `precursor_disclosure`.

    🔴 The wording is rendered backend-side (5 languages, single source in the i18n
    catalog) — do NOT re-phrase it here or a second, unversioned copy starts drifting.
    That wording deliberately names the matched CAS: the substance-level answer can
    still be wrong (a name can match the wrong record), and the CAS is the only clue
    the caller has to notice that.

    🔴 A non-dict entry must not take down the whole safety answer — same guard, and
    same reason, as `_precursor_disclosure_block` / `_unresolved_block`.

    🔴 **天花板：渲染进 TextContent ≠ 用户读到。** [[CI-592]] 与 [[CI-523]] 都实测过——
    工具文本要经过 claude.ai / Copilot 那一层**重写**才到用户眼前，而结构化披露会被
    summary LLM 复述掉。所以这个 block 让披露**有机会**到达，不保证到达。
    ⇒ 别在票里、也别对外把「加了渲染线」说成「修复已到达消费者」；真要确定性，
    得走那条我们自己控制渲染的通道（web 快聊 / Slack / Teams 的 `rich_message.py`）。
    """
    entries = [e for e in (data.get("no_hazard_basis") or []) if isinstance(e, dict)]
    if not entries:
        return []
    lines = [
        "**⚠️ Some chemicals matched a record that carries no hazard data — "
        "this is NOT a finding that they are safe.**",
    ]
    for e in entries:
        name = e.get("query") or e.get("cas") or "Unknown"
        reason = e.get("reason_en") or e.get("reason") or ""
        lines.append(f"- **{name}** (CAS {e.get('cas', 'n/a')}): {reason}")
    return lines


def _batch_not_analysed(data: dict, submitted: list[str]) -> list[str]:
    """调用方提交了、但**没进分析**的那些名字（CI-570）。

    后端在解析之后按 `MAX_BATCH_CHEMICALS` 截断，被丢掉的输入在 `data["chemicals"]` 里
    一条都不出现——analysed / unresolved / rejected 三类都没有 ⇒ 差集就是它们。

    🔴 差集之所以敢做：后端 `_resolve_all` 用**调用方原样的字符串**（strip 后）当 key，
    不是解析后的规范名。若它换成规范名，`"67-64-1"` 会被算成「没进分析」——而那是一个
    看起来很权威的**假警报**，比不提示更糟。改后端那一侧的人：这里会跟着错，且不报错。
    """
    if not data.get("truncated"):
        return []
    # 🔴 **按大小写敏感比**（review 抓到的）：后端 `name_to_cas[name]` 保留原样大小写，
    # 所以 `"Acetone"` 和 `"acetone"` 在它那边是**两个 key**。这边若折叠成小写，
    # 「留下 Acetone、丢掉 acetone」时那个被丢的会被当成已入账 ⇒ `truncated=true` 配
    # `not_analysed=[]`，正是本票要防的那句「我们什么都没丢」。后端已经 strip 过，
    # 所以这里只 strip、不 lower。
    accounted = {
        (e.get("name") or "").strip()
        for e in (data.get("chemicals") or []) if isinstance(e, dict)
    }
    out: list[str] = []
    seen: set[str] = set()
    for raw in submitted:
        name = (raw or "").strip()
        if not name or name in accounted or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _batch_truncation_block(data: dict, submitted: list[str]) -> list[str]:
    """CI-570：提交了 13–20 个时，超出 12 的那些**既不在结果里、也没有任何提示**。

    与 CI-277 同一个形状：**没有结论被读成没有危害**。所以这段话要说清的不是「我们只算了
    12 个」，而是「下面的一切——**包括没有警告这件事**——都不涉及这几个」。
    """
    if not data.get("truncated"):
        return []
    dropped = _batch_not_analysed(data, submitted)
    lines = [
        "🔴 **This request exceeded the batch size limit, so not every submitted "
        "chemical was analysed.**",
    ]
    if dropped:
        # 🔴 作用域必须收窄到「相容性 / 风险」两节（review 抓到的）：受管制前体披露是
        # **在截断之前**算的（后端有意为之），它**能**点到被丢掉的那个物质。原文写
        # 「nothing below says anything about them」会让读者把两行之下那条真正适用的
        # 披露也当成不适用 —— 那是比不提示更糟的一种错。
        lines.append(
            f"These {len(dropped)} were NOT analysed. The compatibility and risk results "
            f"below cover only the others — **the absence of a warning there says nothing "
            f"about these**:"
        )
        lines.extend(f"- {name}" for name in dropped)
        if data.get("precursor_disclosure"):
            lines.append(
                "(The regulated-precursor notice below is computed on everything you "
                "submitted, so it may name these.)"
            )
        # 🔴 本工具拒收少于 2 个输入 ⇒ 丢掉恰好 1 个（13 送 12 算，最常见的一种）时，
        # 「拿这些再调一次」是一条**保证会被拒**的建议。
        lines.append(
            "Call batch_safety_check again with just those to get their results."
            if len(dropped) > 1 else
            f"This tool needs at least 2 chemicals, so check {dropped[0]} with "
            f"get_chemical_risk_warnings, or re-run the batch with it plus one other."
        )
    else:
        # 🔴 走到这里说明差集丢了东西：被截断的输入在 `data["chemicals"]` 的三类里
        # 一条都不该出现。**要吵，别含糊过去** —— 含糊的措辞会让
        # `_batch_not_analysed` 那条跨仓前提失效时看起来一切正常。
        lines.append(
            "We could not determine which ones. This is a defect on our side, not a "
            "statement that nothing was dropped — treat the results below as covering "
            "an unknown subset of what you submitted, and re-submit in groups of 12."
        )
    lines.append("")
    return lines


def _physical_form_disclosures_block(data: dict) -> list[str]:
    """CI-577：三个批量端点（/compatibility/check、/risk-warnings、/batch-safety）的
    **记录驱动**形态披露渲染进文本面。

    后端给顶层 `physical_form_disclosures: [{chemical, cas, physical_form,
    physical_form_disclosure}]`；`_expose()` 会把它带进 structuredContent，但模型读的是
    text ⇒ 不渲染就等于没修（CI-553 / CI-842 同一个形状）。单项渲染走
    `_form_disclosure_lines` 这一个出口，批量里有多个化学品，所以每行前面加化学品名
    （句子本身只说「这份 SDS」，不带名字就分不清在说哪一个）。
    `physical_form_lookup_failed` 为真时说一句：查询没跑成 ≠ 没有可披露的（CI-666）。
    """
    out: list[str] = []
    for item in data.get("physical_form_disclosures") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("chemical") or item.get("cas") or ""
        for line in _form_disclosure_lines(item):
            body = line[len("- ⚠️ "):] if line.startswith("- ⚠️ ") else line.lstrip("- ")
            out.append(f"- ⚠️ {name}: {body}" if name else line)
    if data.get("physical_form_lookup_failed"):
        out.append("- ⚠️ The physical-form check for the SDS on file could not run for "
                   "this request; which form (e.g. anhydrous vs aqueous) the cited sheet "
                   "describes is not confirmed.")
    if out:
        out.append("")
    return out


def _query_form_disclosure_block(data: dict) -> list[str]:
    """CI-842: render the backend's "you named a form, we answered from the parent
    record" disclosure into **text**.

    The backend publishes a top-level `query_form_disclosures` list on the three batch
    endpoints (/compatibility/check, /risk-warnings, /batch-safety). `_expose()` carries
    it into structuredContent for free — **but the model reads TextContent, and that is
    assembled field-by-field**, so a key no renderer mentions never appears in the text
    面 for any of the three. Same shape of miss as CI-553/CI-562 (`precursor_disclosure`)
    and CI-470/CI-666 (`no_hazard_basis`); this is the fourth time, which is why the
    single-substance half goes through the one shared exit (`_form_disclosure_lines`)
    instead of a fifth hand-written copy.

    What it is: the caller said a form word (`glacial` / `anhydrous` / `aqueous` /
    `浓` / `无水` …) and the record we answered from does NOT carry that word in its
    name ⇒ **a substitution happened**. Prod measurement before the backend fix:
    `glacial sulfuric acid` → 7664-93-9, `glacial ethanol` → 64-17-5,
    `glacial sodium hydroxide` → 1310-73-2 — three chemically impossible things,
    all answered from the parent record with not one word of disclosure.

    🔴 It is NOT `physical_form_disclosure` — do not merge the two and do not pick one.
    That one is driven by the **record** ("the SDS we hold describes the aqueous form");
    this one is driven by the **difference between the query and the matched name**
    ("you asked for anhydrous; we answered from the record called 'hydrofluoric acid'").
    Both can be true at once and they do not contradict each other.

    🔴 The wording is rendered backend-side (`note`, 5 languages, single source in the
    i18n catalog) — do NOT re-phrase it here or a second, unversioned copy starts
    drifting. Same red line as `_precursor_disclosure_block` / `_no_hazard_basis_block`.
    Note that zh/ja/de/id carry their own inline `**` around the negation, so the note is
    emitted bare: wrapping it again renders `**A**B**C**` and emphasises exactly the
    wrong half (the bug `_form_disclosure_lines` documents).

    🔴 A non-dict entry must not take down the whole safety answer — same guard, and
    same reason, as `_precursor_disclosure_block` / `_unresolved_block`: this block runs
    before any result rendering, so an AttributeError here would replace the answer the
    user asked for with a tool error, which is strictly worse than the missing disclosure.

    🔴 **天花板：渲染进 TextContent ≠ 用户读到**（CI-592 / CI-523 实测，`_no_hazard_basis_block`
    的 docstring 有完整论证）。这个 block 让披露**有机会**到达，不保证到达 ⇒ 别把它
    写成「披露已到达用户」。
    """
    entries = data.get("query_form_disclosures") or []
    if not entries:
        return []
    # 🔴 后端**在 12 个的截断闸之前**算这份披露（`_resolve_all` → 闸，`direct_api.py`
    # 那段注释写死了理由：被丢掉的物质用户确实提交过）。而 `batch_safety_check` 收 20 个
    # ⇒ **被披露点名的那个可以是根本没进分析的那个**，此时「下面那条答案」并不存在。
    # 同 `_precursor_disclosure_block` 记过的那条：头句不许承诺一份可能不在的答案。
    lines = [
        "**⚠️ A form/grade qualifier you specified is NOT in the name of the record "
        "we answered from — we answered from the parent compound's record.**"
        if data.get("truncated") else
        "**⚠️ A form/grade qualifier you specified is NOT in the name of the record "
        "we answered from — the answer below is for the parent compound.**",
    ]
    if data.get("truncated"):
        lines.append(
            "(This disclosure is computed on everything you submitted, before the "
            "batch size limit — so it may name a chemical that was not analysed at all.)"
        )
    bad = [e for e in entries if not isinstance(e, dict)]
    entries = [e for e in entries if isinstance(e, dict)]
    for e in bad:
        lines.append(f"- {e} (unrecognised disclosure entry — reported verbatim)")
    for e in entries:
        # 🔴 后端保证 `note` 在（`query_form_disclosure` 是 `{**match, "note": ...}`），
        # 但缺失时**必须仍然说出发生了替换**：这里 fall back 成结构化字段自己拼一句最小
        # 事实，而不是静默跳过——静默跳过正是本票要修的那个 bug 本身。
        note = e.get("note")
        if note:
            lines.append(f"- ⚠️ {note}")
        else:
            lines.append(
                f"- ⚠️ **{e.get('chemical', 'Unknown')}**: you specified "
                f"'{e.get('form', 'a form')}', but we answered from the record for "
                f"{e.get('matched', 'the parent compound')} "
                f"(CAS {e.get('cas', 'n/a')})."
            )
    lines.append("")
    return lines


# 🔴 CI-1099：限制条件必须长在**判定句**上，不能只待在平级的披露块里。
# 客户端模型会整块丢掉旁边的披露、而保留判定（判定才是用户问的东西）⇒ 「某一支限售」
# 被删掉、判定句留下，读起来是一个**无条件的放行**。而 `_precursor_disclosure_block`
# 的头句是我们自己写的，曾经还亲口把这块标成 informational only 并把读者指向下面的结论
# ——那等于我们自己签发的丢弃授权。
#
# 🔴 措辞仍**不在这里造**：成句来自后端 i18n（`statement`，5 语言、单一出处）。这里只引用
# `tier` / `note` 两个**机器可读分面**（`note` 是逐字抄自法规的阈值具文，后端刻意不翻译它）
# ⇒ 判定行上出现的是引用，不是第二份会漂的措辞。
def _precursor_index(data: dict) -> dict[str, list[dict]]:
    """把后端那份披露按「判定行拿得到的把手」（用户打的名字 / 匹配名 / CAS）反查。

    🔴 三个把手都要：后端按 CAS 去重（同一次调用里用名字和 CAS 问同一个物质只出一条），
    所以只按 `query_name` 对是会漏的——漏的方向是**少披露**。
    """
    index: dict[str, list[dict]] = {}
    for e in data.get("precursor_disclosure") or []:
        if not isinstance(e, dict):
            continue
        for handle in (e.get("query_name"), e.get("matched_name"), e.get("cas")):
            if handle:
                index.setdefault(str(handle).strip().lower(), []).append(e)
    return index


# 判定句的补语。🔴 它必须与判定同处一行：分行写就又变成一个可以单独丢掉的块。
_PRECURSOR_VERDICT_CLAUSE = (
    " — ⚠️ **conditional: a regulated precursor is involved.** This verdict covers the "
    "chemistry only; it is not clearance to supply, sell or transfer."
)


def _pair_sides(pair: dict) -> tuple[tuple, tuple]:
    """一对的两侧，每侧写成 `(名字, CAS)`。

    🔴 **名字与 CAS 是两种东西，别摊平成一串把手**：名字是「显示用的那个」——它必须和
    判定行上印的字一致；CAS 只是**匹配用的**。摊平之后，名字没命中而 CAS 命中时，
    披露行会印出一串 CAS 号，读者对不上判定行里的名字（CI-1099 review 的第三条）。
    这一层名字为空时才退回用 CAS 当显示名。
    """
    return ((pair.get("chem1"), pair.get("cas_a")),
            (pair.get("chem2"), pair.get("cas_b")))


def _precursor_side_entries(
    index: dict[str, list[dict]], side: tuple, done: set[int],
) -> tuple[str, list[dict]]:
    """一侧的 `(显示名, 本侧新命中的披露)`。`done` 跨侧去重（同一条只算一次）。"""
    label = next((str(h) for h in side if h), "")
    found: list[dict] = []
    for handle in side:
        if not handle:
            continue
        for e in index.get(str(handle).strip().lower(), []):
            if id(e) not in done:
                done.add(id(e))
                found.append(e)
    return label, found


def _precursor_pair_note(
    index: dict[str, list[dict]], *sides: tuple,
) -> tuple[str, list[str]]:
    """判定行的 `(补语, 附加行)`；没命中就是 `("", [])`，渲染端零变化。

    🔴 命中了但 `tier` 缺失**不许静默跳过**（同 `_precursor_disclosure_block` 记过的那条）：
    退回时把**还剩下的分面**写在这儿，别把读者指回那个会被整段丢掉的披露块。
    """
    if not index:
        return "", []
    lines: list[str] = []
    done: set[int] = set()
    for side in sides:
        handle, entries = _precursor_side_entries(index, side, done)
        if not entries:
            continue
        seen: set[tuple] = set()
        cited: list[str] = []
        for e in entries:
            tier = (e.get("tier") or "").strip()
            note = (e.get("note") or "").strip()
            if not tier or (tier, note) in seen:
                continue
            seen.add((tier, note))
            cited.append(f"{tier} ({note})" if note else tier)
        if cited:
            lines.append(f"  ⚠️ **{handle}** is on a regulated-precursor list: "
                         + " · ".join(cited) + ".")
        else:
            # 🔴 CI-1099 review 抓到：这里原来写 "see the notice for the full statement"
            # ——指回的正是本票证明了会被整段丢掉的那个块 ⇒ 在「命中但 tier 缺失」这个
            # 子情形里，原来的失效形态原样复发。改成把**还剩下的分面**直接写在这儿；
            # 一个都不剩时才说「清单名没拿到」，并且不再把读者指走。
            residual = " / ".join(
                str(v) for v in (
                    entries[0].get("regime"), entries[0].get("authority")) if v)
            lines.append(
                f"  ⚠️ **{handle}** is on a regulated-precursor list"
                + (f" ({residual})" if residual else
                   " (the list name was not returned by the backend)")
                + ". Treat this as a flagged listing and verify against the cited regime.")
    if not lines:
        return "", []
    return _PRECURSOR_VERDICT_CLAUSE, lines


def _precursor_pair_facets(
    index: dict[str, list[dict]], *sides: tuple,
) -> list[dict]:
    """structuredContent 侧的同一件事：把限制挂到**这一对**上，不只顶层那个数组。

    只带分面（`chemical` / `tier` / `note` / `regime`），不复制 `statement`——成句的唯一
    出处仍是顶层 `precursor_disclosure`。两者在同一次渲染里从同一个数组推出来，推导不会漂。
    """
    out: list[dict] = []
    done: set[int] = set()
    for side in sides:
        label, entries = _precursor_side_entries(index, side, done)
        for e in entries:
            out.append({"chemical": label, "cas": e.get("cas"),
                        "regime": e.get("regime"), "tier": e.get("tier"),
                        "note": e.get("note")})
    return out


def _batch_pair_structured(pair: dict, index: dict[str, list[dict]]) -> dict:
    """batch 的结构化 pair：透传 + CI-1099 的 `precursor_restrictions`。

    单独拎成函数只为一件事——让两个工具的结构化面**共用同一段挂载逻辑**。此前它是
    列表推导里的一行 `_expose(...)`，于是「给 pair 挂东西」这件事在本文件里有了两处
    互不相干的写法，而漏掉的那处不会报错。
    """
    out = _expose(pair, rename={"chem1": "chemical_a", "chem2": "chemical_b"},
                  override={"traceability": pair.get("traceability", "rule_based")})
    facets = _precursor_pair_facets(index, *_pair_sides(pair))
    if facets:
        out["precursor_restrictions"] = facets
    return out


def _precursor_disclosure_block(data: dict) -> list[str]:
    """CI-553/CI-562: render the backend's regulated-precursor disclosure into **text**.

    CI-541 added a top-level `precursor_disclosure` key to /compatibility/check,
    /risk-warnings and /batch-safety. `_expose()` passes it through to
    structuredContent for free — but the model reads `TextContent`, and that is
    assembled field-by-field by each renderer, so a key nobody mentions simply
    never appears in the text面 for any of the three.

    ⚠️ Do NOT repeat the CI-553 ticket's claim that `batch_safety_check` "returns a
    bare string so it has no structuredContent" — measured false: it is annotated
    `-> str` but actually returns a CallToolResult carrying `_expose(data)`
    (`test_structured_passthrough.py` asserts exactly that). All three tools
    reach structured clients; what was missing everywhere is the text面.

    🔴 The disclosure is informational and the answer still stands — Blake's CI-541
    ruling was "answer AND disclose", never "refuse". The wording is rendered
    backend-side (`statement`, 5 languages, single source in the i18n catalog);
    do NOT re-phrase it here or a second, unversioned copy starts drifting.

    🔴 Batch truncation is why the header cannot promise "all of this was analysed":
    the backend computes the disclosure BEFORE its size gate (deliberate — a dropped
    chemical was still submitted and still audited). This tool's own limit matches
    that gate today, but the two are separate values, so a disclosed chemical can
    still be one that never entered the analysis if the gate is lowered. When
    `truncated` is set we say so instead of implying coverage.
    """
    entries = data.get("precursor_disclosure") or []
    if not entries:
        return []
    # 🔴 CI-1099：头句原来写 "informational only … its results follow below"。
    # 那句同时做了两件事：①保住「这不是拒答」（**必须保留**，见上面 CI-541 那条红线，
    # 也防 RAI 那类闸把它读成拒绝）②把这块标成 informational、并把读者指向下面的结论
    # ——②是我们自己签发的丢弃授权，而模型确实照办了。
    # ⇒ 保留①、去掉②，并明说这些登记项**限定**下面的结论。别把警告调硬（那是另一个
    # 失效方向，见 `precursor_disclosure.py` 的「分档语气」），也别加回「informational only」。
    # 限制条件本身现在还**另挂在每条判定行上**（`_precursor_pair_note`）——这块被整段
    # 丢掉时判定行仍带着它。
    lines = [
        "**⚠️ Regulated-precursor notice — not a refusal: the requested analysis was "
        "performed and is reported in full. These listings qualify it: a safety verdict "
        "is not clearance to supply, sell or transfer the chemical.**",
    ]

    # 🔴 A non-dict entry must not take down the whole safety answer: this block runs
    # before any result rendering, so an AttributeError here replaces the
    # compatibility/risk answer the user asked for with a tool error — strictly worse
    # than the missing disclosure being fixed. `_unresolved_block` guards the same way.
    bad = [e for e in entries if not isinstance(e, dict)]
    entries = [e for e in entries if isinstance(e, dict)]
    for e in bad:
        lines.append(f"- {e} (unrecognised disclosure entry — reported verbatim)")

    # Group by chemical. Everyday bulk chemicals sit on several control lists at once
    # (HCl: UN 1988 Table II + DEA List II + EU 273/2004 Cat 3), and those statements are
    # the same sentence with the list name swapped in — printed one per row they buried
    # the actual answer under seven near-identical paragraphs (measured live on Prod,
    # hydrochloric acid + acetone).
    #
    # 🔴 The safety property is the equality test below, NOT this grouping: a follow-up
    # statement is folded away ONLY if it equals the first one with its own tier
    # substituted in. Anything else is printed in full — acetone's EU 2019/1148 Annex II
    # entry (reportable suspicious transactions) survives that way, and so would two
    # tiers of one regime that differ on a threshold.
    #
    # Grouping by (chemical, regime) was tried first; a mutation run showed it changes
    # no output at all, because the equality test already refuses to fold anything that
    # reads differently. Keeping it would have been a knob that looks protective and
    # isn't — the kind of guard this repo keeps catching itself writing.
    groups: dict[tuple, list[dict]] = {}
    for e in entries:
        key = (e.get("cas") or e.get("query_name"),)
        groups.setdefault(key, []).append(e)

    for key, items in groups.items():
        head = items[0]
        name = head.get("query_name") or head.get("matched_name") or "(unnamed)"
        cas = head.get("cas")
        label = f"**{name}**" + (f" (CAS {cas})" if cas else "")
        statement = (head.get("statement") or "").strip()
        if not statement:
            # 🔴 Never drop an entry just because the rendered sentence is missing:
            # a hit with no `statement` is exactly when the reader most needs to be
            # told something was flagged. Fall back to the machine-readable facets
            # and say plainly that the wording did not arrive.
            facets = " / ".join(
                str(head[k]) for k in ("regime", "tier", "authority") if head.get(k))
            lines.append(
                f"- {label} — listed as a regulated precursor"
                + (f" ({facets})" if facets else "")
                + ". The disclosure text was not returned by the backend; treat this "
                  "as a flagged listing and verify against the cited regime."
            )
            rest = items[1:]
        else:
            lines.append(f"- {label} — {statement}")
            rest = items[1:]

        head_tier = head.get("tier") or ""
        same, different = [], []
        for e in rest:
            stmt = (e.get("statement") or "").strip()
            tier = e.get("tier") or ""
            if statement and stmt and head_tier and tier and \
                    stmt == statement.replace(head_tier, tier):
                same.append(e)
            else:
                different.append(e)
        if same:
            listed = " · ".join(
                (e.get("tier") or "?") + (f" ({e['authority']})" if e.get("authority") else "")
                for e in same)
            lines.append(f"  Also listed on: {listed} — same wording as above.")
        for e in different:
            stmt = (e.get("statement") or "").strip()
            if stmt:
                lines.append(f"- {label} — {stmt}")
            else:
                facets = " / ".join(
                    str(e[k]) for k in ("regime", "tier", "authority") if e.get(k))
                lines.append(
                    f"- {label} — listed as a regulated precursor"
                    + (f" ({facets})" if facets else "")
                    + ". The disclosure text was not returned by the backend; treat this "
                      "as a flagged listing and verify against the cited regime."
                )
    # 🔴 CI-570：截断提示**曾经写在这里**，于是它只在「这批里恰好有受管制前体」时才出现
    # ——一个与截断毫无关系的条件。没有前体命中的普通批次（绝大多数）全程沉默。
    # 现在它是 `_batch_truncation_block`，无条件渲染。别再往这里搬回来。
    lines.append("")
    return lines
