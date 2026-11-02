from __future__ import annotations

import re

from msds_mcp.app import LANG
from msds_mcp.calls import ToolReason
from msds_mcp.params import _normalize_catalog_lang
from msds_mcp.regulatory_lists import _REASON_LOCALIZED_LANGS, _unresolved_boolean_note



# CI-1138: labels only. Every sentence that carries meaning (`notice`, the §8 gap
# notes, the §15 `scope_note`) is already localized by the backend and rendered
# verbatim — re-wording them here would be a second copy that drifts.
_SDS_DRAFT_STRINGS = {
    "en": {"title": "SDS Draft",
           "no_regional": "⚠️ We hold no regulatory list for {region}: only international conventions were checked. Nothing below is a finding about {region}.", "s8": "Section 8 — Exposure controls / personal protection",
           "s15": "Section 15 — Regulatory information", "source": "Source",
           "revision": "revision", "pdf": "original PDF on file",
           "not_drafted": "Not drafted", "s15_basis": "Basis: regulatory lists we hold for",
           "lists_checked": "Lists checked", "listed_on": "Listed on",
           "not_listed": "Not on the lists checked",
           "not_checked": "⚠️ Could not check (list source unreadable) — this is NOT \"not listed\"",
           "lists_unreadable": "⚠️ The regulatory lists could not be read, so NOTHING was checked for Section 15. This is NOT a finding that the ingredients are unlisted.",
           "unresolved": "Not identified", "lookup_failed": "Lookup failed (retry)",
           "truncated": "⚠️ This block may be cut off: check the original SDS before copying it.", "limits_not_found": "⚠️ No exposure-limit values found in this text: that does NOT mean there are none. Check 8.1 in the original SDS.",
           "missing": "For every ingredient listed as not drafted or not identified: upload that supplier's SDS with `upload_msds_pdf`, or pass a CAS number, then draft again."},
    "zh": {"title": "SDS 草稿",
           "no_regional": "⚠️ 我们没有 {region} 的任何法规清单：只核查了国际公约。下面没有任何一条是关于 {region} 的结论。", "s8": "第 8 节 — 接触控制 / 个体防护",
           "s15": "第 15 节 — 法规信息", "source": "出处",
           "revision": "修订", "pdf": "有 SDS 原件（PDF）",
           "not_drafted": "未起草", "s15_basis": "依据：我们持有的法规清单，地区",
           "lists_checked": "已核查的清单", "listed_on": "列于",
           "not_listed": "不在已核查的清单上",
           "not_checked": "⚠️ 无法核查（清单数据源不可读）—— 这不等于「不在清单上」",
           "lists_unreadable": "⚠️ 法规清单当前不可读，第 15 节一份都没有核查。这不等于这些成分不在任何清单上。",
           "unresolved": "未能识别", "lookup_failed": "查询失败（请重试）",
           "truncated": "⚠️ 这一段可能被截断了：抄进你的 SDS 前请核对原件。", "limits_not_found": "⚠️ 这段文字里没找到接触限值：这不代表没有限值，请核对原件的 8.1。",
           "missing": "上面列为未起草或未能识别的成分：用 `upload_msds_pdf` 上传该供应商的 SDS，或改传 CAS 号，然后重新起草。"},
    "zh-TW": {'title': 'SDS 草稿',
              'no_regional': '⚠️ 我們沒有 {region} 的任何法規清單：只核查了國際公約。下面沒有任何一條是關於 {region} 的結論。',
              's8': '第 8 節 — 接觸控制 / 個體防護',
              's15': '第 15 節 — 法規資訊',
              'source': '出處',
              'revision': '修訂',
              'pdf': '有 SDS 原件（PDF）',
              'not_drafted': '未起草',
              's15_basis': '依據：我們持有的法規清單，地區',
              'lists_checked': '已核查的清單',
              'listed_on': '列於',
              'not_listed': '不在已核查的清單上',
              'not_checked': '⚠️ 無法核查（清單資料來源不可讀）—— 這不等於「不在清單上」',
              'lists_unreadable': '⚠️ 法規清單當前不可讀，第 15 節一份都沒有核查。這不等於這些成分不在任何清單上。',
              'unresolved': '未能識別',
              'lookup_failed': '查詢失敗（請重試）',
              'truncated': '⚠️ 這一段可能被截斷了：抄進你的 SDS 前請核對原件。',
              'limits_not_found': '⚠️ 這段文字裡沒找到接觸限值：這不代表沒有限值，請核對原件的 8.1。',
              'missing': '上面列為未起草或未能識別的成分：用 `upload_msds_pdf` 上傳該供應商的 SDS，或改傳 CAS 號，然後重新起草。'},
    "ja": {"title": "SDS 下書き",
           "no_regional": "⚠️ {region} の規制リストは保有していません：国際条約のみ確認しました。以下は {region} についての結論ではありません。", "s8": "第 8 項 — ばく露防止及び保護措置",
           "s15": "第 15 項 — 適用法令", "source": "出典",
           "revision": "改訂", "pdf": "SDS 原本（PDF）あり",
           "not_drafted": "下書き対象外", "s15_basis": "根拠：当方が保有する規制リスト、地域",
           "lists_checked": "確認したリスト", "listed_on": "掲載リスト",
           "not_listed": "確認したリストには掲載なし",
           "not_checked": "⚠️ 確認できませんでした（リストのデータ源を読み取れません）—「掲載なし」ではありません",
           "lists_unreadable": "⚠️ 規制リストを読み取れなかったため、第 15 項は何も確認していません。成分がどのリストにも載っていないという意味ではありません。",
           "unresolved": "特定できず", "lookup_failed": "照会に失敗（再試行してください）",
           "truncated": "⚠️ この部分は途中で切れている可能性があります：転記する前に原本を確認してください。", "limits_not_found": "⚠️ この本文にばく露限界値が見つかりません：限界値がないという意味ではありません。原本の 8.1 を確認してください。",
           "missing": "下書き対象外・特定できずとなった成分は、`upload_msds_pdf` でその供給者の SDS をアップロードするか CAS 番号を指定して、もう一度下書きしてください。"},
    "de": {"title": "SDB-Entwurf",
           "no_regional": "⚠️ Wir führen keine Regulierungsliste für {region}: geprüft wurden nur internationale Übereinkommen. Nichts unten ist eine Aussage über {region}.", "s8": "Abschnitt 8 — Begrenzung und Überwachung der Exposition / persönliche Schutzausrüstung",
           "s15": "Abschnitt 15 — Rechtsvorschriften", "source": "Quelle",
           "revision": "Revision", "pdf": "Original-PDF vorhanden",
           "not_drafted": "Nicht entworfen", "s15_basis": "Grundlage: unsere Regulierungslisten für",
           "lists_checked": "Geprüfte Listen", "listed_on": "Gelistet auf",
           "not_listed": "Auf keiner der geprüften Listen",
           "not_checked": "⚠️ Nicht prüfbar (Listenquelle nicht lesbar) — das heißt NICHT „nicht gelistet“",
           "lists_unreadable": "⚠️ Die Regulierungslisten waren nicht lesbar, daher wurde für Abschnitt 15 NICHTS geprüft. Das heißt nicht, dass die Bestandteile auf keiner Liste stehen.",
           "unresolved": "Nicht identifiziert", "lookup_failed": "Abfrage fehlgeschlagen (erneut versuchen)",
           "truncated": "⚠️ Dieser Abschnitt ist möglicherweise abgeschnitten: vor dem Übernehmen mit dem Original-SDB abgleichen.", "limits_not_found": "⚠️ In diesem Text wurden keine Grenzwerte gefunden: das heißt NICHT, dass es keine gibt. Abschnitt 8.1 im Original-SDB prüfen.",
           "missing": "Für jeden Bestandteil, der als nicht entworfen oder nicht identifiziert aufgeführt ist: das SDB dieses Lieferanten mit `upload_msds_pdf` hochladen oder eine CAS-Nummer angeben und erneut entwerfen."},
    "id": {"title": "Draf SDS",
           "no_regional": "⚠️ Kami tidak memiliki daftar regulasi untuk {region}: hanya konvensi internasional yang diperiksa. Tidak ada di bawah ini yang merupakan temuan tentang {region}.", "s8": "Bagian 8 — Pengendalian paparan / perlindungan diri",
           "s15": "Bagian 15 — Informasi regulasi", "source": "Sumber",
           "revision": "revisi", "pdf": "PDF asli tersedia",
           "not_drafted": "Tidak didraf", "s15_basis": "Dasar: daftar regulasi yang kami miliki untuk",
           "lists_checked": "Daftar yang diperiksa", "listed_on": "Tercantum di",
           "not_listed": "Tidak ada di daftar yang diperiksa",
           "not_checked": "⚠️ Tidak dapat diperiksa (sumber daftar tidak terbaca) — ini BUKAN \"tidak tercantum\"",
           "lists_unreadable": "⚠️ Daftar regulasi tidak dapat dibaca, sehingga TIDAK ADA yang diperiksa untuk Bagian 15. Ini bukan temuan bahwa bahan-bahan tersebut tidak tercantum.",
           "unresolved": "Tidak teridentifikasi", "lookup_failed": "Pencarian gagal (coba lagi)",
           "truncated": "⚠️ Bagian ini mungkin terpotong: periksa SDS asli sebelum menyalinnya.", "limits_not_found": "⚠️ Tidak ada nilai batas paparan di teks ini: itu BUKAN berarti tidak ada. Periksa 8.1 di SDS asli.",
           "missing": "Untuk setiap bahan yang tercantum sebagai tidak didraf atau tidak teridentifikasi: unggah SDS pemasok tersebut dengan `upload_msds_pdf`, atau berikan nomor CAS, lalu draf ulang."},
    'ko': {'title': 'SDS 초안',
     'no_regional': '⚠️ {region}에 대한 규제 목록은 보유하고 있지 않습니다: 국제 협약만 확인했습니다. 아래 내용은 {region}에 대한 결론이 아닙니다.',
     's8': '제8항: 노출 방지 및 개인 보호구',
     's15': '제15항: 법적 규제 현황',
     'source': '출처',
     'revision': '개정',
     'pdf': 'SDS 원본(PDF) 보유',
     'not_drafted': '초안 작성 안 됨',
     's15_basis': '근거: 당사가 보유한 규제 목록, 지역',
     'lists_checked': '확인한 목록',
     'listed_on': '등재 목록',
     'not_listed': '확인한 목록에 없음',
     'not_checked': '⚠️ 확인할 수 없음(목록 데이터 원본을 읽을 수 없음): "등재되지 않음"이라는 뜻이 아닙니다',
     'lists_unreadable': '⚠️ 규제 목록을 읽을 수 없어 제15항은 아무것도 확인하지 않았습니다. 성분이 어떤 목록에도 없다는 뜻이 아닙니다.',
     'unresolved': '식별되지 않음',
     'lookup_failed': '조회 실패(다시 시도하세요)',
     'truncated': '⚠️ 이 부분은 잘렸을 수 있습니다: 옮겨 적기 전에 원본 SDS를 확인하세요.',
     'limits_not_found': '⚠️ 이 본문에서 노출 한계값을 찾지 못했습니다: 한계값이 없다는 뜻이 아닙니다. 원본 SDS의 8.1을 확인하세요.',
     'missing': '초안 작성 안 됨 또는 식별되지 않음으로 표시된 각 성분은 `upload_msds_pdf`로 해당 공급업체의 SDS를 업로드하거나 CAS 번호를 지정한 '
                '뒤 다시 초안을 작성하세요.'},
    'ru': {'title': 'Черновик SDS',
     'no_regional': '⚠️ У нас нет регуляторных списков для {region}: проверены только международные '
                    'конвенции. Ничто ниже не является выводом о {region}.',
     's8': 'Раздел 8. Контроль воздействия / средства индивидуальной защиты',
     's15': 'Раздел 15. Информация о национальном и международном законодательстве',
     'source': 'Источник',
     'revision': 'редакция',
     'pdf': 'есть оригинал SDS (PDF)',
     'not_drafted': 'Не подготовлено',
     's15_basis': 'Основание: имеющиеся у нас регуляторные списки для',
     'lists_checked': 'Проверенные списки',
     'listed_on': 'Включено в',
     'not_listed': 'Нет в проверенных списках',
     'not_checked': '⚠️ Проверить не удалось (источник списков недоступен): это НЕ означает «не '
                    'включено»',
     'lists_unreadable': '⚠️ Регуляторные списки не удалось прочитать, поэтому для раздела 15 НИЧЕГО '
                         'не проверено. Это не означает, что компоненты не включены ни в один список.',
     'unresolved': 'Не идентифицировано',
     'lookup_failed': 'Ошибка запроса (повторите попытку)',
     'truncated': '⚠️ Этот фрагмент может быть обрезан: сверьтесь с оригиналом SDS перед копированием.',
     'limits_not_found': '⚠️ В этом тексте не найдено предельных значений воздействия: это НЕ '
                         'означает, что их нет. Проверьте п. 8.1 в оригинале SDS.',
     'missing': 'Для каждого компонента, отмеченного как не подготовленный или не идентифицированный: '
                'загрузите SDS этого поставщика через `upload_msds_pdf` или укажите номер CAS и '
                'подготовьте проект заново.'},
    'fr': {'title': 'Brouillon de FDS',
     'no_regional': '⚠️ Nous ne détenons aucune liste réglementaire pour {region} : seules les '
                    "conventions internationales ont été vérifiées. Rien ci-dessous n'est une "
                    'conclusion concernant {region}.',
     's8': "Section 8 : Contrôles de l'exposition / protection individuelle",
     's15': 'Section 15 : Informations relatives à la réglementation',
     'source': 'Source',
     'revision': 'révision',
     'pdf': 'PDF original disponible',
     'not_drafted': 'Non rédigé',
     's15_basis': 'Base : listes réglementaires que nous détenons pour',
     'lists_checked': 'Listes vérifiées',
     'listed_on': 'Inscrit sur',
     'not_listed': 'Absent des listes vérifiées',
     'not_checked': '⚠️ Vérification impossible (source de la liste illisible) : cela ne signifie PAS '
                    '« non inscrit »',
     'lists_unreadable': "⚠️ Les listes réglementaires n'ont pas pu être lues, donc RIEN n'a été "
                         'vérifié pour la section 15. Cela ne signifie pas que les composants ne '
                         'figurent sur aucune liste.',
     'unresolved': 'Non identifié',
     'lookup_failed': 'Échec de la recherche (réessayer)',
     'truncated': '⚠️ Ce passage est peut-être tronqué : vérifiez la FDS originale avant de le '
                  'recopier.',
     'limits_not_found': "⚠️ Aucune valeur limite d'exposition trouvée dans ce texte : cela ne "
                         "signifie PAS qu'il n'y en a pas. Vérifiez la section 8.1 de la FDS "
                         'originale.',
     'missing': 'Pour chaque composant indiqué comme non rédigé ou non identifié : téléversez la FDS '
                'de ce fournisseur avec `upload_msds_pdf`, ou indiquez un numéro CAS, puis relancez la '
                'rédaction.'},
    'es': {'title': 'Borrador de FDS',
     'no_regional': '⚠️ No disponemos de listas regulatorias para {region}: solo se verificaron '
                    'convenios internacionales. Nada de lo siguiente es una conclusión sobre {region}.',
     's8': 'Sección 8: Controles de exposición / protección individual',
     's15': 'Sección 15: Información reglamentaria',
     'source': 'Fuente',
     'revision': 'revisión',
     'pdf': 'PDF original disponible',
     'not_drafted': 'No redactado',
     's15_basis': 'Base: listas regulatorias que tenemos para',
     'lists_checked': 'Listas verificadas',
     'listed_on': 'Incluido en',
     'not_listed': 'No figura en las listas verificadas',
     'not_checked': '⚠️ No se pudo verificar (la fuente de la lista no es legible): esto NO significa '
                    '«no incluido»',
     'lists_unreadable': '⚠️ No se pudieron leer las listas regulatorias, así que NO se verificó nada '
                         'para la sección 15. Esto no significa que los componentes no figuren en '
                         'ninguna lista.',
     'unresolved': 'No identificado',
     'lookup_failed': 'Error en la consulta (reintente)',
     'truncated': '⚠️ Este fragmento puede estar cortado: compruebe la FDS original antes de copiarlo.',
     'limits_not_found': '⚠️ No se encontraron valores límite de exposición en este texto: eso NO '
                         'significa que no existan. Compruebe la sección 8.1 de la FDS original.',
     'missing': 'Para cada componente indicado como no redactado o no identificado: suba la FDS de ese '
                'proveedor con `upload_msds_pdf`, o indique un número CAS, y vuelva a redactar.'},
}


