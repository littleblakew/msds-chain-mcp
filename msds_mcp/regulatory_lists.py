from __future__ import annotations

import httpx

from msds_mcp.app import API_URL, LANG, TIMEOUT
from msds_mcp.calls import _billed_json, _headers
from msds_mcp.params import _normalize_catalog_lang



# CI-523: the coverage caveat is part of the ANSWER, not decoration — it is what
# stops "we found EU entries" from being read as "cleared everywhere else". It must
# therefore appear on BOTH result branches (hits and no hits), and it must follow the
# caller's language: quick-chat used to answer zh callers in Chinese, so rendering it
# in English only would be a regression this ticket introduced. One entry per
# language in `_CATALOG_LANGS` (CI-1095).
#
# 🔴 No list COUNT in any of these strings (CI-1164): the backend derives the count
# from the database, and a number copied here goes stale silently the next time a
# list is added. Name what matters for not over-reading the answer instead.
_REG_LIST_COVERAGE_NOTE = {
    "en": ("Coverage note: this is our curated copy of the regulatory lists, not a "
           "live regulatory feed. For Taiwan it holds four restriction lists (MOENV "
           "Listed Toxic and Concerned Chemical Substances, OSHA Priority Management "
           "and Controlled Chemicals) plus the OSHA Permissible Exposure Standards, "
           "which record membership only: no limit values, and not a restriction. It "
           "holds no Taiwan inventory (TCSI) and no IARC data. An absent list means "
           '"not found in our copy", never "not regulated".'),
    "zh": "覆盖范围说明：这是我们整理的监管清单副本，不是实时监管数据源。"
          "台湾有四份限制类清单（环境部「列管毒性化学物质」「关注化学物质」、"
          "职安署「优先管理化学品」「管制性化学品」），另有职安署「容许暴露标准」，"
          "后者只记录是否列名，不含限值，也不是限制。不含台湾既有化学物质名录（TCSI），"
          "也不含 IARC 数据。某份清单没命中只代表「我们这份副本里没有」，"
          "绝不等于「不受监管」。",
    "zh-TW": ('覆蓋範圍說明：這是我們整理的監管清單副本，不是實時監管資料來源。臺灣有四份限制類清單（環境部「列管毒性化學物質」「關注化學物質」、職安署「優先管理化學品」「管制性化學品」），另有職安署「容許暴露標準」，後者只記錄是否列名，不含限值，也不是限制。不含臺灣既有化學物質名錄（TCSI），也不含 '
              'IARC 資料。某份清單沒命中只代表「我們這份副本裡沒有」，絕不等於「不受監管」。'),
    # 🔴 **每一门语言都必须点名台湾、MOENV、TCSI 与 IARC**（CI-523 的前向红线：
    # 营销面可以模糊，runtime 必须精确）。翻译时这些名字和那句「没命中 ≠ 不受监管」
    # 一起搬，少掉任何一半都会把一句限定说明变成一句担保。
    "ja": ("カバレッジに関する注記：これは当社が整理した規制リストの写しであり、"
           "リアルタイムの規制フィードではありません。台湾については制限リスト 4 件"
           "（環境部の「列管毒性化学物質」「関注化学物質」、労働部職業安全衛生署の"
           "「優先管理化学品」「管制性化学品」）と、職業安全衛生署の「許容暴露基準」を"
           "保有しています。後者は収載の有無のみで、限度値は含まず、制限でもありません。"
           "台湾の既存化学物質インベントリ（TCSI）と IARC のデータは含まれていません。"
           "リストに一致がないことは「当社の写しに見つからない」という意味であり、"
           "「規制対象外」という意味では決してありません。"),
    "de": ("Hinweis zur Abdeckung: Dies ist unsere kuratierte Kopie der "
           "Regulierungslisten, kein Live-Regulierungs-Feed. Für Taiwan enthält sie "
           "vier Beschränkungslisten (MOENV: gelistete toxische und besorgniserregende "
           "Chemikalien; OSHA: Chemikalien mit Prioritätsmanagement und kontrollierte "
           "Chemikalien) sowie die OSHA-Expositionsgrenzwertliste, die nur die "
           "Aufnahme erfasst: keine Grenzwerte und keine Beschränkung. Sie enthält kein "
           "taiwanisches Altstoffverzeichnis (TCSI) und keine IARC-Daten. Eine fehlende "
           "Liste bedeutet „in unserer Kopie nicht gefunden“ und niemals „nicht reguliert“."),
    "id": ("Catatan cakupan: ini adalah salinan kurasi kami atas daftar regulasi, "
           "bukan umpan regulasi langsung. Untuk Taiwan memuat empat daftar pembatasan "
           "(MOENV: Bahan Kimia Beracun Terdaftar dan Bahan Kimia yang Menjadi "
           "Perhatian; OSHA: Bahan Kimia Manajemen Prioritas dan Bahan Kimia "
           "Terkendali) serta Standar Paparan yang Diizinkan OSHA, yang hanya mencatat "
           "keanggotaan: tanpa nilai batas dan bukan pembatasan. Tidak memuat inventaris "
           "Taiwan (TCSI) dan tidak memuat data IARC. "
           "Daftar yang tidak cocok berarti \"tidak ditemukan dalam salinan kami\", "
           "bukan berarti \"tidak diatur\"."),
    'ko': ('적용 범위 안내: 이것은 당사가 정리한 규제 목록 사본이며 실시간 규제 피드가 아닙니다. 대만은 제한 목록 4종(MOENV 지정 독성화학물질·관심화학물질, OSHA '
     '우선관리화학품·관제성화학품)과 OSHA 허용노출기준을 보유하며, 후자는 등재 여부만 기록하고 한도값이 없으며 제한도 아닙니다. 대만 기존화학물질 목록(TCSI)과 IARC '
     '데이터는 포함하지 않습니다. 목록에 일치 항목이 없다는 것은 "당사 사본에서 찾지 못함"을 뜻하며, 결코 "규제 대상 아님"을 뜻하지 않습니다.'),
    'ru': ('Примечание об охвате: это наша курируемая копия регуляторных списков, а не живой регуляторный '
     'источник. По Тайваню она содержит четыре ограничительных списка (MOENV: перечисленные токсичные '
     'и вызывающие озабоченность химические вещества; OSHA: вещества приоритетного управления и '
     'контролируемые вещества), а также стандарты допустимого воздействия OSHA, где фиксируется только '
     'факт включения: без предельных значений, и это не ограничение. Реестр существующих веществ '
     'Тайваня (TCSI) и данные IARC не включены. Отсутствие в списке означает «не найдено в нашей '
     'копии», а не «не регулируется».'),
    'fr': ("Note de couverture : il s'agit de notre copie organisée des listes réglementaires, et non d'un "
     'flux réglementaire en direct. Pour Taïwan, elle contient quatre listes de restriction (MOENV : '
     'substances chimiques toxiques répertoriées et substances chimiques préoccupantes ; OSHA : '
     'produits chimiques à gestion prioritaire et produits chimiques contrôlés), ainsi que les normes '
     "d'exposition admissible de l'OSHA, qui n'indiquent que l'inscription : aucune valeur limite, et "
     "ce n'est pas une restriction. Elle ne contient ni l'inventaire de Taïwan (TCSI) ni de données du "
     "CIRC (IARC). L'absence d'une liste signifie « non trouvé dans notre copie », jamais « non "
     'réglementé ».'),
    'es': ('Nota de cobertura: esta es nuestra copia curada de las listas regulatorias, no una fuente '
     'regulatoria en tiempo real. Para Taiwán contiene cuatro listas de restricción (MOENV: sustancias '
     'químicas tóxicas listadas y sustancias químicas de interés; OSHA: sustancias químicas de gestión '
     'prioritaria y sustancias químicas controladas), además de las normas de exposición permisible de '
     'la OSHA, que solo registran la inclusión: sin valores límite y no constituyen una restricción. '
     'No contiene el inventario de Taiwán (TCSI) ni datos de la IARC. Que una lista no aparezca '
     'significa «no encontrado en nuestra copia», nunca «no regulado».'),
}
_REG_LIST_STRINGS = {
    "en": {"title": "Regulatory Lists", "not_checked": "⚠️ **Not checked.**",
           "status": "Status",
           "near": "Near matches in the database", "cas": "CAS",
           "count": "Matching lists", "unknown": "Unknown list",
           "none": "No match in our copy of the regulatory lists."},
    "zh": {"title": "监管清单", "not_checked": "⚠️ **未核查。**",
           "status": "状态",
           "near": "库中的近似命中", "cas": "CAS",
           "count": "命中清单数", "unknown": "未知清单",
           "none": "在我们那份监管清单副本中没有命中。"},
    "zh-TW": {'title': '監管清單',
              'not_checked': '⚠️ **未核查。**',
              'status': '狀態',
              'near': '庫中的近似命中',
              'cas': 'CAS',
              'count': '命中清單數',
              'unknown': '未知清單',
              'none': '在我們那份監管清單副本中沒有命中。'},
    # 🔴 CI-1095：catalog 族放开五语之后，**后端回日文而这里只有英文**＝新造一种
    # 混语言输出，而且它不在 CI-1123 那句 `language_note` 的覆盖里（那一句只描述
    # **后端自己**的回退）⇒ 用户看不出哪半是我们没翻。review 抓到的。
    "ja": {"title": "規制リスト", "not_checked": "⚠️ **未確認。**",
           "status": "ステータス",
           "near": "データベース内の近似一致", "cas": "CAS",
           "count": "一致したリスト数", "unknown": "不明なリスト",
           "none": "当社が保有する規制リストの写しには一致がありませんでした。"},
    "de": {"title": "Regulatorische Listen", "not_checked": "⚠️ **Nicht geprüft.**",
           "status": "Status",
           "near": "Ähnliche Treffer in der Datenbank", "cas": "CAS",
           "count": "Treffer in Listen", "unknown": "Unbekannte Liste",
           "none": "Kein Treffer in unserer Kopie der Regulierungslisten."},
    "id": {"title": "Daftar Regulasi", "not_checked": "⚠️ **Belum diperiksa.**",
           "status": "Status",
           "near": "Kecocokan mirip dalam basis data", "cas": "CAS",
           "count": "Jumlah daftar yang cocok", "unknown": "Daftar tidak dikenal",
           "none": "Tidak ada kecocokan dalam salinan daftar regulasi milik kami."},
    'ko': {'title': '규제 목록',
     'not_checked': '⚠️ **확인하지 못함.**',
     'status': '상태',
     'near': '데이터베이스의 유사 일치 항목',
     'cas': 'CAS',
     'count': '일치한 목록 수',
     'unknown': '알 수 없는 목록',
     'none': '당사가 보유한 규제 목록 사본에서 일치 항목이 없습니다.'},
    'ru': {'title': 'Регуляторные списки',
     'not_checked': '⚠️ **Не проверено.**',
     'status': 'Статус',
     'near': 'Похожие совпадения в базе данных',
     'cas': 'CAS',
     'count': 'Совпавшие списки',
     'unknown': 'Неизвестный список',
     'none': 'Совпадений в нашей копии регуляторных списков нет.'},
    'fr': {'title': 'Listes réglementaires',
     'not_checked': '⚠️ **Non vérifié.**',
     'status': 'Statut',
     'near': 'Correspondances proches dans la base de données',
     'cas': 'CAS',
     'count': 'Listes correspondantes',
     'unknown': 'Liste inconnue',
     'none': 'Aucune correspondance dans notre copie des listes réglementaires.'},
    'es': {'title': 'Listas regulatorias',
     'not_checked': '⚠️ **No verificado.**',
     'status': 'Estado',
     'near': 'Coincidencias cercanas en la base de datos',
     'cas': 'CAS',
     'count': 'Listas coincidentes',
     'unknown': 'Lista desconocida',
     'none': 'Sin coincidencias en nuestra copia de las listas regulatorias.'},
}


