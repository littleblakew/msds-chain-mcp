"""CI-906：这是**公开仓**——测试夹具里不许出现真实语料。

Blake 2026-08-06 拍板：语料剥掉身份后进 `msds-chain`（私有）可以，**进公开仓绝对不行**。
理由不是身份信息——语料里有某客户的**完整配方组分表 + 浓度**（商业秘密量级）。

**这道闸不是在修一个已发生的泄漏。** 2026-09-10 我按形状扫过工作树（54 个测试文件）
与**全部 291 个 commit 的历史**，都干净：配方形态 0、法语标记 0、超长散文 0
（历史里 51 条超长行定位到 README / SKILL.md / JSON 单行 / 一个提交进仓的 macOS 二进制）。
🔴 **它防的是明天**：此前这条红线的执行方式是「记得住」，而**公开仓一次疏忽不可逆**
（推上去就在别人的 clone 和 fork 里了）。**「我保证会守」不是判据。**

---

## 🔴 三条设计约束（前两条 growth 提的，第三条我加的）

**① 绝不把真实语料写进本文件当黑名单** —— 那等于把语料提交进公开仓，
**守卫会变成它要防的那个东西**。所以下面全部是**形状判据 + 白名单**，一个禁词都没有。

**② 变异两侧各造**（见文件末尾 `MUTATIONS`）。只造「该红的」会漏掉误报路径，
而**误报是豁免表被污染的入口**：一旦有人为了让 CI 过而往白名单里塞一条，这道闸从此静默。

**③ 守卫要能自己发现成员** —— 下面 `_iter_fixture_files()` **扫整个 `tests/`**，
不写死文件清单。⇒ **它自己的变异方式是「往 `tests/` 加一个新文件」**，不是改现有文件。

## 覆盖边界（说清楚，别让下一个人以为它管得更宽）

只扫 `tests/`。`server.py` 等源码不在范围内——那里有大量合法的长中文注释，
纳进来会让误报路径压过真报路径（见约束②为什么这很危险）。
⇒ **语料若被贴进源码注释，这道闸看不见。** 已知缺口，不是疏忽。
"""
import ast
import pathlib
import re

import pytest

TESTS_DIR = pathlib.Path(__file__).parent

# ---------------------------------------------------------------------------
# 白名单：允许出现在夹具里的 CAS。**全部是公开的常见物质**（PubChem 上人人可查），
# 外加两个明显的占位号。真实配方会带来**不在这张表上的** CAS —— 那正是判据。
#
# 🔴 往这张表加号是**有意的动作**，会出现在 diff 里被 review 看到。
#    加之前问一句：**这个号是从哪来的？** 来自客户资料 ⇒ 不许加，改用合成夹具。
# ---------------------------------------------------------------------------
ALLOWED_CAS = {
    "67-64-1":    "acetone",
    "7647-01-0":  "hydrochloric acid",
    "71-43-2":    "benzene",
    "7732-18-5":  "water",
    "7664-39-3":  "hydrogen fluoride",
    "64-17-5":    "ethanol",
    "12539-80-9": "silicon carbide (fibre)",
    "7722-84-1":  "hydrogen peroxide",
    "75-05-8":    "acetonitrile",
    "67-56-1":    "methanol",
    "9005-25-8":  "starch",
    "50-00-0":    "formaldehyde",
    "141-78-6":   "ethyl acetate",
    "1310-73-2":  "sodium hydroxide",
    "7681-52-9":  "sodium hypochlorite",
    "7664-93-9":  "sulfuric acid",
    "7440-00-0":  "nickel",
    "1234-56-7":  "(占位号，故意不是真的)",
    "99999-08-0": "(占位号，故意不是真的)",
}

_CAS = re.compile(r"\b\d{2,7}-\d{2}-\d\b")

# 配方组分表的形态：**同一行**里既有百分比又有 CAS。
# 2026-09-10 实测：工作树 0 次、291 个 commit 的历史 0 次 —— 合成夹具天然不长这样。
_PCT_WITH_CAS = re.compile(r"\d{1,3}(?:[.,]\d+)?\s*%.{0,60}?\b\d{2,7}-\d{2}-\d\b")

# 贴进来的 SDS 正文形态：**非 docstring** 的超长字符串字面量。
# 🔴 阈值 600 的依据（2026-09-10 量的，不是拍的）：当时全部非 docstring 字面量的
#    **最大值是 410**（`test_ci493_compliance_two_axes.py:64`），>400 的只有 1 个、>600 的 0 个；
#    而真实 SDS 的一节是**千字级**。600 同时高于合法夹具的天花板、低于真语料的地板。
#    ⚠️ 它是**有意设的上限**，不是「照当前最大值 +1」——夹具本来就不该装长散文。
#    要调它必须重新量一次分布并在这里写下新数字与日期。
_MAX_LITERAL = 600


