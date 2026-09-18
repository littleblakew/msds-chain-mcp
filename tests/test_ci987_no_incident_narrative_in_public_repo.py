"""CI-987：这是个 **public repo**，注释里不许写「哪天被撞到」的事故叙事。

泄的不是语料（那由 `test_ci906_no_real_corpus_in_fixtures.py` 守着），是**我们哪里弱、
哪天被撞到、被谁撞到、量有多小**。收敛前 `server.py` 里有：逐工具的 Prod 延迟表与调用量
（那张表顺带把我们的总调用量暴露成两位数）、一条带**人名 + 精确到分钟 + 渠道**的事故、
「我们最深的那个用户某天两次踩到、而我们看不见」、以及「远程流量全部来自某一个客户端」。
🔴 **这一段本身也不许带真数字**：一个讲「别公布这些」的文件，原样抄着那些数字就等于
在公布它们。举例一律用占位符，下面的变异用例同理。

🔴 **这同时是 CLAUDE.md 那条「注释不写历史」**：两个目标一次达成。

## 判据为什么是「日期 ∧ 叙事词」而不是单看日期

单看日期会误伤三类**合法**用法：公开 spec 的版本日期、工具描述里给用户看的示例串
（`"Grignard prep — 2026-04-16"`）、SDS 的修订日期。单看「实测/事故」又会漏掉
「Measured on Prod (…)」这种英文写法。⇒ **同一段注释里同时出现日期与叙事词**才算命中——
那正是「某天我们被撞到了」这种句子的形状。

🔴 **判据打在 `tokenize` 解析出的注释与 `ast` 解析出的 docstring 上**，不是文件文本：
代码里出现日期（比如构造一个 `revision_date` 字面量）不该让它红。

🔴 **变异（两侧，实测）**：
· 在任意注释里写下「2026-01-01 Prod 实测：X 坏了」⇒ 红
· 在**代码**里写 `revision_date = "2026-01-01"` ⇒ 仍绿
· 只写日期不写叙事词（spec 版本号那种）⇒ 仍绿

📌 **它防的是复发，不是一次性清理**：本票收敛当天，同一个人（我）在同一天里
往公开仓新加了 4 处这种叙事而毫无察觉。清理是一次性的，这道闸是长期的。
"""
import ast
import io
import pathlib
import re
import tokenize

import pytest

_DATE = re.compile(r"20\d\d-[01]\d-[0-3]\d")
# 叙事词：说的是「我们观测到/被撞到了什么」，而不是「这段代码做什么」
_NARRATIVE = re.compile(
    r"实测|实调|实录|事故|Prod evidence|Measured on Prod|Prod 实|线上实|复现于")

_ROOT = pathlib.Path(__file__).resolve().parent.parent
# 🔴 扫描面＝仓根的 `*.py` **加上 `tests/`**：测试文件是同一个公开仓的一部分，
# 而它们恰恰是叙事最密的地方（收敛当天在 `tests/` 里量到 23 个文件 / 27 处）。
# 🔴 **本文件自己排除掉**：它的 docstring 必须写下这个形状才说得清要防什么
# ——**自指的守卫**（用被测的模式描述被测的模式）。这一条排除是显式的、只此一个文件，
# 不是一张会腐化的豁免表。
# 🔴 `scripts/` 也在扫描面内，**它是 2026-09-18 补的，补之前那里是个洞**：
# 扫描面原本只有仓根 `*.py` + `tests/`，而 `scripts/` 恰恰躲开了——收敛当天没被扫的
# `mcp_surface_replay.py` 里同时留着语料内容描述、真实查询条数与网关限流参数。
# **一道只扫一部分目录的闸，在它没扫的那部分上与「那里很干净」完全同形。**
# 加新目录时连同下面 `test_the_guard_can_actually_see_something` 的计数一起看。
_TARGETS = sorted(
    p for p in (list(_ROOT.glob("*.py")) + list((_ROOT / "tests").glob("*.py"))
                + list((_ROOT / "scripts").rglob("*.py")))
    if p.name not in {"conftest.py"} and p.name != pathlib.Path(__file__).name
    and ".venv" not in p.parts)


