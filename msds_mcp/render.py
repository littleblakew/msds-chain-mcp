from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from msds_mcp.app import logger
from msds_mcp.calls import _QUICK_INTERNAL_KEYS, _expose, _format_tool_results



def _chemicals_from_response(data: dict | None) -> list[str] | None:
    """CI-529：从**后端已经解析好的结构化字段**里取化学品名，回填调用日志。

    🔴 只读结构化字段，**绝不从 `answer` 正文里抽**。从自由文本反解析是 [[CI-527]] 的地盘
    且是已判定错误的路：正文里的名字可能是模型顺带提到的另一种物质，抽出来会让「按化学品
    聚合」从**缺**变成**错**，而错的看不出来。

    🔴 **键名照抄后端 `routers/quick_chat.py` 里那段同款提取**（它为 `build_sds_documents`
    做的是同一件事），不要自己发明：初版按想当然写了 `result["chemicals"]` 是字符串列表、
    相容性在 `result["pairs"]` 里——两个键后端**都不产出**（真实是 `chemicals` 为**匹配记录
    的 dict 列表** + `query` 是用户原词；相容性是**顶层** `chemical_a`/`chemical_b` 与
    `matrix[]`）。于是提取器在最常见的两条路径上恒空，而手写 fixture 的测试全绿——
    [[narrow-hand-rolled-fixtures-and-engine-specific-branches]] 的标准形状，review 抓到的。

    取不到返回 None（不是空列表）：**「没记」和「记了但是空的」在下游是两件事**。
    """
    if not isinstance(data, dict):
        return None
    # 🔴 这个函数在 `finally` 里跑：从这里抛出去会**同时**毁掉工具的返回值和整条调用日志
    # （异常发生在 `_log_intent` 之前 ⇒ slot 还是空的 ⇒ `_reported` 什么都不记，
    # 而调用方拿到的是 TypeError 而不是答案）。日志是尽力而为的东西，绝不许有这种代价。
    try:
        return _extract_chemicals(data)
    except Exception:  # noqa: BLE001 —— 见上：宁可少记一列，也不能吃掉答案
        logger.warning("CI-529 chemicals extraction failed", exc_info=True)
        return None


def _extract_chemicals(data: dict) -> list[str] | None:
    names: list[str] = []
    seen: set[str] = set()

    def _add(value) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            names.append(value)

    def _as_list(value):
        return value if isinstance(value, list) else []

    for doc in _as_list(data.get("documents")):
        if isinstance(doc, dict):
            # 🔴 `chemical`（调用方问的词）优先于 `chemical_name`（供应商 SDS 的产品标题，
            # 形如 "Acetone, ACS reagent, ≥99.5%"）——后者拿去 resolve_cas 可能解析成
            # 别的东西或解析不出来，而这一列的下游正是 CI-174 的报告范围。
            _add(doc.get("chemical") or doc.get("chemical_name"))

    for entry in _as_list(data.get("tool_results")):
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict):
            continue
        # search_chemical：`query` 是用户原词，`chemicals` 是**匹配记录**（dict）
        _add(result.get("query"))
        for hit in _as_list(result.get("chemicals")):
            if isinstance(hit, dict):
                _add(hit.get("name") or hit.get("chemical_name"))
        # risk / compliance / exposure / regulatory 等：顶层 chemical / chemical_name
        _add(result.get("chemical"))
        _add(result.get("chemical_name"))
        # check_compatibility：顶层 chemical_a / chemical_b
        _add(result.get("chemical_a"))
        _add(result.get("chemical_b"))
        # check_all_compatibility：matrix[]（不是 pairs）
        for pair in _as_list(result.get("matrix")):
            if isinstance(pair, dict):
                _add(pair.get("chemical_a") or pair.get("chem1"))
                _add(pair.get("chemical_b") or pair.get("chem2"))
        # generate_risk_warnings：warnings[].chemical
        # 🔴 CI-596：相容性派生的 warning 描述的是**两个**化学品，历史上把它们拼成
        # `"A+B"` 放进 `chemical`。这里收的名字会进 intent 日志的化学品列（需求语料
        # 的输入面）⇒ 拼接串会以一个不存在的化学品身份留痕。后端现在给结构化的
        # `chemicals`，有它就以它为准，拼接的那个只当显示文案、绝不当身份。
        # 🔴 分支打在 `kind` 上而不是「`chemicals` 非空」上：后者会让一条
        # kind=pair 但成员缺失的 warning 掉回 else，把拼接串重新当身份收进来
        # ——未知升级成乐观分支。pair 拿不到成员就一个都不收。
        for w in _as_list(result.get("warnings")):
            if not isinstance(w, dict):
                continue
            if w.get("kind") == "pair":
                for member in _as_list(w.get("chemicals")):
                    if isinstance(member, str):
                        _add(member)
                continue
            members = _as_list(w.get("chemicals"))
            if members:
                for member in members:
                    if isinstance(member, str):
                        _add(member)
                continue
            _add(w.get("chemical") or w.get("chemical_name"))

    # 🔴 名字里带逗号的丢掉：这一列在后端是 `",".join(names)` 存的（`mcp_log.py`），
    # 读回来按逗号切 ⇒ `N,N-dimethylformamide` 会变成 `N` + `N-dimethylformamide` 两条，
    # 而 CI-174 的报告范围正是从这里取的。丢掉比劈开好：缺一条是缺，劈开是**编造**。
    # （存储格式本身该修，那是 CI-344 那条线的事，不在本票范围。）
    names = [n for n in names if "," not in n]
    return names[:24] or None


