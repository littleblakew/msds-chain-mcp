from __future__ import annotations

import functools
import httpx
import inspect
import json
import json as _json
import time
import ui_compat_card
import uuid
from contextvars import ContextVar
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from request_identity import caller_headers, get_caller_credential, get_mcp_call_id, reset_mcp_call_id, set_mcp_call_id

from msds_mcp.app import (API_URL, LANG, TIMEOUT_LLM, _UPGRADE_ORIGIN, _UPGRADE_URL, _log_secret_header, logger)
from msds_mcp.params import (_normalize_catalog_lang, _normalize_lang, _normalize_suppliers, _require_api_key)



def _headers() -> dict[str, str]:
    return caller_headers()


# CI-55: the direct/v2 tools call fast no-LLM endpoints on a 15s client timeout.
# Backend tail-latency (cold start right after a deploy, load spikes) can still
# overrun it → httpx.ReadTimeout, which stringifies to "" → the opaque
# `Error executing tool <name>: ` dead end. Unlike the quick-chat path (missing
# data → upload the MSDS), a direct-tool timeout is transient service slowness, so
# the graceful answer is retry-oriented. Applied as a wrapper so all direct tools
# share one behavior. NEVER assert safety here.
# 🔴 CI-915：这句话**不许猜成因**。它一度写着「常见于刚部署后」，而被观察到的那次超时
# 与任何一次部署都不相邻 —— 成因是那一次查询本身很重。
# 误导性诊断比没有诊断更贵：它让用户重试同一个必然再超时的查询，也让排查的人先去翻
# 空无一物的部署记录。⇒ 只说**观察到的事实**（超时了）与**可行动的两条路**（重试 / 拆小），
# 别把负载问题说成部署抖动。
_DIRECT_TIMEOUT_MSG = {
    "en": "This safety check timed out. Please try again in a moment.",
    "zh": "本次安全检查超时。请稍候重试。",
    "zh-TW": '本次安全檢查超時。請稍候重試。',
    "ja": "この安全チェックはタイムアウトしました。少し待ってから再度お試しください。",
    "de": "Diese Sicherheitsprüfung hat das Zeitlimit überschritten. Bitte versuchen Sie es gleich erneut.",
    "id": "Pemeriksaan keselamatan ini melebihi batas waktu. Silakan coba lagi sebentar.",
    'ko': '이 안전 점검의 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.',
    'ru': 'Время ожидания этой проверки безопасности истекло. Повторите попытку чуть позже.',
    'fr': 'Cette vérification de sécurité a expiré. Veuillez réessayer dans un instant.',
    'es': 'Esta verificación de seguridad superó el tiempo de espera. Vuelva a intentarlo en un momento.',
}

# 🔴 第二条也是 CI-915（PR #50 第二轮 review 抓到）：「拆成更少的化学品」只对**收列表的那些工具**
# 成立。17 个被 `@_graceful_timeout` 包住的工具里有 **8 个根本不收 `chemicals`**
# （`get_emergency_response` / `get_sds_document` / `get_audit_report` …）——对它们说这句话，
# 就是 CI-915 要消灭的那种「听起来可行动、实际不适用」的建议，只是换了个地方犯。
# 🔴 **订正过一次**：第一版这句写的是「large batches are the usual cause」——
# **那仍然是在猜**，证据只有一个数据点，而同样的入参重跑一次**正常返回**了
# ⇒ 批量与超时**相关但不决定**。
# 「usual cause」这种因果断言配不上 n=1，而它正是 CI-915 要消灭的那类东西（只是轻一档）。
# ⇒ 只留**可行动的条件建议**（「包含很多化学品的话可以拆小再试」），不说成因。
# ⇒ 按**被包函数的签名**推导用哪一句，**不手写名单**：名单会腐化，签名不会
# （新加一个收列表的工具自动拿到批量那句；守卫按同一条规则全量扫，见 tests/transport/test_timeout_is_error.py）。
_DIRECT_TIMEOUT_HINT_BATCH = {
    "en": " If the request covered many chemicals, try splitting it into smaller calls.",
    "zh": "若这次查询包含很多化学品，可以拆成更小的几次再试。",
    "zh-TW": '若這次查詢包含很多化學品，可以拆成更小的幾次再試。',
    "ja": "多くの化学品をまとめて問い合わせた場合は、小分けにして再度お試しください。",
    "de": " Wenn die Anfrage viele Chemikalien umfasste, teilen Sie sie in kleinere Aufrufe auf.",
    "id": " Jika permintaan mencakup banyak bahan kimia, coba pecah menjadi panggilan lebih kecil.",
    'ko': ' 요청에 화학물질이 많이 포함되었다면 더 작은 호출로 나누어 다시 시도해 보세요.',
    'ru': ' Если запрос охватывал много веществ, попробуйте разбить его на несколько меньших вызовов.',
    'fr': (' Si la demande portait sur de nombreux produits chimiques, essayez de la diviser en appels plus '
     'petits.'),
    'es': ' Si la solicitud incluía muchos productos químicos, intente dividirla en llamadas más pequeñas.',
}


def _timeout_message(lang: str, *, batch: bool) -> str:
    """超时话术＝通用那句（+ 只对收列表的工具附加的批量提示）。"""
    base = _DIRECT_TIMEOUT_MSG.get(lang, _DIRECT_TIMEOUT_MSG["en"])
    if not batch:
        return base
    return base + _DIRECT_TIMEOUT_HINT_BATCH.get(lang, _DIRECT_TIMEOUT_HINT_BATCH["en"])


def _graceful_timeout(fn):
    """Wrap a direct-tool coroutine so a client read-timeout surfaces as an **errored**
    tool result carrying an actionable message.

    CI-55 建这层是为了干掉不透明的空错误（`httpx.ReadTimeout` 字符串化成 `""`
    ⇒ 调用方只看到 `Error executing tool <name>: `）。**那个目标由「消息非空」达成，
    不需要把失败伪装成成功** —— 而初版是 `return` 那句话。

    🔴 CI-914：原来 `return` 那句话让 MCP 协议的 `isError` 位是 `False`，对调用方就是
    「这次调用成功了，下面是结果」，而结果是一句「超时了请重试」。**消费它的是模型**，
    最可能把这句话当正常答案往下用，不走重试。

    🔴 **修法是返回 `CallToolResult(is_error=True)`，不是 `raise`** —— 这一步是 PR #50 的
    review 换来的，值得逐字记下：**工具体里抛出去的异常会被 `Tool.run()` 包成
    `ToolError(f"Error executing tool {name}: {e}")`**（`mcp/server/mcpserver/tools/base.py:181`），
    而那个前缀是**英文硬编码**的 —— 它会粘在五个语种每一句话前面，且正是 CI-55 当初要
    消灭的那种样板。「`raise` 与同仓 4xx 惯例一致」这个理由**听起来对、实际让文案更差**。
    ⇒ 两条性质要同时满足：**位是 `True`** 且 **文字逐字是 `_DIRECT_TIMEOUT_MSG` 那句**。
    🔴 **别改回 `return msg`（位会变假），也别改成 `raise`（文字会被加英文前缀）。**
    守卫 `tests/transport/test_timeout_is_error.py` 对这两个方向各有一条，断言是**逐字相等**
    不是子串 —— 子串断言正是当初没看见那个前缀的原因。
    """
    # 这个工具收不收化学品**列表**，决定要不要给「拆小一点」的建议。
    # 在装饰时算一次（`functools.wraps` 会让 `signature()` 穿透到真正的函数签名）。
    _takes_batch = bool({"chemicals", "ingredients"} & set(inspect.signature(fn).parameters))

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except httpx.TimeoutException:
            # 超时话术也跟调用方的语言走（`lang` 是关键字参数时才取得到；取不到就用服务端默认）
            # 归一化后再查表：`zh-tw` / 带空格的写法要命中 `zh-TW` 那条，认不出的落英文。
            lg = _normalize_catalog_lang(kwargs.get("lang") or LANG)
            msg = _timeout_message(lg, batch=_takes_batch)
            # 🔴 **不是 `raise`**：工具体里抛出去的异常会被 `Tool.run()` 包成
            # `ToolError(f"Error executing tool {name}: {e}")` —— 那个前缀是**英文硬编码的**，
            # 会粘在五个语种的每一句前面，而且正是 CI-55 要消灭的那种样板。
            # （review 抓到的；探针输出里其实一直印着它。）
            # ⇒ 直接返回一个 `is_error=True` 的结果：位是对的，文字逐字保留。
            return CallToolResult(content=[TextContent(type="text", text=msg)], is_error=True)
        except CreditsExhausted as e:
            # CI-1403：同上，返回而不是抛 —— 抛出去只剩一句文字，卡片通道（`_meta`）带不出去。
            return _credits_exhausted_result(e, fn.__name__)
    return wrapper


