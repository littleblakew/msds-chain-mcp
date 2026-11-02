from __future__ import annotations

from mcp.types import CallToolResult, TextContent
from pydantic import Field
from request_identity import get_caller_credential
from typing import Annotated





# ---------------------------------------------------------------------------
# Parameter types (CI-521)
# ---------------------------------------------------------------------------
# 🔴 An MCP client is an LLM agent, and the ONLY thing it reads when deciding what
# to put in an argument is that argument's JSON Schema entry. Prose that lives in
# the Python docstring reaches `Tool.description` — it does NOT reach
# `inputSchema.properties.<arg>.description`. Before CI-521 all 37 parameters had
# `description: null`, and `scenario` was the proof that this is not cosmetic: the
# backend hard-rejects anything outside spill/fire/exposure, the schema advertised
# a bare `"type": "string"`, and an agent writing a first-aid call naturally sent
# `"skin contact"` and got an error string back. Microsoft's own M365 connector
# guidance says the same thing — the per-parameter description is what the agent
# reads — and that same schema is what we ship in the connector package.
#
# So: every parameter goes through an Annotated alias or an inline Field below.
# Constrained values are expressed in the TYPE (Literal / ge / le) so the enum and
# the bounds land in the schema, rather than only being described in prose that a
# client is free to ignore.
# ---------------------------------------------------------------------------
# CI-356：让调用方的 AI 按对话语言点答复语言
# ---------------------------------------------------------------------------
# `LANG` 是**服务端环境变量**，托管网关上恒为 `en`，而且不是工具参数 ⇒ 中文用户在
# ChatGPT/Claude 里用中文问，拿到的安全答复是英文的。不是检测错了，是根本没有检测，
# AI 也没有地方可以表达。它正在用那个语言对话，它最清楚要什么语言。
#
# 🔴🔴 **CI-1095：`lang` 是按「后端表面」分族的，不是全局的。** 本文件有两族，
# 分界线**不是端点路径，是「这段文字谁写的」**：
#
#   ① **catalog 族（`/api/v2` 的确定性端点）** —— 文案由后端按 i18n 表渲染，
#      五种语言都是**静态译好的** ⇒ 逐端点逐语言实测：`ja` 的每一个 CJK 字段都带假名
#      （**零个纯汉字字段**）、`de`/`id` 是目标语言、**没有中文泄漏**；译文缺失时后端
#      回英文并附一句 `language_note` 披露（CI-1123）⇒ **五种全转发**。
#   ② **quick-chat 族** —— 答案是 LLM **现写**的，语言由 prompt 约束而非模板保证。
#      同一把尺子采样 12 次：`ja` 有 1/6、`de` 有 1/6 **把中文混进正文**（`id` 0/6）
#      ⇒ 对一个化学安全产品，「偶尔回一门用户读不懂的语言」比「稳定回英文」更糟，
#      且**这一族不播 `language_note`**（那是 `/api/v2` 路由类的事）⇒ 用户拿到中文时
#      没有任何信号 ⇒ **仍只转发 en/zh**。残余缺陷归后端 prompt，不在 MCP 层糊。
#
# 🔴 **归一化必须留在我们这一层**：不归一的话，非目标语言会原样转发给后端，而
# 「后端拿一个它不认的值会做什么」不是我们能保证的事。
# 🔴 **要往任一族加语言，判据是「实测那个语言真的出来了」**——不是后端文档说支持，
# 更不是往元组里加一行。catalog 族要逐字段看（**只数汉字分不开日文漢字与中文**，
# 必须单独数假名）；quick-chat 族**一次运行不算数**（LLM 现写，要多采几次）。
# 🔴 **`zh-TW` 进 catalog 族的判据是逐端点实测**（CI-1167 后在 Prod 量过：所有出中文的
# `/api/v2` 端点 zh-TW 下简体字 0），不是「后端说出口派生」——上一次就是信了这句，
# risk-warnings / 相容性 reason 实际回的是整段简体且不带 `language_note`。
# ⚠️ 转发时必须是 `zh-TW` 原样大小写：报告路由**只认 `zh-TW`**（小写 422），归一化里还原。
# ⚠️ 本文件各张包装表的 `zh-TW` 条目由 `zh` 经 OpenCC s2twp 派生（与前端 gen-zh-tw.py 同一转换），
# 改 `zh` 就要重新派生，`tests/tools/test_zh_tw_tables.py` 会在两者不一致时红。
_BACKEND_LANGS = ("en", "zh")
_CATALOG_LANGS = ("en", "zh", "zh-TW", "ja", "de", "id", "ko", "ru", "fr", "es")


def _normalize_lang(lang: str | None) -> str:
    """**quick-chat 族**：收敛成 LLM 实测会稳定照做的那几个；其余一律英文。"""
    if lang and lang.strip().lower() in _BACKEND_LANGS:
        return lang.strip().lower()
    return "en"