def _quick_result(data: dict) -> CallToolResult:
    """Build a CallToolResult for quick_chat-backed tools.

    Preserves the human-readable answer as text content (for Claude and other
    clients) and exposes structuredContent (answer + raw tool_results) for
    clients that consume structured output (e.g. ChatGPT Apps SDK).

    CI-89: if the backend returns a top-level `documents` list (blob-backed SDS
    descriptors), append an "📄 Original SDS" section to the text and include
    the list in structuredContent.

    🔴 CI-592：structuredContent **从手抄白名单换成透传**（`_expose`，与 CI-342 给直连
    工具做的同一件事）。旧写法逐字段列了 3 个键，后端新增的字段**不会带上，也不会报错**
    ——客户端侧就是「这个字段不存在」。本票要送出去的 `unchecked` 正是这样一个新字段，
    而下一个新字段还会来。
    """
    answer = data.get("answer", "")
    tool_results = data.get("tool_results", [])
    documents = data.get("documents", [])
    # CI-89-followup: the SDS document links must come RIGHT AFTER the answer, before
    # the raw tool-data appendix. Appended last (after _format_tool_results' JSON blob)
    # the client model summarizes the answer and drops the trailing link — verified on
    # prod: backend returns documents correctly, but claude.ai never surfaced the link
    # for ask_chemical_safety while the (short, link-last) direct tools did.
    # 🔴 CI-841 的指令拼在 `unchecked` **之前** ⇒ 渲染出来在它上面，与后端两个确定性块
    # 的相对顺序一致（「这一问没查」比「这几个化学品没查」更靠近用户要的那件事）。
    # 🔴 CI-978 的指令拼在**最前**：它说的是「下面那段更正不许丢」，而它要保护的
    # 那段更正本身就在 `answer` 的开头 —— 指令离它越近，被重写时一起丢掉的概率越低
    # （同 CI-89-followup 实测的「靠后的内容会被客户端模型丢掉」）。
    text = (_attribution_override_directive(
                data.get("unchecked_intent_attribution_overridden"))
            + _unchecked_intents_directive(data.get("unchecked_intents"),
                                        data.get("intent"),
                                        data.get("unchecked_intents_prose_suppressed"))
            + _unchecked_directive(data.get("unchecked"))
            + answer + _format_sds_documents(documents)
            + _format_tool_results(tool_results))
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        # 🔴 `override` 那三个键是**已有契约**：老写法无论后端给没给都保证它们在
        # （`documents` 缺失时给 `[]`）。换成透传时若不补这一手，后端某次没带
        # `documents` 就会让读 `sc["documents"]` 的客户端 KeyError —— 全量测试当场
        # 抓到（`test_quick_result_ci89_no_documents`）。**新键不享受这个待遇**：
        # `unchecked` 缺失时就该缺失，那是「老后端」这个信息本身。
        structured_content=_expose(
            data, drop=_QUICK_INTERNAL_KEYS,
            override={"answer": answer, "tool_results": tool_results,
                      "documents": documents}),
    )