# CI-714：这三/四条单化学品路只拿得到一个布尔 `unresolved: true`，说不出成因。
# 唯一被授权说的那句话写在这里**一次** —— 此前它被手抄成三种拼写（review 抓到，
# 同族 [[CI-572]]「格式留两份 ⇒ 改一处漏另一处」）。
# 🔴 zh 也要有：`_format_regulatory_lists` 那条路按调用方语言渲染，只给英文等于
# 在中文面上留一句原样的「库中未收录。」。
# 🔴 **极性是这句话的全部**：说的是「我们没解析出来」，**不是**「库里没有」。
# 翻译时任何一门语言把这层否定丢掉，都会变成一句我们无权说的断言（CI-243/322/334 同形）。
_UNRESOLVED_BOOLEAN_NOTE = {
    "en": ("We could not resolve this input to a record — this is NOT a statement that "
           "the database has no record for it."),
    "zh": "我们没能把这个输入解析到一条记录 —— 这**不**代表库中没有它。",
    "zh-TW": '我們沒能把這個輸入解析到一條記錄 —— 這**不**代表庫中沒有它。',
    "ja": ("この入力をレコードに紐付けできませんでした —— これはデータベースに該当する"
           "レコードが**存在しない**という意味では**ありません**。"),
    "de": ("Wir konnten diese Eingabe keinem Datensatz zuordnen — das ist **keine** "
           "Aussage darüber, dass die Datenbank keinen Datensatz dazu hat."),
    "id": ("Kami tidak dapat mencocokkan masukan ini dengan suatu rekaman — ini **bukan** "
           "pernyataan bahwa basis data tidak memiliki rekaman untuknya."),
    'ko': '이 입력을 레코드와 연결하지 못했습니다. 이는 데이터베이스에 해당 레코드가 **없다는** 뜻이 **아닙니다**.',
    'ru': ('Нам не удалось сопоставить этот ввод с записью. Это **не** означает, что в базе данных нет '
     'записи о нём.'),
    'fr': ("Nous n'avons pas pu associer cette saisie à un enregistrement. Cela ne signifie **pas** que la "
     "base de données n'en contient aucun."),
    'es': ('No pudimos asociar esta entrada a un registro. Esto **no** significa que la base de datos no '
     'tenga ningún registro de ella.'),
}