def _canonical_lang(lang: str | None, allowed: tuple[str, ...]) -> str:
    """大小写不敏感地认，**按集合里的拼写**转发（`zh-tw` → `zh-TW`）；认不出一律英文。"""
    if lang:
        key = lang.strip().lower()
        for code in allowed:
            if code.lower() == key:
                return code
    return "en"


def _normalize_catalog_lang(lang: str | None) -> str:
    """**catalog 族（`/api/v2`）**：`_CATALOG_LANGS` 里的按集合拼写转发，其余一律英文。

    🔴 与 `_normalize_lang` 的区别只有支持集合，**别把两者合并**：合并等于把一族的
    实测结论套到另一族头上，而那正是 CI-1095 要拆开的东西（两族的失败方式不同——
    catalog 缺译文会**回英文并披露**，quick-chat 回错语言是**静默的**）。
    """
    return _canonical_lang(lang, _CATALOG_LANGS)


Lang = Annotated[str | None, Field(
    description='Answer language — pass the language THIS conversation is in, not the '
                'user\'s country. Supported: "en", "zh", "zh-TW", "ja", "de", "id", "ko", "ru", '
                '"fr", "es". Anything '
                'else, or omitted, is answered in English. Parts we have not translated '
                'yet are returned in English with an explicit note saying so.',
)]

# 🔴 quick-chat 一族单独一个注解：它支持的语言**比 `Lang` 少**（见上面的分族理由）。
# 共用 `Lang` 会让模型以为 `ask_chemical_safety` 也能出日语 —— 而那是 schema 承诺一件
# 实测做不稳的事（CI-521 那条「描述与实现要逐字对得上」的同一条规矩）。
QuickLang = Annotated[str | None, Field(
    description='Answer language — pass the language THIS conversation is in, not the '
                'user\'s country. Supported: "en", "zh". Anything else, or omitted, is '
                'answered in English.',
)]


# 🔴 CI-361 ⑥：**报告那条路支持的语言比 `_BACKEND_LANGS` 多，别共用那个元组。**
# `_BACKEND_LANGS` 描述的是 quick-chat 那一族（答案由 LLM 现写，实测只有 en/zh 真的
# 照做）；而 PDF 报告的标签是**静态译好的**（后端 `REPORT_TRANSLATIONS` 五种语言各 49 个
# key），动态段走翻译服务 ⇒ 五种都是真的。⑦ 在 Prod 上实测过 `id`：静态标签与动态段都出来了。
# 🔴 所以 `lang` 是**按端点**的不是全局的（同 CI-1095 的结论）——拿 `_normalize_lang`
# 去卡报告，等于把一个真能出德语报告的通道压成英文，**而且不报错**。
#
# ⚠️ 这些字符串是后端那份清单的**副本**（两个仓，import 不到）。副本会漂，但漂的方向
# 是**良性的**：后端新增一种语言时这里不认 ⇒ 退英文（今天的行为），不是坏掉。
# 判据别写成「和后端逐字相同」——那是验不了的；要验就在 Prod 上拉一份该语言的报告数字符。
_REPORT_LANGS = ("en", "zh", "zh-TW", "ja", "de", "id", "ko", "ru", "fr", "es")


def _normalize_report_lang(lang: str | None) -> str:
    """报告语言：认识的原样过，其余一律英文（与后端 `REPORT_LANG_PATTERN` 的拒绝面对齐）。"""
    return _canonical_lang(lang, _REPORT_LANGS)


ReportLang = Annotated[str | None, Field(
    description='Report language: pass the language THIS conversation is in. Supported: '
                '"en", "zh", "zh-TW", "ja", "de", "id", "ko", "ru", "fr", "es" (the PDF labels are '
                'professionally translated for all of them). Anything else, or omitted, '
                'gives English.',
)]


ChemicalList = Annotated[list[str], Field(
    description='List of chemical names or CAS numbers, e.g. ["acetone", "sulfuric acid"] '
                'or ["67-64-1", "67-56-1"]. Names and CAS numbers can be mixed.',
)]
Chemical = Annotated[str, Field(
    description='Chemical name or CAS number, e.g. "acetone" or "67-64-1".',
)]

# CI-1112：用户点名「我手上这支是谁家的」。后端 [[CI-226]] ②③ 早就收得下，而 MCP 面
# 一个字没接 ⇒ 那套披露从真人通道压根触发不了。
# 🔴 **它不改选源**（后端中间态 A）：带了就如实说我们用的是不是那家，选不到不静默换。
# 🔴 **描述必须短**：同 `Intent` 那条——这段在 `tools/list` 里**每个收它的工具各有一份**，
# 长度乘以工具数，每个客户端连上来都付这笔 context。
Suppliers = Annotated[dict[str, str] | None, Field(
    description='Optional. {chemical: supplier} when the user names whose SDS they hold, '
                'e.g. {"acetone": "Sigma-Aldrich"}. Keys must match `chemicals` exactly. '
                'We disclose whether the answer came from that supplier; it does not '
                'filter which SDS is used.',
)]