# Actionable fallback when quick-chat exceeds TIMEOUT_LLM. httpx.ReadTimeout
# stringifies to "", so re-raising surfaced `Error executing tool …: ` — an opaque
# dead end. On timeout, guide the user to the grounded path (retry / upload the SDS /
# give a CAS) instead of raising an empty error. NEVER assert safety here.
_TIMEOUT_ANSWER = {
    "zh": "安全助手响应超时，未能在限定时间内完成分析。请稍后重试。若这是未收录或专有产品，"
          "请上传其 MSDS/SDS PDF 或提供 CAS 号，以便直接查询其危害信息。",
    "zh-TW": '安全助手響應超時，未能在限定時間內完成分析。請稍後重試。若這是未收錄或專有產品，請上傳其 MSDS/SDS PDF 或提供 CAS 號，以便直接查詢其危害資訊。',
    "en": "The safety assistant timed out before completing its analysis. Please try again. "
          "If this is an unlisted or proprietary product, upload its MSDS/SDS PDF or provide a "
          "CAS number so its hazards can be looked up directly.",
    "ja": "安全アシスタントの応答がタイムアウトし、分析を完了できませんでした。もう一度お試しください。"
          "未登録または独自製品の場合は、MSDS/SDS PDF をアップロードするか CAS 番号をご提供ください。",
    "de": "Der Sicherheitsassistent hat vor Abschluss der Analyse das Zeitlimit überschritten. Bitte "
          "erneut versuchen. Bei einem nicht gelisteten oder proprietären Produkt laden Sie dessen "
          "MSDS/SDS-PDF hoch oder geben Sie eine CAS-Nummer an, um die Gefahren direkt nachzuschlagen.",
    "id": "Asisten keselamatan melebihi batas waktu sebelum menyelesaikan analisis. Silakan coba lagi. "
          "Jika ini produk tak terdaftar atau proprietary, unggah PDF MSDS/SDS-nya atau berikan nomor "
          "CAS agar bahayanya dapat dicari langsung.",
    'ko': ('안전 어시스턴트가 분석을 마치기 전에 시간이 초과되었습니다. 다시 시도해 주세요. 목록에 없거나 독점 제품이라면 해당 MSDS/SDS PDF를 업로드하거나 CAS 번호를 '
     '알려 주시면 유해성을 직접 조회할 수 있습니다.'),
    'ru': ('Ассистент по безопасности не успел завершить анализ за отведённое время. Повторите попытку. Если '
     'это незарегистрированный или фирменный продукт, загрузите его MSDS/SDS в PDF или укажите номер '
     'CAS, чтобы найти его опасности напрямую.'),
    'fr': ("L'assistant de sécurité a expiré avant de terminer son analyse. Veuillez réessayer. S'il s'agit "
     "d'un produit non répertorié ou propriétaire, téléversez sa FDS (MSDS/SDS) en PDF ou indiquez un "
     'numéro CAS pour que ses dangers puissent être recherchés directement.'),
    'es': ('El asistente de seguridad superó el tiempo de espera antes de completar su análisis. Vuelva a '
     'intentarlo. Si se trata de un producto no registrado o propietario, suba su MSDS/SDS en PDF o '
     'indique un número CAS para consultar sus peligros directamente.'),
}


async def _quick_chat(message: str, lang: str | None = None) -> dict:
    """POST /quick-chat and return the parsed response.

    On client read-timeout (a slow-but-valid backend turn that overran TIMEOUT_LLM)
    degrade to an actionable message rather than raising an opaque empty error.
    """
    if err := _require_api_key():
        raise ToolReason(err)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_LLM) as client:
            res = await client.post(
                f"{API_URL}/quick-chat",
                json={"message": message, "lang": _normalize_lang(lang or LANG)},
                headers=_headers(),
            )
            return _billed_json(res)
    except httpx.TimeoutException:
        # 与请求同一个归一化：请求按 quick-chat 族发出（zh-TW → en），超时话术不能比请求多出一种语言。
        return {"answer": _TIMEOUT_ANSWER.get(_normalize_lang(lang or LANG), _TIMEOUT_ANSWER["en"]),
                "tool_results": [],
                "_timed_out": True}


# 🔴 CI-595：这两个数是**量出来的**，别凭感觉调。Prod 上一份 4 化学品的相容性结果
# 10,227 字符；旧的 600 会让 6 对里只剩 2 对、且切在 JSON 中间，而被切掉的正是排在
# 后面的漂白剂+盐酸（氯气）· 漂白剂+氨水（氯胺）。4000 的依据：matrix 2,534（结论）
# + sources 1,509（供应商与版本日期，工具说明要求必须引用）+ 结构开销 ≈ 3,900，
# 派生的 warnings（6,061，与 matrix 重复）被丢掉并留记号。
# 总预算是防「某个工具返回一份病态大列表就把上下文撑爆」——按条目先到先得。
_RAW_ENTRY_BUDGET = 4000
_RAW_TOTAL_BUDGET = 8000


# 物质级规程行的行内标记，与后端 `app/services/protocol_marker.PROTOCOL_PREFIX` 同值
# （本仓是独立公开仓，不能 import 后端；`get_emergency_response` 渲染器按同一前缀认）。
_PROTOCOL_PREFIX = "[protocol]"
# 规程条目的骨架余量：化学品名 / 出处 / `_omitted_*` 记号缩到底后的量级（Prod HF 急救 ≈ 700）
_PROTOCOL_ENTRY_OVERHEAD = 800


def _is_protocol(obj) -> bool:
    return isinstance(obj, str) and obj.lstrip().startswith(_PROTOCOL_PREFIX)