# 🔴 CI-592：这条通道上「散文」不是确定性的——`answer` 会被 claude.ai / Copilot 的
# 客户端模型**重写**之后才到用户眼前，而 CI-587 的原始事故正是从 MCP 的
# `validate_protocol_chemicals` 上观察到的。所以这里给的是**指令**，不是内容：
# 内容（用户可见的那段话）由后端确定性渲染在 `answer` 最前面，本行只负责让改写它的
# 那个模型知道这段不许丢、不许改写成「我们没有这些数据」。
#
# 形状照抄 [[CI-567]] 的 `[protocol]`：那次实测证明**光把正确内容放进载荷不够**，
# 模型仍会用通用内容覆盖物质级步骤，是「配对的显式禁令」把它扳回来的。
#
# 🔴 措辞红线与后端模板同源（`quick_engine._UNCHECKED_TEMPLATES`）：只说两件真话
# ——没被检查、怎样才能让它被检查。**不许出现「未收录 / 没有数据 / 建议上传」**，
# 那是断言一件我们根本没验证过的事（CI-243 / CI-322 / CI-334 三次同形事故）。
_UNCHECKED_DIRECTIVE = (
    "[unchecked] {n} chemical(s) were NOT looked up in this turn (per-question limit): "
    "{names}.\n"
    "You MUST relay this to the user BEFORE any safety conclusion, and you MUST NOT "
    "state or imply whether we do or do not have data/records for them — this turn did "
    "not look. Tell the user to ask about them separately. Do not drop this when "
    "summarizing.\n\n"
)


def _unchecked_directive(unchecked) -> str:
    """把后端的 `unchecked` 渲染成给客户端模型的显式指令。空/缺失 ⇒ 空串。

    🔴 **三态，这里只有两种渲染，是有意的**：`[]`＝后端说「没有未检查的」；`null`＝
    后端说「这一轮没算」（额度用尽、或 5 条走不到化学管线的早退路径）；键缺失＝老后端
    （本仓与后端各自发布，版本会错开）。后两者都渲染成空串——没有可靠名单时凭空说一句
    「可能有没查的」只会制造噪声。区分留在 structuredContent 里，客户端自己看得见。
    """
    if not isinstance(unchecked, list):
        return ""
    names = [str(n) for n in unchecked if isinstance(n, str) and n.strip()]
    if not names:
        return ""
    return _UNCHECKED_DIRECTIVE.format(n=len(names), names=", ".join(names))