def _unresolved_boolean_note(lang: str | None = None) -> str:
    """CI-714: 对「只有一个布尔」的未解析载荷，我们唯一说得出口的那句话。"""
    return _UNRESOLVED_BOOLEAN_NOTE.get(_normalize_catalog_lang(lang or LANG),
                                        _UNRESOLVED_BOOLEAN_NOTE["en"])


# 后端 `unresolved_detail.reason` 会本地化的语言（`reason_en` 恒为英文）。
_REASON_LOCALIZED_LANGS = ("zh", "zh-TW")


def _unresolved_reason_note(data: dict, lang: str | None = None) -> str:
    """CI-714 的 MCP 半边：后端能说出「为什么」时就说逐案那句，说不出才回退到通用句。

    上游（growth）把 `/api/v2/compliance` 与 `/api/v2/emergency-response` 的未解析分支
    从「只有一个布尔」升级成带 `unresolved_detail`（`describe_unresolved_i18n` 的产物，
    形状 `{query, code, reason, reason_en}`）。这里只做**取用**，不新造披露口径。

    🔴 **别把这类载荷接进 `_unresolved_block`** —— 它对 `data["unresolved"]` 做
    `', '.join(...)`，而单化学品端点的 `unresolved` 是 **bool**，会直接 TypeError。
    这条是后端在 `direct_emergency_response` 的注释里点名写给本仓的（那处危险早于这个
    新键，实测带不带 `unresolved_detail` 都一样崩），**别"顺手统一"成一个渲染器**。

    🔴 **回退不是可选项，而且它会被频繁走到**：后端只在走 `resolve_cas_and_detail_or_miss`
    的那条路上给这个键。缺席的成因至少两种（走了别的分支 / 后端回滚），而两者
    **在这里完全同形** ⇒ 不为缺席告警，也不据此推断后端版本，老实回退。
    """
    detail = data.get("unresolved_detail")
    if isinstance(detail, dict):
        # 后端只给 zh / zh-TW 本地化 `reason`（zh-TW 是繁体）；其余语言的 `reason` 是简体，
        # 回英文好过回一句读不懂的中文。
        key = "reason" if _normalize_catalog_lang(lang or LANG) in _REASON_LOCALIZED_LANGS else "reason_en"
        reason = (detail.get(key) or "").strip()
        if reason:
            return reason
    return _unresolved_boolean_note(lang)