def _format_sds_draft(data: dict, lang: str | None = None) -> str:
    """Render `/api/v2/sds-draft` (CI-1138). Pure function, testable without a backend.

    🔴 Three rendering red lines — the backend guarantees the data; a template that
    drops any of them turns a draft into a false assurance:
      ① §15 `lists_checked is None` means the lists could NOT be read — never render
         it as "not on any list" (same family as CI-507).
      ② §15 lines come from government lists, not the supplier SDS: the `scope_note`
         and the basis line say so, and §8's per-line source is the supplier SDS.
      ③ `not_drafted` / `unresolved` / `lookup_failed` are listed to the user: they
         are the "what is missing, upload it" entry point, not noise to hide.
    `notice` goes first and verbatim: this is a draft, not a compliant SDS.
    """
    lg = _normalize_catalog_lang(lang or LANG)
    s = _SDS_DRAFT_STRINGS.get(lg, _SDS_DRAFT_STRINGS["en"])

    def flat(v) -> str:
        # Names and concentrations are the caller's own text, echoed into headings:
        # one line, capped, so they cannot forge a heading or a source line.
        return " ".join(str(v).split())[:120]

    def who(item: dict) -> str:
        name = flat(item.get("chemical") or "?")
        if item.get("cas"):
            name += f" (CAS {flat(item['cas'])})"
        if item.get("concentration"):
            name += f" — {flat(item['concentration'])}"
        return name

    lines = [f"**{s['title']}**\n"]
    if data.get("notice"):
        lines.append(f"> {data['notice']}\n")
    sections = data.get("sections") or {}
    gaps = False

    s8 = sections.get("8")
    if s8 is not None:
        lines.append(f"## {s['s8']}\n")
        for item in s8.get("drafted") or []:
            c = item.get("citation") or {}
            src = ", ".join(str(x) for x in (
                c.get("supplier"),
                f"{s['revision']} {c['revision_date']}" if c.get("revision_date") else None,
                c.get("region"),
            ) if x)
            lines.append(f"### {who(item)}")
            lines.append(f"*{s['source']}: {src or '—'} · {s['pdf']}*")
            if item.get("physical_form_disclosure"):
                lines.append(f"> {item['physical_form_disclosure']}")
            # Per-row, above the text: users copy blocks one at a time into their own
            # SDS, so a warning parked in a summary would be read after the copy.
            if item.get("possibly_truncated"):
                lines.append(f"> {item.get('truncation_note') or s['truncated']}")
            if item.get("exposure_limits_in_text") == "not_found":
                lines.append(f"> {item.get('exposure_limits_note') or s['limits_not_found']}")
            body = (item.get("content") or "").strip()
            fence = "`" * max(3, 1 + max((len(r) for r in re.findall(r"`+", body)), default=0))
            lines.append(f"{fence}\n{body}\n{fence}\n")
        not_drafted = s8.get("not_drafted") or []
        if not_drafted:
            gaps = True
            lines.append(f"**{s['not_drafted']}:**")
            lines.extend(f"- {who(i)}: {i.get('note') or i.get('reason') or '—'}"
                         for i in not_drafted)
            lines.append("")

    s15 = sections.get("15")
    if s15 is not None:
        region = s15.get("region") or "—"
        lines.append(f"## {s['s15']} ({region})\n")
        checked = s15.get("lists_checked")
        regional = checked is not None and any(
            (e.get("region") or "").upper() == region.upper() for e in checked)
        if checked is not None and not regional:
            # Only the international conventions were checked: "not on the lists
            # checked" below must not be read as a finding for this region.
            lines.append(s["no_regional"].format(region=region))
        else:
            lines.append(f"*{s['s15_basis']} {region}.*")
        if checked is None:
            lines.append(s["lists_unreadable"])
        else:
            names = [e.get("list") for e in checked if e.get("list")]
            lines.append(f"**{s['lists_checked']}:** {'; '.join(names) or '—'}\n")
            for ing in s15.get("ingredients") or []:
                if ing.get("lists_unavailable"):
                    status = s["not_checked"]
                elif ing.get("listed_on"):
                    status = f"{s['listed_on']}: " + "; ".join(
                        e.get("list") or "?" for e in ing["listed_on"])
                else:
                    status = s["not_listed"]
                lines.append(f"- **{who(ing)}**: {status}")
        if s15.get("scope_note"):
            lines.append(f"\n> {s15['scope_note']}\n")

    unresolved = data.get("unresolved") or []
    if unresolved:
        gaps = True
        lines.append(f"**{s['unresolved']}:**")
        for u in unresolved:
            d = u.get("unresolved_detail") or {}
            # 取哪一半，理由同 `_unresolved_reason_note`。
            reason = (d.get("reason") if lg in _REASON_LOCALIZED_LANGS
                      else (d.get("reason_en") or d.get("reason")))
            reason = reason or _unresolved_boolean_note(lg)
            # The backend's reason already opens with the query; don't print it twice.
            lines.append(f"- {reason}" if (u.get("chemical") or "\0") in reason
                         else f"- {who(u)}: {reason}")
        lines.append("")
    failed = data.get("lookup_failed") or []
    if failed:
        gaps = True
        lines.append(f"**{s['lookup_failed']}:** " + "; ".join(who(f) for f in failed) + "\n")
    if gaps:
        lines.append(s["missing"])
    return "\n".join(lines).rstrip()


