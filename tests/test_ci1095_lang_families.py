"""CI-1095：`lang` 分两族，以及「放开 catalog 族之后本模块自己的文案表不许落队」。

## 两族是什么

- **catalog 族**（`/api/v2` 的确定性端点）：文案由后端按 i18n 表渲染，五种语言都是静态
  译好的 ⇒ `_normalize_catalog_lang` 放行 `en/zh/ja/de/id`。
- **quick-chat 族**（答案由 LLM 现写）：语言靠 prompt 不靠模板，实测会偶发把中文混进
  正文，而**这一族不播 `language_note`** ⇒ 用户拿到错语言时没有任何信号 ⇒
  `_normalize_lang` 仍只放行 `en/zh`。

🔴 **别把两者合并**。它们今天的差别是支持集合，但**失败方式不同**：catalog 缺译文会
回英文**并披露**，quick-chat 回错语言是**静默的**。合并等于把一族的实测结论套到另一族。

## 这个文件真正守的那件事

放开 catalog 族之后，后端会回日文/德文/印尼文，而**本模块自己**那几张 `en`/`zh` 两值
文案表会原地不动 ⇒ 产出一种**新的混语言输出**，并且它**不在** CI-1123 那句
`language_note` 的覆盖里（那一句只描述后端自己的回退）⇒ 用户看不出哪半是我们没翻。
review 抓到的，不是我。

⇒ 判据写成「**语言表的覆盖集合 == `_CATALOG_LANGS`**」，而不是写死一张表名清单：
后来有人往 `_CATALOG_LANGS` 里加第六种语言时，**每一张落队的表都会在这里红**。

## 🔴 变异记录（没记变异的守卫默认当它不存在）

- 从 `_REG_LIST_STRINGS` / `_UNRESOLVED_BOOLEAN_NOTE` / `_REG_LIST_COVERAGE_NOTE`
  任意一张里删掉 `"ja"` ⇒ `test_every_language_table_covers_the_catalog_set` 红并点名那张表。
- 往 `_CATALOG_LANGS` 里加一个 `"fr"`（**集合变大**方向的变异，不是「撤回改动」）
  ⇒ 同一条红，且**六张表全部**点名 —— 这正是它要防的那件事。
- 把 `_REG_LIST_STRINGS["ja"]` 整个换成 `_REG_LIST_STRINGS["en"]` 的副本
  ⇒ `test_translations_are_not_english_copies` 红（「槽位填了」与「真的翻了」同形）。
- 把 `_REG_LIST_COVERAGE_NOTE["ja"]` 里的「台湾」删掉 ⇒ `test_coverage_note_names_taiwan_and_iarc_in_every_language` 红。
- 把 `_REG_LIST_COVERAGE_NOTE["de"]` 改回「enthält keine Daten zu Taiwan」（去掉 MOENV，**仍含 Taiwan**）
  ⇒ 同一条红（CI-1150：「没有台湾」与「只有那一份」都含 Taiwan 这个词，只靠它分不开）。
- 把 `_REG_LIST_COVERAGE_NOTE["id"]` 里的「(TCSI)」删掉 ⇒ 同一条红（CI-1164：台湾清单变多之后，
  唯一还得原样说出来的「缺口」就是没有名录）。

🔴 **本文件不测「翻得好不好」**，只测三件机械的事：覆盖集合、不是英文副本、
以及那条限定说明在每种语言里都点名了台湾与 IARC。措辞质量要人读，这里给不了。
"""
import server

# 🔴 **自己发现成员**：按「模块级、dict、键里同时有 en 和 zh」扫，而不是手抄表名。
# 新表自动进来；漏掉一张的代价是「它一直是绿的」，那正是本文件要防的形状。
def _language_tables() -> dict[str, dict]:
    out = {}
    for name in dir(server):
        value = getattr(server, name)
        if (isinstance(value, dict) and value
                and all(isinstance(k, str) for k in value)
                and {"en", "zh"} <= set(value)):
            out[name] = value
    return out


def test_the_discovery_actually_finds_tables():
    """🔴 阳性对照：下面几条都是「没找到就绿」。

    发现器哪天因为改名/搬家返回空，它们会**一起变成绿的空跑**，而那与「全都合规」
    完全同形。这条钉住「至少找到了那几张我们知道存在的表」。
    """
    tables = _language_tables()
    assert len(tables) >= 4, f"只发现了 {len(tables)} 张语言表：{sorted(tables)} —— 先查发现器"
    for expected in ("_REG_LIST_STRINGS", "_UNRESOLVED_BOOLEAN_NOTE",
                     "_REG_LIST_COVERAGE_NOTE"):
        assert expected in tables, f"{expected} 没被发现器扫到"


def test_every_language_table_covers_the_catalog_set():
    """catalog 族放行几种语言，本模块的文案表就要有几种。"""
    want = set(server._CATALOG_LANGS)
    missing = {name: sorted(want - set(table))
               for name, table in _language_tables().items() if want - set(table)}
    assert not missing, (
        f"这些文案表没跟上 `_CATALOG_LANGS`：{missing}。"
        f"后端已经会用这些语言作答，而本模块的包装文字还停在旧集合 ⇒ 产出混语言输出，"
        f"且它不在 `language_note` 的覆盖里（那一句只说后端自己的回退）。")


