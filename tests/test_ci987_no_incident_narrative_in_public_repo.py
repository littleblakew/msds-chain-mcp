"""CI-987：这是个 **public repo**，注释里不许写「哪天被撞到」的事故叙事。

泄的不是语料（那由 `test_ci906_no_real_corpus_in_fixtures.py` 守着），是**我们哪里弱、
哪天被撞到、被谁撞到、量有多小**。收敛前 `server.py` 里有：逐工具的 Prod 延迟表与调用量
（`6/25 hit 15s`、`0/41`——顺带把我们的总调用量暴露成两位数）、一条带**人名 + 精确到分钟 +
渠道**的事故、「我们最深的用户某天两次踩到、而我们看不见」、以及「100% 远程流量来自某客户端」。

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
_TARGETS = sorted(
    p for p in list(_ROOT.glob("*.py")) + list((_ROOT / "tests").glob("*.py"))
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