_SDS_QUALITY_STRINGS = {'en': {'title': 'SDS quality check',
        'revision': 'revision',
        'revision_unknown': 'revision date not found',
        'contradiction': 'Contradiction',
        'missing': 'Missing',
        'outdated': 'Outdated',
        'format': 'Format',
        'evidence': 'Original text',
        'no_issues': 'No issues found by these checks. This does not mean the SDS is compliant.',
        'skipped': 'Not checked (an input could not be read from this SDS)',
        'newer': 'A newer revision of this SDS is in our library: {date} ({supplier}).',
        'summary': '{n} SDS checked · {c} with a contradiction · {i} with any issue',
        'truncated': 'This session holds more uploads than one call checks: only the most recent '
                     'ones are above. Check older ones one at a time with `msds_id`.',
        'empty': 'No uploaded SDS in this session. Upload one with `upload_msds_pdf`, then check '
                 'it here.',
        'not_found': 'That SDS was not found in this session. Pass the `session_id` and `msds_id` '
                     'that `upload_msds_pdf` returned (only SDSs you uploaded can be checked).'},
 'zh': {'title': 'SDS 质量检查',
        'revision': '修订',
        'revision_unknown': '未找到修订日期',
        'contradiction': '矛盾',
        'missing': '缺项',
        'outdated': '过期',
        'format': '格式',
        'evidence': '原文',
        'no_issues': '这些检查没有发现问题。这不代表这份 SDS 合规。',
        'skipped': '未检查（这份 SDS 里读不到所需的内容）',
        'newer': '我们的库里有这份 SDS 的更新版本：{date}（{supplier}）。',
        'summary': '检查了 {n} 份 SDS · {c} 份有矛盾 · {i} 份有问题',
        'truncated': '这个会话里的上传超过一次调用能检查的数量：上面只有最近的那些。更早的请用 `msds_id` 逐份检查。',
        'empty': '这个会话里没有上传的 SDS。先用 `upload_msds_pdf` 上传，再在这里检查。',
        'not_found': '这个会话里没有这份 SDS。请传入 `upload_msds_pdf` 返回的 `session_id` 和 `msds_id`（只能检查你自己上传的 '
                     'SDS）。'},
 'zh-TW': {'title': 'SDS 品質檢查',
           'revision': '修訂',
           'revision_unknown': '未找到修訂日期',
           'contradiction': '矛盾',
           'missing': '缺項',
           'outdated': '過期',
           'format': '格式',
           'evidence': '原文',
           'no_issues': '這些檢查沒有發現問題。這不代表這份 SDS 合規。',
           'skipped': '未檢查（這份 SDS 裡讀不到所需的內容）',
           'newer': '我們的庫裡有這份 SDS 的更新版本：{date}（{supplier}）。',
           'summary': '檢查了 {n} 份 SDS · {c} 份有矛盾 · {i} 份有問題',
           'truncated': '這個會話裡的上傳超過一次呼叫能檢查的數量：上面只有最近的那些。更早的請用 `msds_id` 逐份檢查。',
           'empty': '這個會話裡沒有上傳的 SDS。先用 `upload_msds_pdf` 上傳，再在這裡檢查。',
           'not_found': '這個會話裡沒有這份 SDS。請傳入 `upload_msds_pdf` 返回的 `session_id` 和 '
                        '`msds_id`（只能檢查你自己上傳的 SDS）。'},
 'ja': {'title': 'SDS 品質チェック',
        'revision': '改訂',
        'revision_unknown': '改訂日が見つかりません',
        'contradiction': '矛盾',
        'missing': '欠落',
        'outdated': '古い版',
        'format': '様式',
        'evidence': '原文',
        'no_issues': 'これらのチェックでは問題は見つかりませんでした。SDS が適合していることを意味するものではありません。',
        'skipped': '未チェック（この SDS から必要な内容を読み取れませんでした）',
        'newer': 'この SDS の新しい版が当社のライブラリにあります：{date}（{supplier}）。',
        'summary': 'SDS {n} 件をチェック · 矛盾あり {c} 件 · 指摘あり {i} 件',
        'truncated': 'このセッションのアップロード数が 1 回でチェックできる上限を超えています。上は最新分のみです。古いものは `msds_id` を指定して 1 '
                     '件ずつチェックしてください。',
        'empty': 'このセッションにはアップロードされた SDS がありません。`upload_msds_pdf` でアップロードしてから、ここでチェックしてください。',
        'not_found': 'このセッションにその SDS はありません。`upload_msds_pdf` が返した `session_id` と `msds_id` '
                     'を渡してください（チェックできるのは自分でアップロードした SDS のみです）。'},
 'de': {'title': 'SDB-Qualitätsprüfung',
        'revision': 'Revision',
        'revision_unknown': 'kein Revisionsdatum gefunden',
        'contradiction': 'Widerspruch',
        'missing': 'Fehlt',
        'outdated': 'Veraltet',
        'format': 'Form',
        'evidence': 'Originaltext',
        'no_issues': 'Diese Prüfungen haben nichts gefunden. Das heißt nicht, dass das SDB konform '
                     'ist.',
        'skipped': 'Nicht geprüft (eine Angabe war in diesem SDB nicht lesbar)',
        'newer': 'In unserer Bibliothek liegt eine neuere Revision dieses SDB: {date} '
                 '({supplier}).',
        'summary': '{n} SDB geprüft · {c} mit Widerspruch · {i} mit Befund',
        'truncated': 'Diese Sitzung enthält mehr Uploads, als ein Aufruf prüft: oben stehen nur '
                     'die neuesten. Ältere einzeln mit `msds_id` prüfen.',
        'empty': 'In dieser Sitzung wurde kein SDB hochgeladen. Erst mit `upload_msds_pdf` '
                 'hochladen, dann hier prüfen.',
        'not_found': 'Dieses SDB wurde in dieser Sitzung nicht gefunden. Übergeben Sie '
                     '`session_id` und `msds_id` aus der Antwort von `upload_msds_pdf` (nur selbst '
                     'hochgeladene SDB können geprüft werden).'},
 'id': {'title': 'Pemeriksaan kualitas SDS',
        'revision': 'revisi',
        'revision_unknown': 'tanggal revisi tidak ditemukan',
        'contradiction': 'Kontradiksi',
        'missing': 'Tidak ada',
        'outdated': 'Kedaluwarsa',
        'format': 'Format',
        'evidence': 'Teks asli',
        'no_issues': 'Pemeriksaan ini tidak menemukan masalah. Ini tidak berarti SDS tersebut '
                     'patuh.',
        'skipped': 'Tidak diperiksa (ada isi yang tidak dapat dibaca dari SDS ini)',
        'newer': 'Revisi yang lebih baru dari SDS ini ada di pustaka kami: {date} ({supplier}).',
        'summary': '{n} SDS diperiksa · {c} dengan kontradiksi · {i} dengan temuan',
        'truncated': 'Sesi ini berisi lebih banyak unggahan daripada yang diperiksa satu '
                     'panggilan: di atas hanya yang terbaru. Periksa yang lebih lama satu per satu '
                     'dengan `msds_id`.',
        'empty': 'Tidak ada SDS yang diunggah di sesi ini. Unggah dengan `upload_msds_pdf`, lalu '
                 'periksa di sini.',
        'not_found': 'SDS itu tidak ditemukan di sesi ini. Berikan `session_id` dan `msds_id` yang '
                     'dikembalikan `upload_msds_pdf` (hanya SDS yang Anda unggah sendiri yang '
                     'dapat diperiksa).'},
 'ko': {'title': 'SDS 품질 점검',
        'revision': '개정',
        'revision_unknown': '개정일을 찾지 못함',
        'contradiction': '모순',
        'missing': '누락',
        'outdated': '오래됨',
        'format': '형식',
        'evidence': '원문',
        'no_issues': '이 점검에서는 문제가 발견되지 않았습니다. SDS가 적합하다는 뜻은 아닙니다.',
        'skipped': '점검 안 함 (이 SDS에서 필요한 내용을 읽지 못함)',
        'newer': '이 SDS의 최신 개정본이 라이브러리에 있습니다: {date} ({supplier}).',
        'summary': 'SDS {n}건 점검 · 모순 {c}건 · 지적 {i}건',
        'truncated': '이 세션의 업로드가 한 번에 점검할 수 있는 수를 넘습니다. 위에는 최신 것만 있습니다. 이전 것은 `msds_id`로 한 건씩 '
                     '점검하세요.',
        'empty': '이 세션에는 업로드된 SDS가 없습니다. `upload_msds_pdf`로 업로드한 뒤 여기서 점검하세요.',
        'not_found': '이 세션에서 해당 SDS를 찾지 못했습니다. `upload_msds_pdf`가 반환한 `session_id`와 `msds_id`를 '
                     '전달하세요 (직접 업로드한 SDS만 점검할 수 있습니다).'},
 'ru': {'title': 'Проверка качества паспорта безопасности',
        'revision': 'редакция',
        'revision_unknown': 'дата редакции не найдена',
        'contradiction': 'Противоречие',
        'missing': 'Отсутствует',
        'outdated': 'Устарело',
        'format': 'Форма',
        'evidence': 'Исходный текст',
        'no_issues': 'Эти проверки ничего не нашли. Это не означает, что паспорт соответствует '
                     'требованиям.',
        'skipped': 'Не проверено (не удалось прочитать нужные данные из паспорта)',
        'newer': 'В нашей библиотеке есть более новая редакция этого паспорта: {date} '
                 '({supplier}).',
        'summary': 'Проверено паспортов: {n} · с противоречием: {c} · с замечаниями: {i}',
        'truncated': 'В этой сессии больше загрузок, чем проверяет один вызов: выше только '
                     'последние. Более ранние проверяйте по одному через `msds_id`.',
        'empty': 'В этой сессии нет загруженных паспортов. Загрузите через `upload_msds_pdf`, '
                 'затем проверьте здесь.',
        'not_found': 'Этот паспорт не найден в данной сессии. Передайте `session_id` и `msds_id`, '
                     'которые вернул `upload_msds_pdf` (проверить можно только паспорта, '
                     'загруженные вами).'},
 'fr': {'title': 'Contrôle qualité de la FDS',
        'revision': 'révision',
        'revision_unknown': 'date de révision introuvable',
        'contradiction': 'Contradiction',
        'missing': 'Manquant',
        'outdated': 'Obsolète',
        'format': 'Forme',
        'evidence': 'Texte original',
        'no_issues': "Ces contrôles n'ont rien trouvé. Cela ne signifie pas que la FDS est "
                     'conforme.',
        'skipped': "Non contrôlé (une donnée n'a pas pu être lue dans cette FDS)",
        'newer': 'Une révision plus récente de cette FDS figure dans notre bibliothèque : {date} '
                 '({supplier}).',
        'summary': '{n} FDS contrôlées · {c} avec contradiction · {i} avec constat',
        'truncated': "Cette session contient plus de téléversements qu'un appel n'en contrôle : "
                     'seuls les plus récents figurent ci-dessus. Contrôlez les plus anciens un par '
                     'un avec `msds_id`.',
        'empty': 'Aucune FDS téléversée dans cette session. Téléversez-en une avec '
                 '`upload_msds_pdf`, puis contrôlez-la ici.',
        'not_found': 'Cette FDS est introuvable dans cette session. Passez le `session_id` et le '
                     '`msds_id` renvoyés par `upload_msds_pdf` (seules les FDS que vous avez '
                     'téléversées peuvent être contrôlées).'},
 'es': {'title': 'Control de calidad de la FDS',
        'revision': 'revisión',
        'revision_unknown': 'fecha de revisión no encontrada',
        'contradiction': 'Contradicción',
        'missing': 'Falta',
        'outdated': 'Desactualizada',
        'format': 'Formato',
        'evidence': 'Texto original',
        'no_issues': 'Estas comprobaciones no encontraron nada. Eso no significa que la FDS sea '
                     'conforme.',
        'skipped': 'No comprobado (no se pudo leer un dato de esta FDS)',
        'newer': 'En nuestra biblioteca hay una revisión más reciente de esta FDS: {date} '
                 '({supplier}).',
        'summary': '{n} FDS comprobadas · {c} con contradicción · {i} con hallazgos',
        'truncated': 'Esta sesión tiene más subidas de las que comprueba una llamada: arriba solo '
                     'están las más recientes. Compruebe las anteriores una a una con `msds_id`.',
        'empty': 'No hay ninguna FDS subida en esta sesión. Súbala con `upload_msds_pdf` y '
                 'compruébela aquí.',
        'not_found': 'Esa FDS no está en esta sesión. Pase el `session_id` y el `msds_id` que '
                     'devolvió `upload_msds_pdf` (solo se pueden comprobar las FDS que usted '
                     'subió).'}}