def _format_regulatory_lists(data: dict, chemical: str, lang: str | None = None) -> str:
    """Render `/api/v2/regulatory-lists` (CI-523). Pure function so the three
    branches below are testable without a backend.

    🔴 Three branches, and the two failure ones are the reason this tool stopped
    going through an LLM at all:
      - `lists_unavailable` → say we did NOT check. An empty list rendered without
        this is read as "on no watch list", which is the opposite of what happened
        (CI-507).
      - unresolved → say which kind of "we don't have it" this is (CI-375),
        including near matches, so the caller can retry with a CAS instead of
        dead-ending.
      - resolved with zero hits → "not found in our copy", never "not regulated".
    """
    lg = _normalize_catalog_lang(lang or LANG)
    s = _REG_LIST_STRINGS.get(lg, _REG_LIST_STRINGS["en"])
    lists = data.get("lists") or []
    lines = [f"**{s['title']} — {data.get('chemical') or chemical}**\n"]

    if data.get("lists_unavailable"):
        lines.append(f"{s['not_checked']} {data.get('error') or ''}".rstrip())
        return "\n".join(lines)

    if not data.get("cas"):
        # 🔴 CI-714 第四处：`error` 缺席时的兜底此前是 `not_found`
        # （"Not found in the database." / 「库中未收录。」）—— 而这一支的条件只是
        # 「没解析出 CAS」。docstring 上面自己写着这里要说"which kind of we don't
        # have it"，兜底那句恰恰是唯一一种我们无权断言的。
        lines.append(f"**{s['status']}:** {data.get('error') or _unresolved_boolean_note(lg)}")
        near = data.get("near_matches") or []
        if near:
            lines.append(f"**{s['near']}:** {', '.join(near)}")
        return "\n".join(lines)

    lines.append(f"**{s['cas']}:** {data.get('cas')}")
    lines.append(f"**{s['count']}:** {data.get('count', len(lists))}\n")
    if lists:
        lines.extend(
            f"- **{entry.get('list') or s['unknown']}** ({entry.get('region') or '—'})"
            for entry in lists
        )
    else:
        lines.append(s["none"])
    # CI-1235: regions whose inventory we do not hold but which have an official
    # single-substance query page (TW TCSI → CSNN). A pointer, not a check.
    for lk in data.get("inventory_lookups") or []:
        lines.append(f"\n🔗 **{lk.get('region') or '—'}:** {_with_lookup_url(lk.get('note'), lk)}")
    lines.append(f"\n> {_REG_LIST_COVERAGE_NOTE.get(lg, _REG_LIST_COVERAGE_NOTE['en'])}")
    return "\n".join(lines)


async def _direct_regulatory_lists(chemical: str, lang: str | None = None) -> dict:
    """POST /api/v2/regulatory-lists — direct list lookup, no LLM (CI-523)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API_URL}/api/v2/regulatory-lists",
            json={"chemical": chemical, "lang": _normalize_catalog_lang(lang or LANG)},
            headers=_headers(),
        )
        return _billed_json(res)


def _with_lookup_url(note: str | None, lookup: dict) -> str:
    """The backend-localized lookup note, guaranteed to contain the official URL.

    The URL has a single definition in the backend; it is only read from the
    payload here, never written in this repo."""
    note = note or lookup.get("name") or ""
    url = lookup.get("url")
    return note if not url or url in note else f"{note} {url}".strip()