def test_nested_tables_have_the_same_keys_in_every_language():
    """嵌套表（如 `_REG_LIST_STRINGS`）漏一个子键 ⇒ 渲染出一个英文标签夹在译文里，
    而 `.get()` 兜底会让它**不报错**。"""
    bad = {}
    for name, table in _language_tables().items():
        shapes = {lang: tuple(sorted(v)) for lang, v in table.items()
                  if isinstance(v, dict)}
        if len(set(shapes.values())) > 1:
            base = shapes.get("en")
            bad[name] = {lang: sorted(set(base) - set(k))
                         for lang, k in shapes.items() if k != base}
    assert not bad, f"这些嵌套表的子键在各语言间不一致：{bad}"


def test_translations_are_not_english_copies():
    """🔴 「槽位填了」与「真的翻了」在覆盖率检查里完全同形。

    把英文原样贴进 `ja` 槽位能让上一条全绿，而用户拿到的仍是英文 —— 只是现在**连那句
    `language_note` 都不会播**（后端认为它已经用目标语言答了）。所以要单独钉一条。
    """
    copies = []
    for name, table in _language_tables().items():
        en = table.get("en")
        for lang in ("ja", "de", "id"):
            if lang in table and table[lang] == en:
                copies.append(f"{name}[{lang}]")
    assert not copies, (
        f"这些槽位是英文原样副本：{copies}。覆盖率检查看不出这一类 —— "
        f"它比缺失更糟，因为缺失至少会回退并披露。")


def test_coverage_note_names_taiwan_and_iarc_in_every_language():
    """🔴 前向红线（CI-523）：营销面可以模糊，**runtime 必须精确**。

    这条限定说明的全部作用是「没命中 ≠ 不受监管」+「台湾只有环境部列管毒化物那一份、
    没有 IARC」。翻译时丢掉任何一半，都会把一句限定说明变成一句担保。

    CI-1150 起台湾从「没有」变成「只有一份」⇒ 每种语言还必须点名**那一份**（MOENV /
    环境部 / 環境部）；只剩「Taiwan」这个词时，写回「不含台湾」也能过，而那句在
    TW 数据上线后是假话（真实调用会同时返回台湾清单命中）。

    CI-1164 起台湾有四份限制清单 + 一份暴露标准 ⇒ 剩下必须原样点名的缺口是**没有名录**，
    用 `TCSI` 这个各语言都不翻的缩写钉住它（写成「不含台湾清单」之类的退化句会掉它）。
    """
    bad = []
    for lang, text in server._REG_LIST_COVERAGE_NOTE.items():
        has_taiwan = any(token in text for token in ("Taiwan", "台湾", "台灣"))
        names_tw_list = any(token in text for token in ("MOENV", "环境部", "環境部"))
        if "IARC" not in text or "TCSI" not in text or not has_taiwan or not names_tw_list:
            bad.append(lang)
    assert not bad, (
        f"这些语言的覆盖范围说明没点名台湾（及环境部清单）、TCSI 或 IARC：{bad} —— "
        f"见 CI-523 / CI-1150 / CI-1164，这些名字和那句「没命中 ≠ 不受监管」要一起搬。")


def test_unresolved_note_is_present_in_every_catalog_language():
    """CI-714 那句「我们没解析出来 ≠ 库里没有」在每种语言里都要有，且非空。

    🔴 极性是这句话的全部。本条只能机械地保证「有一句、且不是英文副本」
    （后者由 `test_translations_are_not_english_copies` 覆盖）——
    **极性本身要人读**，所以措辞旁边留了注释说明它在说什么。
    """
    for lang in server._CATALOG_LANGS:
        text = server._UNRESOLVED_BOOLEAN_NOTE.get(lang)
        assert text and text.strip(), f"{lang} 缺这句话"


def test_the_two_families_stay_separate():
    """两族的归一化必须是两个函数、两个集合。

    合并成一个（或让 `_normalize_catalog_lang` 直接读 `_BACKEND_LANGS`）会把
    quick-chat 的保守集合套到 catalog 上、或反过来 —— 前者悄悄退回 CI-1095 之前，
    后者让 LLM 那条路开始偶发回中文。两个方向都不报错。
    """
    assert server._BACKEND_LANGS == ("en", "zh")
    assert set(server._BACKEND_LANGS) < set(server._CATALOG_LANGS), (
        "quick-chat 族必须是 catalog 族的真子集 —— 反过来说明有人把两族接反了")
    assert server._normalize_lang("ja") == "en"
    assert server._normalize_catalog_lang("ja") == "ja"
    # 两族都不认的值一律英文（这是本文件之外那条更老的不变式，顺手锚住）
    assert server._normalize_lang("fr") == "en"
    assert server._normalize_catalog_lang("fr") == "en"