def _format_sds_quality(data: dict, lang: str | None = None) -> str:
    """Render `/api/v2/validate-sds` and `/validate-sds/session` (CI-1444). Pure function.

    🔴 Three rendering red lines — the backend guarantees the data; a template that
    drops any of them turns a check into a false assurance:
      ① `notice` goes first and verbatim: no issue found ≠ compliant.
      ② every issue carries its `evidence` lines, verbatim: the finding is a pointer
         into the user's own document, and without the quote it cannot be checked.
      ③ `checks_skipped` is said out loud: a check that could not read its input
         found nothing because it did not look, which is not the same as "passed".
    Issue messages are already localised by the backend; this side only adds labels.

    A body without the fields these red lines need raises instead of rendering: a
    missing `issues` would otherwise print as "no issues found".
    """
    _sds_quality_shape(data)
    lg = _normalize_catalog_lang(lang or LANG)
    s = _SDS_QUALITY_STRINGS.get(lg, _SDS_QUALITY_STRINGS["en"])

    def flat(v, cap: int = 300) -> str:
        # Document text (names, quoted lines) is the uploader's: one line, capped,
        # so a crafted SDS cannot forge a heading or a verdict line. A cut is marked:
        # an evidence quote is presented as the original, so a silent cut would send
        # the user looking for a string that is not in their PDF.
        v = " ".join(str(v).split())
        return v if len(v) <= cap else v[:cap] + "…"

    def one(doc_result: dict) -> list[str]:
        d = doc_result.get("document") or {}
        head = flat(d.get("chemical_name") or "?")
        if d.get("supplier"):
            head += f" — {flat(d['supplier'])}"
        rev = d.get("revision_date")
        head += f" ({s['revision']} {flat(rev)})" if rev else f" ({s['revision_unknown']})"
        out = [f"### {head}", f"`msds_id`: {d.get('msds_id')}"]
        issues = doc_result["issues"]
        # "No issues" only when at least one check actually looked: with every check
        # skipped there is nothing to report either way, and the skipped line says so.
        if not issues and doc_result.get("checks_run"):
            out.append(f"- {s['no_issues']}")
        for i in issues:
            raw = i.get("category") or "?"
            cat = s[raw] if raw in _SDS_QUALITY_CATEGORIES else flat(raw)
            secs = " / ".join(f"§{n}" for n in i.get("sections") or [])
            out.append(f"- **[{cat}]**{' ' + secs if secs else ''} {flat(i.get('message') or '', 1000)}")
            for line in i.get("evidence") or []:
                out.append(f"  > {s['evidence']}: \"{flat(line, 1000)}\"")
        if newer := doc_result.get("newer_version"):
            out.append("- " + s["newer"].format(date=flat(newer.get("revision_date") or "?"),
                                                supplier=flat(newer.get("supplier") or "?")))
        if skipped := doc_result.get("checks_skipped"):
            # Rule ids (`check_signal_word`), shown as words; the model can relay them.
            names = ", ".join(flat(n).removeprefix("check_").replace("_", " ") for n in skipped)
            out.append(f"- {s['skipped']}: {names}")
        return out

    lines = [f"**{s['title']}**\n", f"> {data['notice']}\n"]
    if "documents" in data:
        docs = data["documents"]
        if not docs:
            lines.append(s["empty"])
            return "\n".join(lines)
        summ = data.get("summary") or {}
        lines.append(s["summary"].format(n=summ.get("documents", len(docs)),
                                         c=summ.get("with_contradictions", 0),
                                         i=summ.get("with_issues", 0)) + "\n")
        for doc_result in docs:
            lines.extend(one(doc_result))
            lines.append("")
        if data.get("truncated"):
            lines.append(f"⚠️ {s['truncated']}")
    else:
        lines.extend(one(data))
    return "\n".join(lines).rstrip()


_SDS_QUALITY_CATEGORIES = frozenset({"contradiction", "missing", "outdated", "format"})


def _sds_quality_shape(data: dict) -> None:
    """Refuse a `/validate-sds*` body that lacks what the rendering red lines need."""
    def doc_ok(d) -> bool:
        return (isinstance(d, dict) and isinstance(d.get("issues"), list)
                and isinstance(d.get("document"), dict))
    ok = isinstance(data, dict) and isinstance(data.get("notice"), str) and data["notice"].strip()
    if ok and "documents" in data:
        ok = isinstance(data["documents"], list) and all(doc_ok(d) for d in data["documents"])
    elif ok:
        ok = doc_ok(data)
    if not ok:
        raise ToolReason("The SDS check returned an incomplete result, so nothing is "
                         "reported for it. Try again; this is not a finding about the SDS.")