# 🔴 CI-841：**为什么后端已经确定性拼了一段，这里还要再来一条指令。**
# 后端那段（`quick_engine._append_unchecked_intents`）防的是**后端自己的** summary LLM
# ——它确实防住了，Prod 实测 4/5 违反 prompt 侧禁令、确定性拼串才是承重层。但 MCP 这条
# 通道上还有**第三个模型**：claude.ai / ChatGPT / Copilot 的客户端模型会把 `answer`
# **重写**之后才送到用户眼前，而后端的确定性对它一个字都管不到。CI-592 正是为此
# 给 `unchecked` 加了配对指令（原始事故就是从 MCP 的 `validate_protocol_chemicals`
# 观察到的），CI-567 更早实测过「光把正确内容放进载荷不够」。
# ⇒ `unchecked` 在这条通道上有两层，`unchecked_intents` 此前只有一层。补齐的是那一层，
# 不是对称美学。
#
# 🔴 措辞红线同 `_UNCHECKED_DIRECTIVE`：只说两件真话——**这一问本轮没跑任何工具**、
# **请单独再问一次**。**不许出现「未收录 / 没有数据 / 建议上传 SDS」**：我们没查，
# 不等于我们没有（CI-243 / CI-322 / CI-334 三次同形事故）。
_UNCHECKED_INTENTS_DIRECTIVE = (
    "[unchecked-question] This question also asks about: {names}. NO tool was run for "
    "that part in this turn, so there is NO tool output for it.\n"
    "You MUST tell the user that part was not checked, BEFORE any conclusion about it. "
    "You MUST NOT present any statement about it as coming from the SDS, from a "
    "protocol, from the tool results, or from 'the data' — none of those were consulted "
    "for it. You MUST NOT state or imply whether we do or do not have data for it — "
    "this turn did not look. Tell the user to ask about that part separately. Do not "
    "drop this when summarizing.\n\n"
)


# 🔴 CI-869 review：与后端 `quick_engine._SKIP_INTENT_BLOCK_ON` **同一条策略的第二处拼写**。
# 后端在拒答回合（RAI 内容过滤）里**故意**把散文块跳过、而把结构化字段照常给全量，
# 它的注释写死了理由：「本块说的是『这一问没查，请单独再问一次』——而我们刚刚整条拒绝了
# 这次请求，等于邀请用户把我们刚拒掉的那半重新问一遍」，并明说两者分开是为了让
# **不经模型改写的消费者**仍拿得到事实。
# 🔴 **本仓这条指令恰恰把文本面变成了「经模型改写的消费者」** ⇒ 不加这道闸，
# 初版就是在拒答回合里叫模型请用户把刚被拒的那半再问一次。
# ⚠️ **别照抄 `_unchecked_directive` 的无条件形状**：化学品那半后端三条路径都拼
# （「别对这几个下任何结论」在拒答回合同样成立），intent 这半刻意只有两条 —— 不对称是有意的。
# 🔴 **CI-874：这里不再是主判据，是 fallback。** 后端现在直接把它的决定放进载荷
# （`unchecked_intents_prose_suppressed`，实调确认过），主路径消费那个字段
# ⇒ 后端改 `_SKIP_INTENT_BLOCK_ON` 时本仓自动跟上，不再是两处各拼一份的漂移源。
#
# 🔴 **契约①：字段缺席 ⇒ 回退到下面这个 frozenset，绝不默认「照发」。**
# 缺席是**常态不是罕见**——`run_quick` 有 6 条裸 `return` 路径不经过那个字段，
# 且本仓与后端各自发布、版本必然错开。默认照发＝在拒答回合里叫模型请用户把刚被拒的那半再问一次。
# 🔴 **契约②：这不是完全收敛，只是「主路径单源 + fallback」。** 只要 fallback 还在，
# 这个 frozenset 就仍是一份副本、仍会漂——只是漂的影响面从「全部回合」缩到「不带字段的那些回合」。
# **别把这段注释删掉当作已经收敛了。**
_SKIP_INTENT_DIRECTIVE_ON = frozenset({"grounded_only"})