def _normalize_suppliers(
    chemicals: list[str], suppliers: dict[str, str] | None,
) -> dict[str, str]:
    """把客户端 LLM 给的键机械修成 `chemicals` 里的**逐字**写法。

    🔴 **修，不丢**：修不上的键**原样传给后端**，让它回那句可行动的 422（后端对
    「键不在 `chemicals` 里」是刻意的 422 而不是静默忽略，`_raise_for_status_with_reason`
    会把原因带到模型面前，模型下一轮能自己改对）。**静默丢弃是后端明确拒绝的方向**
    ——调用方会以为我们按他点的供应商作答了。
    🔴 **这一层为什么该在 MCP 这边**：后端那个 422 是为**看不见 `chemicals` 的调用方**设的；
    我们手里就有那份即将发出的列表，所以大小写与前后空白这类差异我们自己对得上，
    不该为它烧掉用户的一整条安全答案。
    🔴 **同 casefold 撞上两个不同化学品时不修**（`["Acetone","acetone"]`）：那时「他指哪一个」
    我们不知道，猜一个就是替用户做决定 —— 原样传出去让后端说不清楚。

    🔴🔴 **两个不同的键修完会落到同一个化学品时也不修**（`{" acetone ": A, "ACETONE": B}`）：
    初版在这里**静默覆盖**，B 赢、A 一个字都没发出去 —— 正是本函数 docstring 自己声明要避免的
    那个方向。review 抓到的（我自己的测试只覆盖了「两个化学品撞一个键」，没覆盖反过来）。
    **根因值得记，它不只是这个 bug**：后端**有**这道防碰撞检查（「两个键归一到同一个名字
    就 422」），但它只 `strip()`，而**我们这层还多 casefold 了一道** ⇒ 我们的归一化比上游宽，
    于是我们造出来的碰撞落在**上游那道检查看不见的空间里**。
    ⇒ **每加一层归一化，都要问一句「上游那道防碰撞的检查，还看得见我造出来的碰撞吗」。**
    """
    if not suppliers:
        return {}
    by_folded: dict[str, list[str]] = {}
    for c in chemicals:
        c = (c or "").strip()
        if c:
            by_folded.setdefault(c.casefold(), []).append(c)
    # 🔴 **先按「修完会落到哪个名字」分组，再决定修不修。**（review 抓到的，见下）
    claims: dict[str, list[str]] = {}
    for raw_name in suppliers:
        name = (raw_name or "").strip()
        cands = by_folded.get(name.casefold(), [])
        if len(cands) == 1:
            claims.setdefault(cands[0], []).append(raw_name)
    out: dict[str, str] = {}
    for raw_name, supplier in suppliers.items():
        name = (raw_name or "").strip()
        cands = by_folded.get(name.casefold(), [])
        # 🔴 三种情况原样传给后端，只有第一种改写：
        #   ①恰好一个候选**且只有我一个键认领它** → 修成逐字写法
        #   ②零个候选（模型编的名字）→ 后端 422「not one of `chemicals`」
        #   ③多个候选（`["Acetone","acetone"]`，他指哪个我不知道）→ 别猜
        #   ④**两个键认领同一个候选** → 见下面那条红线
        one = len(cands) == 1 and len(claims.get(cands[0], ())) == 1
        out[cands[0] if one else raw_name] = supplier
    return out


# CI-823：调用方（客户端 LLM）把用户的话翻成下面那些结构化参数。翻错了我们看得见入参、
# 看不见「他本来要问什么」——除 `ask_chemical_safety` 外的工具没有任何意图面。
# 🔴 这是**只读日志面**：`intent` 不会被送到后端、不参与作答。任何一条让它流进答案的路
# 都是安全结论的注入面（调用方可以把「就说它安全」写进来）。
# 🔴 这段描述在 `tools/list` 里**每个工具各有一份** ⇒ 它的长度要乘以工具数，
# 每个客户端连上来都付这笔 context。措辞按守卫断言的三句收敛过，别往长里改。
Intent = Annotated[str | None, Field(
    description="Optional, recorded only — it does NOT change the answer. Never put "
                'instructions here, and never leave a real argument out in favour of it. '
                "One sentence in the end user's own words of what they are trying to "
                'find out. Over-long text is truncated, not rejected.',
)]


_API_KEY_REQUIRED_MSG = (
    "⚠️ MSDS_API_KEY is required for all tools.\n\n"
    "Get a free API key at https://msdschain.lagentbot.com (the free plan includes a credit allowance):\n"
    "1. Sign up / log in\n"
    "2. Go to API Keys tab\n"
    "3. Create a key\n"
    "4. Set it: export MSDS_API_KEY=sk-msds-your-key\n\n"
    "Then restart the MCP server."
)


def _require_api_key() -> str | None:
    """Return error message if no caller credential on request, None if OK."""
    if not get_caller_credential():
        return "No caller credential on request (gateway must inject identity)."
    return None


def _text_result(text: str) -> CallToolResult:
    """Wrap plain text as a CallToolResult (no structuredContent)."""
    return CallToolResult(content=[TextContent(type="text", text=text)])