def _comment_chunks(path: pathlib.Path):
    """(行号, 文本) —— 注释按**连续块**聚合：一段叙事通常跨好几行 `#`，
    日期在第一行、叙事词在第三行是常态，逐行判会漏掉整类。"""
    out, buf, start, prev = [], [], None, None
    with io.open(path, "rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type != tokenize.COMMENT:
                continue
            line = tok.start[0]
            if prev is not None and line != prev + 1:
                out.append((start, "\n".join(buf)))
                buf, start = [], None
            if start is None:
                start = line
            buf.append(tok.string)
            prev = line
    if buf:
        out.append((start, "\n".join(buf)))
    return out


def _docstrings(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                yield (getattr(node, "lineno", 1), doc)


@pytest.mark.parametrize("path", _TARGETS, ids=lambda p: p.name)
def test_no_dated_incident_narrative(path):
    hits = []
    for lineno, text in list(_comment_chunks(path)) + list(_docstrings(path)):
        if _DATE.search(text) and _NARRATIVE.search(text):
            first = next(l for l in text.splitlines() if l.strip())
            hits.append(f"{path.name}:{lineno} {first.strip()[:90]}")
    assert not hits, (
        "公开仓里出现了「带日期的事故/实测叙事」——它泄的是我们哪里弱、哪天被撞到：\n  "
        + "\n  ".join(hits)
        + "\n⇒ 只讲**机制**（什么形状的失败、判据是什么、别改回去什么），"
          "把日期/条数/人名/渠道/日志 id 留在私有票里。")


def test_the_guard_can_actually_see_something(tmp_path):
    """🔴 覆盖面自检：扫描面塌了（glob 没匹配到文件、tokenize 静默失败）时，
    上面那条会**因为什么都没扫而恒绿**，与「仓里很干净」完全同形。"""
    assert _TARGETS, "一个目标文件都没扫到"
    assert any(_comment_chunks(p) for p in _TARGETS), "一条注释都没解析出来"

    probe = tmp_path / "probe.py"
    probe.write_text('# 2026-01-01 Prod 实测：这条必须被抓到\nx = 1\n', encoding="utf-8")
    chunks = _comment_chunks(probe)
    assert chunks and _DATE.search(chunks[0][1]) and _NARRATIVE.search(chunks[0][1]), \
        "判据对一个已知阳性都不亮 —— 上面那条测试是空跑的"

    clean = tmp_path / "clean.py"
    clean.write_text('revision_date = "2026-01-01"  # 代码里的日期不该命中\n', encoding="utf-8")
    c = _comment_chunks(clean)
    assert not (c and _NARRATIVE.search(c[0][1])), "误伤：代码里的日期字面量被当成叙事"


# ---------------------------------------------------------------------------
# 第二条判据：**业务量披露**（不要求有日期）
# ---------------------------------------------------------------------------
# 🔴 为什么第一条不够：它是「日期 ∧ 叙事词」的**合取**，于是整类**不带日期**的句子
# 从它下面走过去了——而 CI-987 要防的四样里，「量有多小」恰恰通常不带日期：
#   「N 次外部调用里只有 M 次走某个工具」「实测 p50 X 秒」「可答率从 X% 升到 Y%
#   （Prod 全量 N 条）」「网关令牌桶容量 N、补充 R/s」「唯一深度活跃的真实 MCP 用户」
# 🔴 **这里和下面的用例一律用占位数字**：真数字写进来，这道闸就在公布它自己禁止的东西
# ——而且是写在一个专门讲「别公布这些」的文件里。收敛当天我第一版就是照抄真句子的。
# 这些比「哪天被撞到」更值钱：调用量说明我们多小，可答率/延迟是产品能力，
# 限流参数是可以直接拿去用的东西，「唯一那个用户」连用户基数都交代了。
#
# 🔴 **判据打在「业务量名词」上，不是「数字形状」上。** 第一版按数字形状判
# （百分比 / `N/M` / `p50`），当场误伤了一批**工程常量推导**：「3,000 条的列表逐条
# 重序列化要 7.7 秒 CPU」「砍 25% 会过度丢弃」「两条 incompatible 落在下标 150/188」
# ——那些恰恰是 CLAUDE.md 要求留下的「为什么这个常量是这个值」。
# ⇒ 改成**数字必须挨着一个业务量名词**（同段内 ±`_WINDOW` 字符），
# 于是 `25%` 不响、`可答率 … X%` 响。
# 🔴 **名词表必须窄。** 第二版用了 `调用|请求|查询|用户` 这种通用词，在这个仓里
# 它们**就是领域名词**（「请求了 5 个、库里认得 3 个」「给两个化学品」）⇒ 58 个命中
# 里绝大多数是误伤。**一道恒红的闸会被绕开**，杀伤面比它挡住的还宽。
# ⇒ 只认**生产聚合量**的那几个形状：外部流量 · 比率指标 · 延迟分位 · 限流参数 ·
# 语料/全量规模 · 定性的用户基数。
_AGGREGATE = re.compile(
    # 外部流量（「外部」＝客户流量，与内部 smoke 分开）
    r"外部调用|external (?:failed )?calls|外部流量"
    # 比率指标：指标名 + **百分比**。🔴 只认 `%`，不认裸数字也不认 `N/M`：
    # 「误判率压不到 0」「覆盖率从 23/23 掉到 23/24」都是机制描述不是披露。
    r"|(?:可答率|成功率|覆盖率|命中率|误判率|转化率?|留存率?|拒答率)[^\n]{0,14}\d+(?:\.\d+)?\s*%"
    r"|\d+(?:\.\d+)?\s*%[^\n]{0,10}(?:可答率|成功率|覆盖率|命中率|误判率|转化率?|留存率?|拒答率)"
    # 规模：Prod 全量 / 语料 / 注册用户数。🔴 「全量」不能裸着认——
    # 「每次全量重序列化，在 3,000 条的列表上」是算法推导。
    r"|(?:Prod 全量|语料规模|库里共|注册用户|付费用户)[^\n]{0,8}[\d,]{3,}"
    # 限流参数（可以直接拿去用的东西）
    r"|(?:令牌桶|限流|速率上限)[^\n]{0,20}(?:容量\s*\d|\d+\s*/\s*s)"
    r"|容量\s*\d+[^\n]{0,10}补充")
# 延迟分位数：`p50`/`p95` 后面跟着数字就是在公布我们的延迟
_LATENCY = re.compile(r"\bp(?:50|95|99)\b[^\d\n]{0,8}\d")
# 定性的用户基数披露 —— 一个数字都没有，却把基数说尽了
_QUALITATIVE = re.compile(
    r"唯一[^\n]{0,10}(?:活跃|深度|真实)[^\n]{0,8}用户|我们最深的用户|唯一(?:的)?付费")
# 🔴 票号先剥掉再匹配：`CI-906` 里那三位数字会让「语料…906」这种句子假红，
# 而票号在这个仓里到处都是 ⇒ 不剥的话这条判据的假阳性主要来源就是它自己。
_TICKET = re.compile(r"CI-\d+")

# 🔴 **它抓不到什么，写在这里，别把绿读成干净**：
# · 散在表格里、离指标名很远的比值（CI-613 那张 RAI 拒答率表就是这形状）；
# · 用全新说法讲同一件事（「我们这边一天也就几十次」）。
# ⇒ 这是**复发闸不是审计**。真要审一轮，`server.py`+`tests/`+`scripts/` 全量人读，
#   判据是「这句说的是机制，还是我们的量」。

def _volume_hits(text: str) -> bool:
    """只认生产聚合量 —— 见上面为什么名词表必须窄。"""
    text = _TICKET.sub("CI", text)
    return bool(_AGGREGATE.search(text) or _LATENCY.search(text)
                or _QUALITATIVE.search(text))


@pytest.mark.parametrize("path", _TARGETS, ids=lambda p: p.name)
def test_no_business_volume_in_public_comments(path):
    hits = []
    for lineno, text in list(_comment_chunks(path)) + list(_docstrings(path)):
        if _volume_hits(text):
            first = next(l for l in text.splitlines() if l.strip())
            hits.append(f"{path.name}:{lineno} {first.strip()[:90]}")
    assert not hits, (
        "公开仓的注释里出现了业务量披露——泄的是我们有多小、能力到哪、闸门参数是多少：\n  "
        + "\n  ".join(hits)
        + "\n⇒ 留**机制**（为什么这个常量是这个值、什么形状的失败），"
          "把条数/占比/延迟/限流参数/用户基数留在私有票里。")


def test_volume_criterion_is_not_a_no_op(tmp_path):
    """🔴 两侧变异（写死在测试里，别只写在注释里）。

    阳性按**真实泄露的形状**造：把 2026-09-18 从 `server.py` / `scripts/` / `tests/`
    删掉的那几句原样喂回去。阴性是**同一轮里被这条判据误伤过的真实句子**
    ——它们留在仓里，所以这半边一旦松了会立刻被这个文件自己抓到。
    """
    positives = [
        # 🔴 数字全是编的（见上）——形状照被删掉的真句子，取值一个真的都不留
        "# 看不见「他本来要问什么」——111 次外部调用里只有 22 次走 `ask_chemical_safety`",
        "# 实测 p50 **33.3 秒**；而后端早就是确定性实现",
        "# 必须渲进文本：后端可答率因这一层从 11.1% 升到 22.2%（exposure 场景）",
        "# 可答率因此从 11.1% 升到 22.2%（exposure 场景，Prod 全量 333,333 条）",
        "# 网关是令牌桶：**容量 11、补充约 2.22/s**（实测）",
        "# 也意味着这一整票的收益，唯一深度活跃的真实 MCP 用户根本吃不到。",
    ]
    for i, line in enumerate(positives):
        probe = tmp_path / f"pos{i}.py"
        probe.write_text(line + "\nx = 1\n", encoding="utf-8")
        chunks = _comment_chunks(probe)
        assert chunks, f"注释没解析出来：{line}"
        assert _volume_hits(chunks[0][1]), f"已知阳性不亮，这条判据是空跑的：{line}"

    negatives = [
        # 工程常量推导 —— CLAUDE.md 明确要求留下的那类，不许误伤
        "# 按比例先砍一刀：实测 3,000 条的列表逐条重序列化要 7.7 秒 CPU",
        "# 砍 25% 会过度丢弃（实测同一份载荷，预算够放 6 对时只留下了 3 对）",
        "# review 实测两条 `incompatible` 落在下标 150 / 188，砍尾巴让它们一条都没活下来",
        "# CI-55: direct tools call fast no-LLM endpoints on a 15s client timeout.",
        "# 🔴 Prod 上这条路径不经过那个字段，缺席是常态不是罕见",
        # 这一轮被前两版判据误伤过的真实句子 —— 它们都还在仓里
        "# 覆盖率就静默地从 23/23 掉到 23/24，而没有任何一步会报错",
        "# 改措辞把误判率压下来了，但**压不到 0**（任何写法都还有残余）",
        "# 每次全量重序列化，在 3,000 条的列表上要 7.7 秒 CPU",
        "# 取值换成合成夹具——原始那个 CAS 来自真实语料，按 CI-906 红线不进公开仓",
    ]
    for i, line in enumerate(negatives):
        probe = tmp_path / f"neg{i}.py"
        probe.write_text(line + "\nx = 1\n", encoding="utf-8")
        assert not _volume_hits(_comment_chunks(probe)[0][1]), \
            f"误伤了一条工程常量推导：{line}"