def _unchecked_intents_directive(unchecked_intents, intent: str | None = None,
                                prose_suppressed: object = None) -> str:
    """把后端的 `unchecked_intents` 渲染成给客户端模型的显式指令。空/缺失 ⇒ 空串。

    三态与 `_unchecked_directive` 逐字相同（`[]`＝算过没有；`None`＝这一轮没算；
    键缺失＝老后端，本仓与后端各自发布、版本会错开），**两者都渲染成空串**，
    区分留在 structuredContent 里。不重复论证，见那个函数。

    🔴 **载荷里的 `unchecked_intents` 是内部标识符**（`first_aid_guidance` 这类），
    本地化只发生在后端构造 `answer` 的时候（`_render_unchecked_intents`）。所以这条指令
    念出来的是标识符：它面向的是**模型不是用户**，用户可见的措辞仍单源在后端的 `answer` 里。
    """
    # CI-874：后端说了算；它没说（字段缺席 / 不是 bool）才回退到本仓那份 frozenset。
    # 🔴 `is None` 不能写成 falsy 判断——`False` 是后端一个**有内容的回答**（「我没压制」），
    # 而 falsy 会把它和「没回答」揉成一个，正好抹掉这次改动的全部意义。
    suppressed = prose_suppressed if isinstance(prose_suppressed, bool) else None
    if suppressed is None:
        suppressed = intent in _SKIP_INTENT_DIRECTIVE_ON
    if suppressed:
        return ""
    if not isinstance(unchecked_intents, list):
        return ""
    names = [str(n) for n in unchecked_intents if isinstance(n, str) and n.strip()]
    if not names:
        return ""
    return _UNCHECKED_INTENTS_DIRECTIVE.format(names=", ".join(names))


# 🔴 CI-978：后端的出口检查刚刚在 `answer` 开头拼了一条**确定性更正**
# （「下面把 X 写成了来自 SDS 的样子，而本轮没为它跑过工具 ⇒ 那段没有出处」）。
# 本仓这条指令的唯一作用是**不让它在重写中消失**。
#
# 🔴 **为什么后端确定性拼了还不够**：这条通道上 `answer` 要过**第三个模型**
# （claude.ai / ChatGPT / Copilot 重写之后才到用户眼前），后端的确定性对它一个字
# 都管不到。同仓已有 prod 实证：CI-89-followup 里靠后的 SDS 链接被客户端模型丢掉。
# 先例与完整论证在 `_UNCHECKED_INTENTS_DIRECTIVE`（CI-592 / CI-567 / CI-869），不重复。
#
# 🔴 **只说「别丢」，不复述更正的内容**：更正里点了哪些意图的名，那是后端按 `lang`
# 本地化的用户可见措辞，单源在后端。本仓复述一份就是第二处拼写。
#
# 🔴 **不带 `_SKIP_INTENT_DIRECTIVE_ON` 那道闸**，与上面那条刻意不同：那道闸防的是
# 「在拒答回合里邀请用户把刚被拒的那半再问一次」，而本条一个字的邀请都没有——
# 它只说「上面那段更正不许丢」，在任何回合里都成立。
_ATTRIBUTION_OVERRIDE_DIRECTIVE = (
    "[attribution-correction] The answer below OPENS with a correction stating that part "
    "of it was written as if it came from the SDS / our data, while NO tool was run for "
    "that part in this turn.\n"
    "That correction is ours and it is authoritative. You MUST keep it, in full, ABOVE "
    "the content it corrects. You MUST NOT drop it when summarizing, MUST NOT soften it, "
    "and MUST NOT re-attribute the corrected content to the SDS, to the tool results, or "
    "to 'the data'.\n\n"
)


def _attribution_override_directive(overridden) -> str:
    """后端说「这一轮更正过一次」时，配一条「别把那条更正丢掉」的指令。

    🔴 **三态，与本文件其它指令同一条纪律**：`True` ⇒ 发；`False`（后端说没更正过）
    与键缺失（老后端 —— 本仓与后端各自发布、版本必然错开）都 ⇒ 空串。
    🔴 缺席时的默认必须是**不发**：凭空说一句「上面有更正」而 `answer` 里并没有，
    等于让客户端模型去找一段不存在的文字，那是我们自己制造的幻觉源。
    """
    return _ATTRIBUTION_OVERRIDE_DIRECTIVE if overridden is True else ""


# 后端没给 note 时的兜底文案（老后端、或将来新增的 reason）。键是机器可判的
# `document_unavailable_reason`；未知 reason 落到空串 ⇒ 只少一句解释，不会瞎猜原因。
_UNAVAILABLE_FALLBACK = {
    "daily_pdf_quota_reached": "we hold this SDS, but your daily download quota is used up",
    "insufficient_credits": "we hold this SDS, but your credit balance is insufficient to fetch it",
    "quota_check_failed": "we hold this SDS, but the download check is temporarily unavailable",
}


