"""守卫：测试按**被测对象**放，不按票号放。

一张票一个 `test_<票号>_*.py` 的写法，让文件数跟着票数涨而不是跟着代码面涨：
同一个工具的测试散在十几个文件里，要找「`check_mixing_order` 的测试在哪」只能翻文件名。
票号留在 commit message 和 docstring 里就够了，文件名写它测的是什么。

两条规则（成员从 `tests/` 自己发现，不写死清单）：

1. 文件名里不许出现票号前缀 `test_ci<数字>`。
2. `tests/` 根下只放 `conftest.py`；测试文件进 `tests/<被测面>/` 子目录（见 `tests/README.md`）。

## 记录的变异（每条都实测过）

| 变异 | 应该 |
|---|---|
| 新建 `tests/rendering/test_ci9999_x.py` | 第 1 条红 |
| 新建 `tests/test_x.py` | 第 2 条红 |
| `_test_files()` 改成恒返回空 | 自检那条红（守卫看不见东西时不许绿） |
"""
import pathlib
import re

TESTS = pathlib.Path(__file__).resolve().parents[1]

_TICKET_PREFIX = re.compile(r"^test_ci\d+", re.IGNORECASE)
_ROOT_ALLOWED = {"conftest.py"}


def _test_files():
    return sorted(p for p in TESTS.rglob("test_*.py") if "__pycache__" not in p.parts)


def test_the_guard_can_see_test_files():
    """自检：一个扫不到任何文件的守卫，和「全都合规」同形。"""
    files = _test_files()
    assert len(files) > 50, f"只发现 {len(files)} 个测试文件，`TESTS` 指错目录了？({TESTS})"
    assert pathlib.Path(__file__).resolve() in files


def test_no_ticket_number_file_names():
    bad = [str(p.relative_to(TESTS)) for p in _test_files() if _TICKET_PREFIX.match(p.name)]
    assert not bad, (
        f"测试文件名带票号：{bad}。按被测对象命名（例 `rendering/test_precursor_disclosure.py`），"
        "能追加进已有的同主题文件就追加；票号写进 docstring。"
    )


def test_tests_root_holds_no_test_files():
    stray = sorted(p.name for p in TESTS.glob("*.py") if p.name not in _ROOT_ALLOWED)
    assert not stray, (
        f"`tests/` 根下有测试文件：{stray}。放进 `tests/<被测面>/`，目录说明见 `tests/README.md`。"
    )