def _protocol_size(obj) -> int:
    """`obj` 里全部规程行序列化后的字符数——`_format_tool_results` 按它给条目留底。"""
    if _is_protocol(obj):
        return len(json.dumps(obj, ensure_ascii=False)) + 2
    if isinstance(obj, dict):
        return sum(_protocol_size(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_protocol_size(v) for v in obj)
    return 0


def _drop_generic_beside_protocol(obj) -> tuple[object, int]:
    """混着规程行的列表里，只留规程行；返回 (新对象, 丢掉的条数)。

    🔴 CI-711：HF 的 `routes.skin` 是 5 条规程行 + 8 条通用 H 码话术。预算紧时两者
    同列表竞争，而按条目数轮流丢（第 ② 步）看不进嵌套列表，只会把整条途径丢掉——
    Prod 实测丢的正是用户问的 `skin`。规程行是对同一途径更具体的版本，所以先让通用的那几条让位。
    """
    if isinstance(obj, list):
        # 🔴 只丢「不含任何规程行」的条目：同列表里嵌着的 `{"route": "skin", "steps": [规程…]}`
        # 也是规程，不是通用话术（review 实测按 `_is_protocol` 判会把整条 skin 途径丢掉）。
        keep_only_protocol = any(_is_protocol(v) for v in obj)
        out, n = [], 0
        for v in obj:
            if keep_only_protocol and not _protocol_size(v):
                n += 1
                continue
            v2, k = _drop_generic_beside_protocol(v)
            out.append(v2)
            n += k
        return out, n
    if isinstance(obj, dict):
        out, n = {}, 0
        for k, v in obj.items():
            v2, c = _drop_generic_beside_protocol(v)
            out[k] = v2
            n += c
        return out, n
    return obj, 0


def _shorten_strings(obj, allowance: int, protocol_allowance: int | None = None):
    """把过长的字符串截短（保留键），供 `_compact_for_context` 用。

    只动**字符串值**，不动键、不动数值、不删字段——结论住在字段里（`verdict` /
    `level`），解释住在长字符串里（`reason`）。截短处留一个带**量级**的记号。

    🔴 CI-711：以 `_PROTOCOL_PREFIX` 开头的字符串是物质级急救规程行（HF「不痛也要就医」
    那类后半句警示），**最后才挨刀**，与后端 `tool_payload` 的 CI-574 同一规则：
    `protocol_allowance=None` 时原样放行，由调用方在通用文本已缩到底、仍放不下时才传值。
    后端的「最后」是排序不是豁免，这里同理——传了值它照样被缩，所以总预算仍受控。

    🔴 两个坑（review 实测抓到的）：
    ① 记号自己也占长度 ⇒ `allowance` 太小时「缩短」后的串**比原串还长**，
       于是逐级收紧的循环**不单调**，反而把载荷推进兜底截断。⇒ 下限钳住。
    ② 记号里的数字要是**真正丢掉的字符数**，不是「超出 allowance 的量」——
       两者差一个记号的长度，报少了会让人以为丢得比实际少。
    """
    if isinstance(obj, str):
        if obj.lstrip().startswith(_PROTOCOL_PREFIX):
            if protocol_allowance is None:
                return obj
            limit = protocol_allowance
        else:
            limit = allowance
        if len(obj) <= limit:
            return obj
        keep = max(limit - 20, 24)
        if keep >= len(obj):
            return obj
        return obj[:keep] + f"...(+{len(obj) - keep} chars)"
    if isinstance(obj, dict):
        return {k: _shorten_strings(v, allowance, protocol_allowance) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shorten_strings(v, allowance, protocol_allowance) for v in obj]
    return obj


def _with_markers_first(body: dict, omitted: dict) -> dict:
    """把 `_omitted_*` 记号排在**最前面**。

    🔴 review 实测：记号是新加的键，`json.dumps` 把它排在最后，于是兜底的字节截断
    **第一个砍掉的就是它** ⇒ 模型拿到一份空矩阵却看不到任何「有东西被删了」的提示，
    读起来就是「没有发现不相容」。丢弃重新变回静默——正是本函数存在的理由。
    """
    return {**omitted, **body}


def _compact_for_context(result, budget: int = _RAW_ENTRY_BUDGET) -> str:
    """把一条工具结果压进预算 —— **按结构压，不按字节切**。

    🔴 CI-595：这里原来是 `json.dumps(result)[:600]`。逐对的 `check_compatibility`
    结果各自远小于 600，所以每一对都活着；CI-589 之后快聊面改发**一份整份矩阵**
    （Prod 实测 **10,227 字符**）⇒ 附录被切在 JSON 中间，**6 对里只剩 2 对，而被切掉的
    正是排在后面的那几对**——漂白剂+盐酸（氯气）、漂白剂+氨水（氯胺）。而本文件另一处
    注释已写明「多数 MCP 客户端**只读 text**」⇒ 这条通道不是可有可无的补充。

    降级顺序（**每一步都可解释，不靠「哪个列表最大」这种启发式**）：
      ① 逐级缩短长字符串——解释可以短，结论不能没有
      ② 还放不下才丢条目，**在所有列表之间轮流丢**，并把 `_omitted_*` 记号排在最前
      ③ 最后才字节截断，并写明丢了多少

    🔴 **别再按「最大的那个列表」优先丢**（第一版就是这样，review 用 20 个化学品的
    批量结果打穿了）：那份载荷里最大的列表**恰好是 matrix 本身**（190 对，而 warnings
    只有 2 条）⇒ 190 对里 175 对被丢，**两条 incompatible 一条都没活下来**，因为它们
    排在尾部——正是本票说「绝不能被切掉」的那个位置。轮流丢不保证公平，但它保证
    **不会有某一条列表被单独清空**。
    """
    full = json.dumps(result, ensure_ascii=False)
    if len(full) <= budget:
        return full

    def _dump(obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

    if not isinstance(result, dict):
        keep = max(budget - 28, 1)
        return f"{full[:keep]}...(+{len(full) - keep} chars trimmed)"

    # ① 缩短字符串（保住全部字段与全部条目）
    work = result
    # 通用文本先缩到底（规程行原样），仍放不下才轮到规程行，它也走同一梯度（CI-711）
    ladder = (160, 100, 60, 40, 24)
    for allowance in ladder:
        work = _shorten_strings(result, allowance)
        if len(_dump(work)) <= budget:
            return _dump(work)

    # ①b 规程行旁边的通用话术先让位（CI-711），规程行仍原样
    omitted: dict = {}
    pruned, n_generic = _drop_generic_beside_protocol(work)
    if n_generic:
        work = pruned
        omitted["_omitted_generic_beside_protocol"] = n_generic
        if len(_dump(_with_markers_first(work, omitted))) <= budget:
            return _dump(_with_markers_first(work, omitted))

    # ② 轮流丢条目，记号在最前。🔴 CI-711：规程行原样参与这一步，**不先缩它**——
    # 旧顺序是「规程梯度缩到 24 → 再丢条目」，Prod 上 HF 的 16 条规程行全被切成
    # `[protocol] Move the pers...`，而丢条目本可以腾出位置。含规程行的条目先钉住不丢。
    body = {k: v for k, v in work.items()}

    def _too_big() -> bool:
        return len(_dump(_with_markers_first(body, omitted))) > budget

    def _drop_round_robin(pin: bool) -> None:
        lists = [k for k, v in body.items() if isinstance(v, list) and v]
        # 🔴 dict 值同样能吃掉整个预算（`sources` 在实测载荷里 1,509 字符，而它不是列表）
        # ——第一版只认列表，于是 matrix 被清空、结果**仍然**超预算、最后走字节截断，
        # 两条红线（结论不许丢 / 必须仍是合法 JSON）被同一份载荷同时打破。
        dicts = [k for k, v in body.items() if isinstance(v, dict) and v]
        guard = 0
        while _too_big() and (lists or dicts) and guard < 10000:
            guard += 1
            pool = [(k, len(body[k])) for k in lists if len(body[k]) > 0] + \
                   [(k, len(body[k])) for k in dicts if len(body[k]) > 0]
            if not pool:
                break
            # 轮流：每次从**当前条目数最多**的那个容器里砍掉一条尾部条目。
            # 🔴 用条目数而不是字节数选，才不会因为某个列表条目更长就被反复清空。
            key = max(pool, key=lambda kv: kv[1])[0]
            original = body[key]
            n = len(original)
            vals = original if isinstance(original, list) else list(original.values())
            # 🔴 钉住与否**每个容器算一次**，别放进二分里——review 实测放进去后
            # 3,000 条全钉住的列表要 0.96 秒（旧 0.04），而这是同步调用在事件循环里。
            pinned = [i for i, v in enumerate(vals) if _protocol_size(v)] if pin else []
            pinned_set = set(pinned)
            free = [i for i in range(n) if i not in pinned_set]

            # 按比例先砍一刀，别一条一条删——review 实测 3,000 条的列表逐条重序列化
            # 要 7.7 秒 CPU，而这是**同步**调用在事件循环里。
            # 🔴 **二分出放得下的最大条目数**，别按固定比例砍：砍 25% 会过度丢弃
            # （实测同一份 Prod 载荷，预算够放 6 对时只留下了 3 对），而逐条砍在 3,000 条
            # 的列表上要 7.7 秒 CPU——这是**同步**调用在事件循环里。二分两头都躲开。
            def _sample(keep_n):
                """跨步取样，**不是砍尾巴**。CI-589 的原始 bug 就是位置偏置；砍尾巴是
                同一个毛病换个位置——review 实测 20 个化学品的批量结果里两条
                `incompatible` 落在下标 150 / 188，砍尾巴让它们一条都没活下来。
                取样不能保证留下危险的那些（那需要语义），但保证幸存者**铺满整个列表**。
                钉住的条目（CI-711）总在，取样只在其余条目里做，钉住的条目数也算进 keep_n。"""
                keep_n = max(0, min(keep_n, n))
                if keep_n >= n:
                    return original
                m = max(keep_n - len(pinned), 0)
                if m >= len(free):
                    pick = free
                elif m <= 0:
                    pick = []
                elif m == 1:
                    pick = [free[0]]
                else:
                    pick = [free[min(round(i * (len(free) - 1) / (m - 1)), len(free) - 1)]
                            for i in range(m)]
                idx = sorted(pinned_set | set(pick))
                if isinstance(original, list):
                    return [original[i] for i in idx]
                keys = list(original)
                return {keys[i]: original[keys[i]] for i in idx}

            prior = omitted.get(f"_omitted_{key}", 0)
            lo, hi = 0, n - 1
            best = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                body[key] = _sample(mid)
                omitted[f"_omitted_{key}"] = prior + n - len(body[key])
                if len(_dump(_with_markers_first(body, omitted))) <= budget:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            body[key] = _sample(best)
            kept = len(body[key])
            omitted[f"_omitted_{key}"] = prior + n - kept
            if kept == n or kept == len(pinned):
                # 这个容器已经放得下了、或只剩钉住的条目，却仍然超预算 ⇒ 换下一个容器，别死循环
                lists = [k for k in lists if k != key]
                dicts = [k for k in dicts if k != key]

    _drop_round_robin(pin=True)

    # ②b 条目丢到底仍放不下，才轮到规程行走收紧梯度（排序不是豁免，CI-711）
    if _too_big():
        for protocol_allowance in (800, 400) + ladder:
            shrunk = _shorten_strings(body, ladder[-1], protocol_allowance)
            if len(_dump(_with_markers_first(shrunk, omitted))) <= budget:
                break
        body = shrunk

    # ②c 规程行缩到底仍放不下（例如 100 个化学品各带规程行的批量结果）⇒ 钉住的条目
    # 也得轮流丢。🔴 review 实测：没有这一步时直接落进字节截断——非法 JSON、尾部化学品
    # 整个消失，两条 CI-595 红线同时破；旧实现在同一份载荷上是合法 JSON、取样铺满。
    if _too_big():
        _drop_round_robin(pin=False)

    out = _dump(_with_markers_first(body, omitted))
    if len(out) > budget:
        # ③ 兜底必须**真的**兜住：不加这一步，`_RAW_TOTAL_BUDGET` 只是个愿望——
        # 调用方按返回值扣预算，而返回值可以任意大。代价是这一条不再是合法 JSON。
        keep = max(budget - 28, 1)
        return f"{out[:keep]}...(+{len(out) - keep} chars trimmed)"
    return out


def _format_tool_results(tool_results: list[dict]) -> str:
    """Render tool_results as compact structured text for context."""
    if not tool_results:
        return ""
    lines = ["\n\n---\n**Raw tool data:**"]
    remaining = _RAW_TOTAL_BUDGET
    left = len(tool_results)
    # 🔴 CI-711：带规程行的条目在均分份额之外另拿一笔**额外**额度（规程行总长 + 骨架余量，
    # 全部条目合计至多总预算一半），不从别的条目里抢——不带规程行的条目分到的与改前逐字相同。
    # Prod 实测 6 个条目均分时 HF 急救只分到 ~1,333，而它 16 条规程行就要 ~2,100；
    # 试过从总预算里预留，被挤掉的是隔壁 PPE 条目的供应商名（引用红线）⇒ 只能另给。
    extra_left = _RAW_TOTAL_BUDGET // 2
    for item in tool_results:
        tool = item.get("tool", "unknown")
        result = item.get("result", {})
        # 🔴 **按剩余条目均分**，不是先到先得。先到先得时前两个大结果就能吃掉总预算的
        # 一半以上，第三个（可能正是相容性结论）只剩几十字符 ⇒ **比旧的 `[:600]` 还惨**，
        # 而且「哪个工具活下来」取决于后端返回的顺序——一个与安全无关的变量。
        share = max(remaining // max(left, 1), 200)
        need = _protocol_size(result)
        extra = min(need + _PROTOCOL_ENTRY_OVERHEAD, extra_left) if need else 0
        cap = max(_RAW_ENTRY_BUDGET, need + _PROTOCOL_ENTRY_OVERHEAD) if need else _RAW_ENTRY_BUDGET
        rendered = _compact_for_context(result, min(cap, share + extra))
        used_extra = min(max(len(rendered) - share, 0), extra)
        extra_left -= used_extra
        remaining = max(remaining - (len(rendered) - used_extra), 0)
        left -= 1
        lines.append(f"\n`{tool}`: {rendered}")
    return "\n".join(lines)


def _error_text(e: BaseException) -> str:
    """Render an exception for storage/logging — never an empty string.

    CI-250: a large share of failed call-log rows carried no `error_message`
    at all. Root cause: several httpx exceptions (notably ReadTimeout /
    PoolTimeout and other httpx.TimeoutException subclasses) stringify to ""
    — `str(e) == ""` — and every tool's except-block did
    `error_msg = _error_text(e)` with no fallback. The durations on those rows
    sit exactly on the two client timeouts, which is what makes this a
    mechanism rather than a one-off. Always prefix with the exception's class
    name so rows stay groupable even when the message itself is empty.
    """
    msg = str(e).strip()
    label = type(e).__name__
    text = f"{label}: {msg}" if msg else f"{label}: (no message)"
    return text[:500]


# Process-local counter for dropped call-log POSTs (CI-248). Not persisted —
# it exists so a single `logger.warning` line carries a running rate, not just
# an isolated one-off, without standing up separate metrics infra for a
# network-isolated core that only ships stderr → Log Analytics.
_call_log_post_failures = 0


async def _log_call(tool_name: str, chemicals: list[str] | None, duration_ms: int,
                    success: bool, error_message: str | None = None,
                    input_params: str | None = None, response_text: str | None = None,
                    response_kind: str | None = None):
    """Fire-and-forget: POST call record to backend.

    Never raises into the caller — a logging failure must not break the user's
    tool call, so the POST is still wrapped in try/except and awaited without
    blocking the tool's own response path (this coroutine is only ever awaited
    from each tool's own `finally` block, after the result is already computed).

    CI-248: previously the except-block was a bare `except Exception: pass` —
    any backend hiccup (network blip, 5xx, auth resolution failure) dropped the
    call record with literally no trace anywhere, so a low call count could
    silently be an undercount with no way to know by how much. Now every drop
    is logged to stderr (Container Apps → Log Analytics for this
    network-isolated core) with enough context to correlate: which tool, the
    caller's credential presence, why it failed, and a running per-process
    count.
    """
    global _call_log_post_failures
    cred = get_caller_credential()
    # CI-113: strip "Bearer " prefix before logging so the backend's sk-msds-
    # prefix check resolves correctly. The gateway always forwards the resolved
    # sk-msds- key via X-API-Key (no Bearer), so this only fires for direct-to-
    # core callers that set Authorization instead of X-API-Key.
    if cred and cred.startswith("Bearer "):
        cred = cred[len("Bearer "):].strip()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.post(
                f"{API_URL}/mcp/call-log",
                json={
                    "tool_name": tool_name,
                    "chemicals": chemicals,
                    "duration_ms": duration_ms,
                    "success": success,
                    "error_message": error_message,
                    "input_params": input_params,
                    # CI-333：客户端真正读到的正文。后端在 CI-333/344 那版之前会静默
                    # 忽略这个字段（pydantic 默认 ignore extra），所以先发后存是安全的。
                    "response_text": response_text,
                    # CI-977：这一轮我们给了什么（answered / rejected / redirected）。
                    # 🔴 与 `success` 分开：一次 RAI 拒答在传输层是成功的 ⇒ 会被记成
                    # success=true、不进任何失败率，而它对用户就是一次没被回答的提问。
                    # 老后端会静默忽略这个字段（pydantic 默认 ignore extra）⇒ 先发后存安全。
                    "response_kind": response_kind,
                    # CI-548: the same id this tool call sent as `X-MCP-Call-Id`, so the
                    # backend's audit rows join back to this record. Older backends ignore it.
                    # Read from the context (set by `_reported`, reset only after this call)
                    # rather than passed in, so the test doubles' signatures stay as they are.
                    "mcp_call_id": get_mcp_call_id(),
                    "api_key": cred,
                },
                headers={**_headers(), **_log_secret_header()},
            )
            # Previously unchecked: a non-2xx response from the logging
            # endpoint itself (e.g. validation 4xx, backend 5xx) was silently
            # accepted as "logged" since no exception was raised without this.
            res.raise_for_status()
    except Exception as e:
        _call_log_post_failures += 1
        logger.warning(
            "mcp_call_log_post_failed tool=%s call_success=%s dur_ms=%s "
            "cred_present=%s reason=%s failures_this_process=%d",
            tool_name, success, duration_ms, bool(cred), _error_text(e),
            _call_log_post_failures,
        )



# ---------------------------------------------------------------------------
# CI-333：把「何时上报、上报多少」收进一个装饰器
# ---------------------------------------------------------------------------
# 要记的新东西是**回复正文**，而它在每个工具的 `finally` 里拿不到——返回值是在 `try` 里
# 直接 `return` 掉的（23 个工具共 48 个 return 点）。逐个改成 `result = …; return result`
# 的话，**漏掉任何一个 return，那条路径就永远没有正文，且不报错**。
#
# 所以反过来：工具在 `finally` 里只**声明要记什么**（`_log_intent`），由外层装饰器统一负责
# 计时、成败、取返回值、发 POST。装饰器拿到的是**函数真正返回的那个对象**，
# 结构上不可能漏掉某条 return 路径。
#
# 🔴 **声明的内容仍然由各工具自己算，装饰器不去 dump 入参**——那些手写的 dict
# **编码的是脱敏决定**：`upload_msds_pdf` 记的是 `<inline data URI, N chars>` 而不是那段
# base64（见该处注释：记录哪怕一个前缀都会把客户 SDS 内容写进 `mcp_call_logs`）。
# CI-344 之后 `input_params` 原文真的落库 ⇒ 这条脱敏从「防御性」变成「承重」，
# 自动 dump 入参会直接把客户文档字节写进日志表。
_log_slot: ContextVar[dict | None] = ContextVar("mcp_log_slot", default=None)


def _log_intent(tool_name: str, chemicals: list[str] | None,
                input_params: str | None = None, *,
                success: bool = True, error_message: str | None = None,
                response_kind: str | None = None) -> None:
    """工具声明：本次调用要记的身份 + **已脱敏的**入参（+ 可选的失败标记）。

    🔴 `success` 存在是因为**有些失败不抛异常**：quick-chat 超时被转成一句可读消息、
    `upload_msds_pdf` 把失败作为文本返回。装饰器只看得见「抛没抛」，看不见这一类
    ⇒ 工具必须能把它降级。初版漏了这个参数，基线比对当场抓到 `upload_msds_pdf`
    从 `success=False` 变成了 `True`——一个只在日志里、线上完全看不出来的退化。

    只能**降级**：装饰器那边取的是 `抛没抛 and 这里说的`，工具说不了「其实成功」。
    """
    _log_slot.set({"tool_name": tool_name, "chemicals": chemicals,
                   "input_params": input_params,
                   "success": success, "error_message": error_message,
                   "response_kind": response_kind})


# CI-823：上限只**截断不拒绝**。写成 schema 的 `maxLength` 会让 pydantic 直接打回整次调用
# ⇒ 一个纯诊断字段就能把一次真实的安全查询挡在门外，方向反了。
_MAX_INTENT_CHARS = 500


def _cap_intent(text: str) -> str:
    return text if len(text) <= _MAX_INTENT_CHARS else text[:_MAX_INTENT_CHARS] + "…[truncated]"


def _supplier_params(chemicals: list[str], suppliers: dict[str, str] | None) -> dict:
    """CI-226：调用日志里记归一化后的 `suppliers`（发给后端的那一份），没带就是空 dict。

    展开进 `_intent_params` 的手写 dict ⇒ 没带 supplier 时日志与之前逐字节相同。
    """
    cleaned = _normalize_suppliers(chemicals, suppliers)
    return {"suppliers": cleaned} if cleaned else {}


def _intent_params(payload: dict, intent: str | None) -> str:
    """把工具**手写的**脱敏 dict 序列化，调用方给了 `intent` 就多挂一个键（截断后）。

    🔴 「手写 dict 编码的是脱敏决定」这条约定不变（见 `_reported` 上方那段）——这里只
    多挂一个键，仍然不去自动 dump 入参。`intent` 缺省时输出与加这个参数之前**逐字节相同**。
    """
    if intent and intent.strip():
        payload = {**payload, "intent": _cap_intent(intent.strip())}
    return _json.dumps(payload)


# 发送侧的上限。后端也会截（`_clean_payload`），这里再挡一道是因为**这一段要走网络**：
# 一份 PDF 抽出来的正文可以很大，让它先跑一趟 HTTP 再被对面砍掉是白费带宽和延迟，
# 而这个 POST 挂在每个工具调用的关键路径后面。数值与后端保持一致，改一处要改两处。
_MAX_RESPONSE_LOG_CHARS = 20_000


def _response_text(result) -> str | None:
    """把工具的返回值压成一段文本——客户端真正读到的那一份。"""
    if result is None:
        return None
    if isinstance(result, str):
        return _cap(result)
    content = getattr(result, "content", None)
    if content:
        parts = [t for b in content if (t := getattr(b, "text", None))]
        if parts:
            return _cap(("\n".join(parts)))
    return None


def _cap(text: str) -> str:
    return text if len(text) <= _MAX_RESPONSE_LOG_CHARS else text[:_MAX_RESPONSE_LOG_CHARS] + "…[truncated]"


# CI-977：后端 quick-chat 的 `intent` → 我们要记的那条事实。
# 🔴 **判据从后端的载荷推导，不去猜回复正文**：拒答文案是 5 语种一张表，按文本认它
# 就是 [[testing-unreliability-seven-forms]] 里那条「自由文本判据」——加一门语言、
# 改一个字都会让它静默失配，而失配的方向是「看起来没有误伤」。
#
# 🔴 **三态**：`None` = 后端没给（老后端、或压根不经 quick-chat 的早退路径）⇒ 别填
# `answered` 兜底，那会把「不知道」写成一句肯定。
_RESPONSE_KIND_BY_INTENT = {"rejected": "rejected", "redirected": "redirected"}


def _response_kind(data: dict | None) -> str | None:
    """这一轮我们给了什么：`answered` / `rejected` / `redirected` / `None`（没算）。"""
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    if not isinstance(intent, str) or not intent:
        return None
    return _RESPONSE_KIND_BY_INTENT.get(intent, "answered")


def _reported(fn):
    """包在每个工具最内层：计时 / 成败 / 回复正文 / 上报，一处做完。

    🔴 必须在 `_graceful_timeout` **内层**（装饰器列表里写在它下面）：超时被转成一句
    可读消息之前，这里要先看到原始异常，否则超时会被记成 success=True——那正是
    CI-55 想看见的信号。
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        token = _log_slot.set(None)
        # CI-548：每次工具调用一个 id，本次调用里打后端的每个请求都带上（`caller_headers`），
        # 上报日志时同值写进 `mcp_call_id` ⇒ 后端的审计行能接回这次的回复正文。
        call_id = uuid.uuid4().hex
        call_token = set_mcp_call_id(call_id)
        t0 = time.monotonic()
        success, error_msg, result = True, None, None
        try:
            # CI-1133：在这里抬 `language_note` 是为了**成员自己发现**——每个工具都戴着
            # 这个装饰器，新工具不必记得补一行。理由与代价见 `_relay_language_note`。
            # 🔴 赋回 `result` 是必须的：`finally` 里 `_response_text(result)` 记的就该是
            # **我们真正发出去的那串文本**，否则调用日志与客户端看到的不是一回事。
            result = _relay_language_note(await fn(*args, **kwargs))
            return result
        except Exception as e:  # noqa: BLE001 — 记完再抛，行为不变
            success, error_msg = False, _error_text(e)
            raise
        finally:
            try:
                slot = _log_slot.get()
                if slot is not None:
                    # 只能降级：工具报的失败与「抛了异常」取与
                    ok = success and slot.get("success", True)
                    err = error_msg or slot.get("error_message")
                    await _log_call(
                        slot["tool_name"], slot["chemicals"],
                        int((time.monotonic() - t0) * 1000), ok, err,
                        slot["input_params"], _response_text(result),
                        # CI-977：工具没报就是 `None`（＝「这一轮没算」），**别在这里填
                        # `answered` 兜底**——那会把「不经 quick-chat 的工具」和「真的答了」
                        # 揉成一个桶，正是本票要拆开的那种同形。
                        # 🔴 **关键字传参**：位置传参会让每个测试替身的签名都成为隐式契约，
                        # 加一个参数就会一次性打翻一批不相干的守卫。
                        response_kind=slot.get("response_kind"),
                    )
            finally:
                # 上报被取消（CancelledError 落在 `await _log_call` 上）时也要复位，否则这个
                # task 之后再打后端，会带着这次调用的 id 和日志槽。
                _log_slot.reset(token)
                reset_mcp_call_id(call_token)
    return wrapper


# ---------------------------------------------------------------------------
# Direct service layer helpers (bypass LLM)
# ---------------------------------------------------------------------------

def _parse_usage(res: "httpx.Response") -> dict | None:
    """Read per-call credit usage the backend echoes via X-Msds-Credits-* headers.
    Returns {cost, balance, reason} or None when the call wasn't metered."""
    cost = res.headers.get("X-Msds-Credits-Cost")
    if cost is None:
        return None
    try:
        return {
            "cost": float(cost),
            # 🔴 CI-904：**缺 header 时是 `None`，不是 `-1`**。原来默认 `-1`，而 `_usage_line`
            # 把 `bal < 0` 当「订阅」哨兵 ⇒ 同一个取值编码了两件事：「订阅，没扣费」与
            # 「我不知道余额」。后者会被说成前者，于是一次真的扣了 10 credits 的调用
            # 逐字印出 `Included in your plan (no credits deducted).`
            # 🔴 说错的方向**对用户有利 ⇒ 不会被投诉发现**，只能靠守卫。
            "balance": (None if res.headers.get("X-Msds-Credits-Balance") is None
                        else float(res.headers["X-Msds-Credits-Balance"])),
            "reason": res.headers.get("X-Msds-Credits-Reason", ""),
        }
    except (TypeError, ValueError):
        return None


def _detail_text(res: "httpx.Response", limit: int = 400) -> str:
    """把后端 4xx 的 `detail` 压成一句可行动的话。

    FastAPI/pydantic 的 422 是 `{"detail": [{"loc": [...], "msg": "...", ...}, ...]}`，
    HTTPException 则是 `{"detail": "一句话"}`——两种都要认。

    🔴 解析失败时**不把原始响应打出来**（memory: `ps-leaks-credentials-from-command-lines`
    的同族——响应体可能带凭证或客户文档字节），只说「后端没给出原因」并附状态码。
    🔴 截断到 400（不是复用 `_cap` 的 20000——那是**日志**的预算）：这句话会进工具返回值，
    调用方读的是它，塞进去一整个响应体只会把真正的原因埋掉。
    """
    def _short(text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit] + "…"

    try:
        detail = res.json().get("detail")
    except Exception:  # noqa: BLE001 — 见上：不打印原始响应
        return f"backend gave no machine-readable reason (HTTP {res.status_code})"
    if isinstance(detail, str) and detail.strip():
        return _short(detail.strip())
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            # loc 形如 ["body", "chemicals"] ⇒ 只留字段名那截，"body" 对调用方没信息量
            loc = ".".join(str(x) for x in (item.get("loc") or []) if x != "body")
            msg = str(item.get("msg") or "").strip()
            parts.append(f"{loc}: {msg}" if loc and msg else (msg or loc))
        joined = "; ".join(p for p in parts if p)
        if joined:
            return _short(joined)
    return f"backend gave no machine-readable reason (HTTP {res.status_code})"


# 🔴 CI-868：鉴权失败必须变成**可行动的一句话 + 一条给模型的禁令**。
#
# 失败形状：core 打后端拿到 401，而这里当时直接 `raise_for_status()` ⇒ 调用方拿到一个
# 裸的 `HTTPStatusError`，正文里还带着我们后端的完整 URL。三个问题一次犯全：
#   ① **不可行动** —— 模型看不出这是「你的授权失效了」还是「我们坏了」，
#      于是它做了最糟的那件事：**转去用自身知识回答相容性**，末尾才轻描淡写一句免责。
#      ⇒ 光把失败暴露出来不够，**必须显式禁止它替我们回答**（同 `_UNCHECKED_DIRECTIVE` 的形状：
#      [[CI-567]] 实测过，光把正确内容放进载荷不够，是配对的禁令把模型扳回来的）。
#   ② **泄露内网地址** —— 那串完整的 Azure 后端 URL 直接进了第三方客户端的上下文。
#   ③ **401 与 403 被当成同一件事** —— 401 是「没认出你」（重连有用），
#      403 是「认出你了但不给」（重连没用，叫人去重连是浪费他时间）。
#
# 🔴 **措辞红线**：这几句话**绝不能被读成安全结论**。
# 失败方向很坏——「工具报错」到「所以大概没事」只有一步，而调用方常常正在问一个危险组合。
# 所以每句都显式写「这不是安全结论 / 别拿常识补」。
# 🔴 **也不许说「我们没有这个数据」** —— 我们根本没查成（[[CI-243]]/[[CI-322]]/[[CI-334]] 同形）。
# 🔴 **不猜原因**：401 至少三种成因（token 过期 / key 被吊销 / [[mcp-key-cap-leak]] 的 10-key
# 上限把它挤掉），后端对它们**返回同一个 401** ⇒ 这里只说「授权没被接受」，不替它选一个。
_AUTH_FAILED_MSG = {
    401: (
        "Not authorized: the backend did not accept this connection's credential (HTTP 401). "
        "This is an authentication failure on our side of the call — it is NOT a safety "
        "finding, NOT a result, and NOT a statement that we lack data for what you asked. "
        "Nothing was looked up. "
        "You MUST tell the user the request could not be run, and you MUST NOT answer the "
        "question from your own knowledge or infer that anything is safe or compatible. "
        "Remedy depends on how this server was reached: remote callers should reconnect the "
        "MSDS Chain connector (re-run the authorization flow); a self-hosted stdio caller "
        "should check the MSDS_API_KEY this server was started with. If neither helps, the "
        "account's API credential may have been revoked."
    ),
    403: (
        "Forbidden: this connection was recognised but is not permitted to make this call "
        "(HTTP 403). This is a permissions failure — it is NOT a safety finding, NOT a "
        "result, and NOT a statement that we lack data for what you asked. Nothing was "
        "looked up. You MUST tell the user the request could not be run, and you MUST NOT "
        "answer the question from your own knowledge or infer that anything is safe or "
        "compatible. Reconnecting will NOT help; the account needs access to this feature."
    ),
}


class ToolReason(ToolError, RuntimeError):
    """我们**故意**抛给调用方、且带可读原因的错误（422 的字段原因、401/403 的重连指引…）。

    🔴 为什么必须是 `ToolError` 的子类：
    SDK 的 `Tool.run()` 把异常分成两类 ——
    · `ToolError` ＝「工具故意抛的」⇒ 包成 `f"Error executing tool {name}: {exc}"`，**文案保留**
    · 其它 `Exception` ＝「崩溃」⇒ **mcp 2.2.0 起只回 `f"Error executing tool {name}"`，文案丢掉**
      （有意为之：别把服务端内部错误泄露给模型）
    我们原来抛裸 `RuntimeError`，在 mcp 2.0.0 上碰巧带着文案，**在 2.2.0 上就没了**
    ⇒ [[CI-410]] 的保证（422 的原因要到达调用方）静默失效，而依赖钉的是 `mcp>=2.0.0,<3`。
    🔴 **同时继承 `RuntimeError` 是有意的**：仓里既有大量 `pytest.raises(RuntimeError)`
    与调用方约定，换成纯 `ToolError` 会在一个与本问题无关的维度上制造破坏性改动。
    🔴 **别改回裸 `RuntimeError`** —— 那个回归在 2.0.0 上测不出来（那正是它这次溜进 main 的原因）。
    """


class CreditsExhausted(ToolReason):
    """402 from the backend: the check did not run because the wallet is empty.

    Carries where the user can upgrade, so `_graceful_timeout` can put it in the card
    channel (`_meta`), which the host renders without passing through the model. A host's
    model is free to rewrite an error sentence and drop its link, so the text alone cannot
    be relied on to get the user to the upgrade page.
    """

    def __init__(self, text: str, *, upgrade_url: str | None = None,
                 resets_at: str | None = None):
        super().__init__(text)
        self.upgrade_url = upgrade_url
        self.resets_at = resets_at

    def retold(self, text: str) -> "CreditsExhausted":
        """Same upgrade target under a longer sentence (a caller adding context keeps the card)."""
        return CreditsExhausted(text, upgrade_url=self.upgrade_url, resets_at=self.resets_at)


# Tools whose definition points at the card template. A card payload on any other tool's
# result would reach no template, so it is left off.
_CARD_TOOLS = frozenset({"check_chemical_compatibility"})


def _credits_exhausted_result(e: CreditsExhausted, tool_name: str) -> CallToolResult:
    """The errored result for an empty wallet: what happened and the link for the user, plus
    the card payload on tools that have the card template. No `structured_content`: that slot
    holds each tool's own data shape, and an error must not pose as one."""
    url = ui_compat_card.allowed_upgrade_link(e.upgrade_url, _UPGRADE_ORIGIN) or _UPGRADE_URL
    text = (f"{e}\n\nThis check was NOT run: there is no MSDS Chain result for it. "
            f"Give the user this link to upgrade or top up: {url}\n"
            "If you add general knowledge, label it as general knowledge, not as an MSDS Chain result.")
    meta = None
    if tool_name in _CARD_TOOLS:
        meta = {ui_compat_card.CARD_META_KEY: ui_compat_card.exhausted_payload(
            str(e), url, e.resets_at, _UPGRADE_ORIGIN)}
    return CallToolResult(content=[TextContent(type="text", text=text)], meta=meta, is_error=True)


def _rate_limited_message(res: "httpx.Response") -> str:
    """The sentence for a 429: how long to wait (from `Retry-After` when it is a number of
    seconds, else a short default) and to retry once rather than in a loop."""
    wait = 30
    try:
        raw = (getattr(res, "headers", None) or {}).get("retry-after") \
            or (getattr(res, "headers", None) or {}).get("Retry-After")
        if raw is not None:
            wait = max(1, min(int(float(str(raw).strip())), 3600))
    except (TypeError, ValueError):
        pass
    return (f"MSDS Chain is rate-limiting this connection (too many calls in a short time). "
            f"Wait about {wait} seconds, then retry this call once. Do not retry in a loop.")


def _raise_for_status_with_reason(res: "httpx.Response") -> None:
    """`raise_for_status()` 的替身：422 带上后端说的原因（CI-410）、401/403 变成可行动的话（CI-868）。

    🔴 存在的理由是**别的路径不走 `_billed_json`**：`_build_audit_session` 的三步与
    `upload_msds_pdf` 直接打 `/sessions*`，此前它们的 422 仍是裸状态行 —— 同一张票要修的
    同一种缺陷，只是在两个不路由到计费包装的工具上。review 抓到的完整性缺口。
    **CI-868 把 401/403 挂在同一个收口上，理由逐字相同。**

    ⚠️ 402 由 `_billed_json` 在调本函数**之前**特判 ⇒ 余额耗尽仍走它自己那句话，别在这里抢。
    """
    if res.status_code == 422:
        raise ToolReason(f"Request rejected (422): {_detail_text(res)}")
    if res.status_code == 429:
        # CI-1400：限流不是故障，也不是用户的错 ⇒ 告诉模型等多久、只重试一次。
        # 裸 `raise_for_status()` 会给出 `Client error '429 …' for url <后端地址>`：
        # 不可行动，还把后端地址交给了第三方客户端。
        raise ToolReason(_rate_limited_message(res))
    if msg := _AUTH_FAILED_MSG.get(res.status_code):
        # 🔴 **不把 detail 拼进给模型那句话**：后端 401 的 detail 是内部代号
        # （`password_auth_disabled` 这类），对第三方客户端既无信息量又是内部实现的泄露。
        #
        # 🔴 **但必须记进我们自己的日志——review 抓到初版这里是一句假话。**
        # 初版的注释写着「排障要的信息在 `mcp_call_logs.error_message` 里」，而实际上：
        # ①这里既不记也不留，RuntimeError 只带固定样板；②`_error_text` 把 `str(e)` 截到 500,
        # 而 401 那段样板本身就 593 字符 ⇒ 那一列存的是**被截断的禁令、零后端信号**。
        # 更糟的是方向：[[CI-868]] 的根因至今未定，而能把「token 过期 / key 吊销 /
        # 10-key 上限挤掉」分开的**唯一线索就是这个 `detail`** —— 初版把它在唯一看得见它的
        # 地方丢掉了。**这是「注释描述意图、代码做另一件事」的又一例。**
        # ⚠️ 记的是 `_detail_text` 抽出来的 `detail`，**不是原始响应体**
        # （[[ps-leaks-credentials-from-command-lines]] 的同族红线仍然适用）。
        logger.warning("auth_failed status=%s detail=%s",
                       res.status_code, _detail_text(res))
        raise ToolReason(msg)
    res.raise_for_status()


# 🔴 CI-937：后端**故意**多给的一格，MCP 面不能透出去。
# `preparation_disclosure_tail` 是安全披露**首句之后**那半段（CI-917① 为网页端加的：
# 前端要「自己写首句 + 接着念其余」，此前它在客户端用正则剥第一句，而正文里的 `0.03%`
# 被从**小数点**处切开、屏幕上印成 `03%`）。
# 🔴 危害是**不对称**的：完整的 `preparation_disclosure` 带着浓度，而 tail **不带**
# ⇒ 只印 tail 的消费者会**静默丢掉浓度**，正是 CI-917① 要消灭的那个失败。
# MCP 面本来就拿得到完整那句，tail 对它零信息增量 ⇒ 删掉不减少任何东西。
#
# 🔴 **为什么在入口剥而不是在出口**：`structured_content=` 在本文件有 25 个构造点，
# 实测 23 个工具里有 **15 个**会把它透出去（探针给每个容器都塞上这个键再全量调一遍）。
# 挑其中几处改 ＝ 作用域手写 ⇒ 必漏。入口只有本函数 + 少数直接 `.json()` 的旁路，
# 而旁路由守卫 `test_preparation_tail_not_exposed.py` 扫出来（它按 `list_tools()` 发现成员，
# 新工具自动进来；变异＝往仓里加一个透传新工具看它红不红）。
_TAIL_ONLY_KEYS = frozenset({"preparation_disclosure_tail"})


def _drop_tail_keys(obj):
    """递归剥掉 `_TAIL_ONLY_KEYS`。非容器原样返回；不改原对象。"""
    if isinstance(obj, dict):
        return {k: _drop_tail_keys(v) for k, v in obj.items() if k not in _TAIL_ONLY_KEYS}
    if isinstance(obj, list):
        return [_drop_tail_keys(v) for v in obj]
    return obj


def _billed_json(res: "httpx.Response") -> dict:
    """raise_for_status with a caller-friendly 402 (balance exhausted) message, then
    return the JSON body with any credit usage attached under `_usage`."""
    if res.status_code == 402:
        detail = {}
        try:
            detail = res.json().get("detail") or {}
        except Exception:
            pass
        if not isinstance(detail, dict):
            detail = {}
        bal = detail.get("balance")
        msg = "Credit balance exhausted."
        # CI-1396：余额 ≤0 时不报数。soft_cap 的欠款按设计会抵扣次日额度，「Remaining: -2」
        # 是真数字但对用户只有困惑；去路由由后端的 `message` 讲。
        try:
            if bal is not None and float(bal) > 0:
                msg += f" Remaining: {float(bal):g} credits."
        except (TypeError, ValueError):
            pass
        # CI-1396：后端的 `message` 按钱包算好了去路（升级链接 + 免费档几点重置），比我们
        # 这句通用话准 ⇒ 有就用它；没有（旧后端）才退回通用话。
        hint = detail.get("message")
        upgrade = detail.get("upgrade_url") if isinstance(detail.get("upgrade_url"), str) else None
        resets = detail.get("resets_at") if isinstance(detail.get("resets_at"), str) else None
        if isinstance(hint, str) and hint.strip():
            raise CreditsExhausted(f"{msg} {hint.strip()}", upgrade_url=upgrade, resets_at=resets)
        raise CreditsExhausted(msg + " Top up at msdschain.lagentbot.com to continue.",
                               upgrade_url=upgrade, resets_at=resets)
    # CI-410：pydantic 把「为什么不合法」放在响应体的 `detail` 里，而这条错误路径此前
    # 从不读它 ⇒ 调用方只拿到 `Client error '422 Unprocessable Entity' for url …`。
    # 不是哑失败（调用可见地失败了），但**不可行动**：模型看不出是"化学品超过 24 个"
    # 还是"参数名写错了"。同 CI-523 一族——信息在，只是没到达读它的人。
    _raise_for_status_with_reason(res)
    data = _drop_tail_keys(res.json())
    usage = _parse_usage(res)
    if usage and isinstance(data, dict):
        data["_usage"] = usage
    return data


def _usage_line(usage: dict) -> str:
    """Human-readable one-liner appended to a metered tool's text output."""
    bal = usage.get("balance")
    reason = usage.get("reason", "")
    cost = usage.get("cost", 0) or 0
    # 🔴 CI-904：「订阅」只由 `reason` 说了算。余额未知时**什么都别声称**——
    # 尤其别说「no credits deducted」，那一句在 cost>0 时是**假话**，且假在对用户有利那侧。
    if reason == "subscription":
        return "\n\n---\n💳 Included in your plan (no credits deducted)."
    head = (f"This call used {cost:g} credits" if cost > 0
            else "Free lookup (0 credits)")
    if bal is None or bal < 0:
        return f"\n\n---\n💳 {head}."
    return f"\n\n---\n💳 {head} · Balance: {bal:g} credits remaining."


def _with_usage(result: "CallToolResult", data: dict) -> "CallToolResult":
    """Append the credit usage line to a value tool's result (text + structuredContent).
    No-op when the backend didn't meter the call (`_usage` absent)."""
    usage = (data or {}).get("_usage")
    if not usage:
        return result
    content = list(result.content or [])
    line = _usage_line(usage)
    if content and isinstance(content[0], TextContent):
        content = [TextContent(type="text", text=content[0].text + line)] + content[1:]
    sc = result.structured_content
    if sc is not None:
        sc = {**sc, "usage": usage}
    # `meta` 是只给组件的通道（CI-1098），重建时不带上就会静默丢掉。
    return CallToolResult(content=content, structured_content=sc, meta=result.meta)


# ---------------------------------------------------------------------------
# CI-1133：把后端的 `language_note` 从 structuredContent 抬到**文本面**
# ---------------------------------------------------------------------------
# 上游 CI-1123 让 `/api/v2` 在这一轮确实读过「只有 en/zh 两份」的文案表时回一个顶层
# `language_note`。`_expose()` 默认全透 ⇒ 那个键**已经在** structuredContent 里；而
# 模型与多数客户端读的是 `TextContent`，它是各工具逐字段显式拼出来的 ⇒ 没人提的键
# 永远不出现。同族：CI-553（`precursor_disclosure`）· CI-360（`insufficient_reason`）。
#
# 🔴 **一处，不是 23 个渲染器各补一行** —— 与后端把它做成路由类（`LanguageNoteRoute`）
# 是同一条理由：逐渲染器补＝23 份推导不出来的副本，而第 24 个工具出生时**没有任何东西
# 会红**。这里挂在 `_reported` 上，它包着每一个工具 ⇒ 成员自己发现。
# 🔴 它是「披露」不是「内容」：措辞由后端确定性给出（五语言一张表），本模块只负责把它
# 搬到文本面并配一条禁令——CI-1096 已证明散文层保证不了任何具体字符串到达用户，
# 所以判据只能打在**我们返回的文本**上（本票的验收就是这么写的）。
# 🔴 放在**最前**：CI-89-followup 实测靠后的内容会被客户端模型丢掉，而这句话的全部价值
# 在于用户看见它（看不见时他会以为是自己参数传错了，那正是 CI-1123 要消灭的误解）。
_LANGUAGE_NOTE_DIRECTIVE = (
    "[language] {note}\n"
    "Relay that sentence to the user verbatim, in the language it is written in, "
    "before anything else. Do not translate it, do not drop it when summarizing, and "
    "do not restate it as \"this language is not supported\" — the rest of this answer "
    "is still in the requested language wherever we have it.\n\n"
)


def _language_note_of(items) -> dict:
    """从一组后端响应里把那句披露抬到顶层 —— 给**扇出 N 次**的工具用。

    🔴 这不是第二份副本：那句话就在 `items` 里，这里只把它搬到 `_relay_language_note`
    够得着的位置（顶层）。手拼 structuredContent 白名单的工具**不会**自动带上新键
    （CI-342 记过的那条），而 `check_regulatory_compliance` 恰好是 CI-1123 在 Prod 上
    验过会播那句话的两个端点之一 ⇒ 不补这一手，最该说话的那个工具正好是哑的。
    🔴 取第一条非空的就够：扇出里各条是**同一个 lang、同一张表**，后端给的是同一句。
    """
    for item in items or []:
        if isinstance(item, dict):
            note = item.get("language_note")
            if isinstance(note, str) and note.strip():
                return {"language_note": note}
    return {}


def _relay_language_note(result):
    """文本面补上那句「有部分内容只有英文」。没有这个键时**一个字节都不改**。

    🔴 幂等：正文里已经逐字有这句话就原样返回（哪天某个渲染器自己渲染了它，这里不该
    再播一遍；后端那一层同样是 `setdefault`，两边对「更具体的那句优先」口径一致）。
    """
    sc = getattr(result, "structured_content", None)
    if not isinstance(sc, dict):
        return result
    note = sc.get("language_note")
    if not isinstance(note, str) or not note.strip():
        return result
    content = list(getattr(result, "content", None) or [])
    # 🔴 首块不是文本就放过：这条通道只负责**文本面**，硬塞一个 TextContent 进去会改变
    # 返回形状，而形状是对外契约（CI-919 那批适配器按块类型读）。
    if not content or not isinstance(content[0], TextContent):
        return result
    if note in content[0].text:
        return result
    return CallToolResult(
        content=[TextContent(type="text",
                             text=_LANGUAGE_NOTE_DIRECTIVE.format(note=note)
                                  + content[0].text)] + content[1:],
        structured_content=sc,
        is_error=bool(getattr(result, "is_error", False)),
        meta=getattr(result, "meta", None),
    )


def _strip_usage(data: dict) -> dict:
    """Drop the internal `_usage` key that _billed_json attaches, so lookup tools
    that expose `structured_content=data` don't leak it into the client output.
    (Value tools build their own structuredContent + surface a clean `usage` block.)"""
    if not isinstance(data, dict) or "_usage" not in data:
        return data
    return {k: v for k, v in data.items() if k != "_usage"}


# ---------------------------------------------------------------------------
# CI-342：structuredContent 从「白名单」翻成「透传 + 显式挡掉的键」
# ---------------------------------------------------------------------------
# 旧写法是逐字段手抄的 dict：后端往响应里加字段，我们**不会带上，也不会报错**，
# 客户端侧就是「这个字段不存在」。实测丢掉的（真调后端 + 真调工具做的差集）：
#   顶层 `unresolved_detail`（compat / risk / batch）——`unresolved` 只给了名字，
#     而**为什么没解析出来**的机器可读 `code` 全在这个键里
#   `compat.pairs[]` / `batch.compatibility.pairs[]` 丢 `cas_a`/`cas_b`/`citation`/
#     `source_detail`/`verdict`（batch 还多丢 `source`）——11 个字段只透出 5 个
#   `risk.warnings[]` 丢 `additional_hazards`（带 supplier + revision_date）
#   `get_sds_document` 丢 `physical_form`/`physical_form_disclosure`
#   `search_msds_online` 丢 `status`/`completeness`/`chemical_name`
#
# 🔴 票里担心「白名单也承担着不外泄内部字段的职责，别一把梭透传」——**对实测到的这批
# 不成立**，逐个看过值：`citation` 是 `CAMEO:1x10-acid-strong-base-strong`（公开引用）、
# `source_detail` 引的是 CAMEO 的分类规则、`additional_hazards` 带 supplier 与修订日期
# ——全是可追溯性数据，正是我们要给出去的东西，没有一个泄露内部机制。
# 但「今天这批没问题」不等于「以后都没问题」⇒ 不做裸透传，走这个 helper：**默认全透，
# 要挡的键必须写进 `drop`**，于是「决定不给」这件事永远是显式的、可 grep 的。
_INTERNAL_KEYS = frozenset({"_usage"})

# CI-592：`/quick-chat` 的载荷除了 `_usage` 还带一个 `_timed_out`——那是 `_quick_chat`
# 自己在超时兜底时打的标记，工具函数用它记 `_log_intent`，对客户端没有意义（超时这件事
# 已经写在 `answer` 里）。🔴 它必须**显式**出现在这里：`_expose` 默认全透，漏掉就会把一个
# 下划线开头的内部标记发给客户端。
_QUICK_INTERNAL_KEYS = _INTERNAL_KEYS | frozenset({"_timed_out"})


def _expose(data: dict, *, rename: dict[str, str] | None = None,
            drop: frozenset[str] = _INTERNAL_KEYS, override: dict | None = None) -> dict:
    """把后端的一层 dict 透给客户端：默认全给，只挡 `drop` 里的键。

    `rename` 处理**有意的**对外命名（如 `chem1` → `chemical_a`：MCP 面的命名是对外契约，
    改它会打断现有客户端）。`override` 覆盖我们自己算过的值（如归一化后的 traceability）。
    """
    if not isinstance(data, dict):
        return data
    rename = rename or {}
    out = {rename.get(k, k): v for k, v in data.items() if k not in drop}
    if override:
        out.update(override)
    return out


def _with_suppliers(body: dict, chemicals: list[str],
                    suppliers: dict[str, str] | None) -> dict:
    """没带 supplier 时请求体**一个字节都不变**。

    🔴 刻意不写成 `body["suppliers"] = _normalize_suppliers(...)`：那样每个请求都会多带一个
    `"suppliers": {}`。后端对它等价，但它会把**七个端点的请求形状**同时改掉，而
    「不带这个参数的调用一字未变」是这次改动唯一能便宜验证的事。
    """
    cleaned = _normalize_suppliers(chemicals, suppliers)
    return {**body, "suppliers": cleaned} if cleaned else body