_REFERENCE_SHEET = "reference_sheet"


def _format_sds_documents(documents: list[dict]) -> str:
    """Render a `documents` list as an '📄 Original SDS' section.

    Each document: {chemical, chemical_name, cas, supplier, revision_date, region,
    record_id, sds_document_url}.  URL is output verbatim (no domain rewriting).
    Returns "" when documents is empty so callers can safely concatenate.
    """
    if not documents:
        return ""
    # `document_kind == "reference_sheet"`：库里这份原件是公开参考速查表（如州卫生部门的
    # 职业卫生资料），不是供应商 SDS。用户可能拿这里的文件去合规归档，所以标题与逐条都要照实说。
    all_reference = all(d.get("document_kind") == _REFERENCE_SHEET for d in documents)
    heading = ("**📄 Original reference document, not a supplier SDS (click to verify):**"
               if all_reference else "**📄 Original SDS (click to verify):**")
    lines = [f"\n\n---\n{heading}"]
    for doc in documents:
        chemical = doc.get("chemical_name") or doc.get("chemical") or "?"
        supplier = doc.get("supplier", "")
        revision = doc.get("revision_date", "")
        url = doc.get("sds_document_url", "")
        meta_parts = [p for p in [supplier, revision] if p]
        meta = " · ".join(meta_parts)
        entry = f"- {chemical}"
        if meta:
            entry += f" ({meta})"
        if doc.get("document_kind") == _REFERENCE_SHEET and not all_reference:
            entry += " [public reference fact sheet, not a supplier SDS]"
        if url:
            entry += f": {url}"
        else:
            # 🔴 CI-488：没有 URL **不等于**「我们没有这份 SDS」。
            #
            # 后端现在会在每日下载额度用尽（或配额子系统故障）时保留条目、去掉链接，
            # 并附上 `document_unavailable_reason` / `_note`。少了这一句，这里渲染出来
            # 的是「📄 Original SDS」标题下一行**没有链接也没有解释**的条目——人和模型
            # 都只会读成「这份文件不存在/坏了」，正是后端那两个字段要避免的歧义。
            # 后端建模好了而渲染层没接，本仓吃过的亏就叫「修了但没到达真正的消费者」。
            note = doc.get("document_unavailable_note") or _UNAVAILABLE_FALLBACK.get(
                doc.get("document_unavailable_reason") or "", ""
            )
            if note:
                entry += f" — {note}"
        # 🔴 CI-365：**这份文件不是本次结论所引用的那一份**时必须说出来。
        #
        # 后端现在让附件跟着「结论的依据」走；取不到被引用那行的原件时（罕见）
        # 它退回文档侧选择，并把 `document_follows_citation=False` +
        # `citation_divergence_note` 一起发下来。**那一句不渲染，降级就是静默的**——
        # 用户看到标题「📄 Original SDS (click to verify)」下的一个链接，会拿它去核对
        # 结论里那句「依据 X 家 SDS 第 7 节」，而它出自另一份文件。**比不给链接更坏**：
        # 不给链接只是缺一步，给错链接是替一份没参与结论的文件背书。
        # 这正是上面那条注释命名的「修了但没到达真正的消费者」，同一个函数里第二次。
        #
        # 🔴 三态，别写成 `if not doc.get("document_follows_citation")`：
        # `None`（这条回答没有引用，绝大多数）会被那个写法一起命中，于是每一条普通结果
        # 后面都挂一句「这不是依据」——把一句重要的警告变成噪声，人和模型都会学会忽略它。
        if doc.get("document_follows_citation") is False:
            divergence = doc.get("citation_divergence_note") or (
                "This file is not the SDS the conclusion cited; do not treat it as "
                "the basis for that conclusion."
            )
            entry += f" — ⚠️ {divergence}"
        lines.append(entry)
    return "\n".join(lines)