def _iter_fixture_files():
    """🔴 自己发现成员：扫整个 `tests/`，不写死清单。"""
    for f in sorted(TESTS_DIR.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        if f.name == pathlib.Path(__file__).name:   # 本文件自己不算夹具
            continue
        yield f


def _docstring_ids(tree):
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def test_no_formulation_table_shape():
    """组分+浓度同行 ＝ 配方表。这是那份语料最要命的部分，也是形状最独特的部分。"""
    bad = []
    for f in _iter_fixture_files():
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _PCT_WITH_CAS.search(line):
                bad.append(f"{f.relative_to(TESTS_DIR.parent)}:{i}")
    assert not bad, (
        "疑似配方组分表（同一行里既有百分比又有 CAS）——这是**公开仓**，"
        f"真实语料绝不能进：{bad}\n"
        "若确属合成夹具，请改写成不同形状；**不要**为它开豁免。"
    )


def test_every_cas_is_on_the_public_whitelist():
    """夹具里的 CAS 必须都在白名单上。真实配方会带来不在表上的号。"""
    unknown = {}
    for f in _iter_fixture_files():
        for cas in set(_CAS.findall(f.read_text(encoding="utf-8"))):
            if cas not in ALLOWED_CAS:
                unknown.setdefault(cas, []).append(str(f.relative_to(TESTS_DIR.parent)))
    assert not unknown, (
        f"夹具里出现白名单外的 CAS：{unknown}\n"
        "🔴 加进 ALLOWED_CAS 之前先问：**这个号是从哪来的？**\n"
        "   来自客户资料 / 真实语料 ⇒ **不许加**，改用合成夹具（这是公开仓）。\n"
        "   是公开的常见物质 ⇒ 加上并写明名称，让 diff 里看得出你判过。"
    )


def test_no_pasted_prose_in_fixtures():
    """非 docstring 的超长字符串 ＝ 贴进来的 SDS 正文。docstring 不算（本仓注释本来就长）。"""
    long_ones = []
    for f in _iter_fixture_files():
        src = f.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:                     # 语法坏了是别的测试的事
            continue
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs and len(node.value) > _MAX_LITERAL:
                long_ones.append(
                    f"{f.relative_to(TESTS_DIR.parent)}:{node.lineno} ({len(node.value)} 字符)")
    assert not long_ones, (
        f"夹具里有超过 {_MAX_LITERAL} 字符的非 docstring 字符串，疑似贴进来的 SDS 正文："
        f"{long_ones}\n夹具不该装长散文；要解释就写进 docstring（不计入本判据）。"
    )


def test_the_guard_actually_sees_files():
    """🔴 自检：粒度必须和守卫的粒度一样。

    上面三条都是「没找到就绿」。若 `_iter_fixture_files()` 哪天返回空
    （目录挪了 / 过滤写错），三条会**一起变成绿的空跑**，而且与「真的很干净」完全同形。
    """
    files = list(_iter_fixture_files())
    assert len(files) >= 40, f"只发现 {len(files)} 个夹具文件，扫描面塌了（2026-09-10 是 53 个）"
    # 阳性对照：白名单里至少有一个号真的出现在夹具里 —— 否则「CAS 检查」也是空跑
    seen = set()
    for f in files:
        seen |= set(_CAS.findall(f.read_text(encoding="utf-8")))
    assert seen & set(ALLOWED_CAS), "夹具里一个 CAS 都没有，CAS 那条判据是空跑"


# ---------------------------------------------------------------------------
# 🔴 变异记录（2026-09-10 实跑，两侧各造。没记变异的守卫默认当它不存在）
#
#   【该红的】
#   1. 往 tests/ **新增一个文件**，内含 `"12.5 % ... 108-88-3"`
#      → test_no_formulation_table_shape 红 + test_every_cas... 红
#      （这一条同时验证了约束③：守卫**自己发现**了那个新文件）
#   2. 现有夹具里把一个 CAS 换成白名单外的号
#      → test_every_cas_is_on_the_public_whitelist 红
#   3. 塞一个 700 字符的普通字符串
#      → test_no_pasted_prose_in_fixtures 红
#   4. 把 _iter_fixture_files 的 rglob 改成永远返回空
#      → test_the_guard_actually_sees_files 红（其余三条会变绿 —— 这正是它存在的理由）
#
#   【不该红的】
#   5. 正常合成夹具（`invented filler b` / acetone / 现有 53 个文件全量）
#      → 全绿。**误报是豁免表被污染的入口**，所以这一侧必须一起验。
#   6. 一段 1500 字符的中文 **docstring**（本仓风格）
#      → 不红（docstring 不计入长度判据）
# ---------------------------------------------------------------------------
MUTATIONS = "见上方注释块；改本文件的人请一并更新，并两侧都重跑一次。"