def _doc_link_lookup(documents: list[dict]) -> dict[str, str]:
    """Build {key -> sds_document_url} keyed by chemical, chemical_name and cas
    (all casefolded) so a per-item render can find its chemical's SDS link inline.

    CI-89-inline: a trailing '📄 Original SDS' block gets summarized away by the
    client model on long answers; an inline link ON each verdict/warning line
    survives because it is part of the structured row the model preserves.
    """
    lut: dict[str, str] = {}
    for doc in documents:
        url = doc.get("sds_document_url")
        if not url:
            continue
        for k in (doc.get("chemical"), doc.get("chemical_name"), doc.get("cas")):
            if k:
                lut.setdefault(str(k).casefold(), url)
    return lut


_LLM_ESCALATED_TAG = "[Basis: model reasoning, not an SDS statement]"
_UNKNOWN_BASIS_TAG = "[Basis: unverified origin, not an SDS statement]"


def _pair_basis_label(traceability: str | None) -> str:
    """相容性判定行的出处前缀（不含方括号）。

    🔴 只有 `sds_backed` 才许说「Source (SDS)」。模型推理（`llm_escalated`）和任何
    不认识的值都不能落到那个说法上：那等于替模型编一个 SDS 出处。
    """
    if traceability == "rule_based":
        return "Basis (rule)"
    if traceability == "sds_backed":
        return "Source (SDS)"
    if traceability == "llm_escalated":
        return "Basis (model reasoning, not an SDS statement)"
    return "Basis (unverified origin, not an SDS statement)"


def _traceability_label(traceability: str | None, chem_key: str | None,
                        sds_backed_chemicals: set) -> str:
    """把后端的 `traceability` 翻成给模型读的一句出处标注。

    🔴 **单一实现，三个格式化点共用。** 此前这段逻辑在 `server.py` 里**抄了三份**
    （risk-warnings / PPE / 综合报告），CI-336 只在其中一处发现缺陷——
    与 CI-277 记的「收口不止一处，判定链有三个入口会漏」是同一个形态：
    分头修会修三次补丁，而下一类错值来时还得有人记得再改三遍。

    🔴 **`none` 不等于「字段缺失」。** 它是 CI-243 引入、CI-365 沿用的值，含义是
    **什么依据都没有读到**（记录连危害数据都没有）。它一度掉进下面那个
    「这个化学品有没有 PDF」的推断分支，于是一条零危害数据的记录被盖上
    `[Source: SDS document]` —— **后端刚拿掉的虚假出处声明，被这一层原样加了回去**，
    而且是在模型直接消费的文本里。产品唯一的对外主张就是「答案可追溯到具体供应商 SDS」，
    这句话在这里是假的。

    推断分支只保留给**后端根本没给这个字段**的情况（老后端）。即便如此，它断言的也只是
    「这个化学品有文档」，不是「这条结论出自那份文档」——所以别把它扩大到任何已知值上。
    """
    if traceability == "sds_backed":
        return "[Source: SDS document]"
    if traceability == "rule_based":
        return "[Basis: rule/standard]"
    if traceability == "none":
        return ""
    if traceability == "llm_escalated":
        return _LLM_ESCALATED_TAG
    if traceability:
        # 后端发来了一个本版本不认识的值：它声明了出处类别，只是我们不知道是哪一类。
        # 既不能落到下面的「有文档就说有 SDS 出处」，也不能说成规则层。
        return _UNKNOWN_BASIS_TAG
    key = (chem_key or "").lower()
    return "[Source: SDS document]" if key and key in sds_backed_chemicals else ""


def _inline_sds(lookup: dict[str, str], *keys: str) -> str:
    """Return a compact inline SDS-link suffix for the first matching key, else ''."""
    for k in keys:
        if k and (url := lookup.get(str(k).casefold())):
            return f" 📄 SDS: {url}"
    return ""
