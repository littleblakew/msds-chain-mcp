r"""守卫：测试**用到的 pytest 插件**必须在 `requirements-dev.txt` 里声明。

2026-09-15 我用 `@pytest.mark.asyncio` 写了两个用例，**本机 522 passed**、
CI 的 `deploy` job 当场红（`Failed: async def functions are not natively supported`）
⇒ **把一次 Prod 部署堵在了「Run tests」这一步**。
根因：本仓 `requirements-dev.txt` 只有 `pytest`，而 pytest-asyncio 恰好装在我本机的 venv 里。
**本机绿不是 CI 绿 —— 差在一个没被声明的依赖上。**

🔴 **仓里本来就写着这一条**：`tests/test_ppe_undetermined_rendering.py` 顶部有一段注释，
逐字记着同一个事故（「The first version of this file used @pytest.mark.asyncio, passed
locally …, and failed the deploy job」）。**我没看见它** —— 那是一段只有先搜
`pytest.mark.asyncio` 才找得到的散文，而会犯这个错的人恰恰不会去搜那个词。
⇒ 本文件把那段散文换成机械判据。**散文留着没关系，但它不该是唯一的防线。**

判据：扫全部测试文件里出现的 `@pytest.mark.<name>`，凡是**需要插件**的 mark
（`_PLUGIN_MARKS`）都必须能在 `requirements-dev.txt` 里找到对应的包名。

| 守卫 | 把什么改回去会让它红 |
|---|---|
| `test_no_test_uses_an_undeclared_pytest_plugin` | 在任意测试文件里加一个 `@pytest.mark.asyncio`（＝重演 2026-09-15 那次） |
| `test_the_guard_can_actually_see_marks` | 把 `_marks_in_tests` 的扫描目标改成匹配不到任何东西（阳性对照：**守卫自己得先能看见 mark**，否则它在空集上恒绿） |
"""
import ast
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent

# mark 名 → 提供它的包名（出现在 requirements-dev.txt 里的那个词）。
# 🔴 加一个需要插件的 mark 时**这里也要加一行**——否则本守卫对它是瞎的。
# pytest 自带的 mark（skip / skipif / xfail / parametrize / usefixtures / filterwarnings）
# 不需要插件，刻意不收。
_PLUGIN_MARKS = {
    "asyncio": "pytest-asyncio",
    "anyio": "anyio",
    "benchmark": "pytest-benchmark",
    "freeze_time": "pytest-freezegun",
    "mock": "pytest-mock",
}


def _marks_in_tests() -> dict[str, list[str]]:
    """文件 → 它用到的 mark 名。

    🔴 **判据打在 `ast` 解析出来的装饰器上，不是文件文本**（仓里已登记的规则：
    同一个文件里代码与**描述代码的散文**混在一个平面，grep 分不清）。
    本文件和 `test_ci977_response_kind.py` 的 docstring 里都逐字写着
    `@pytest.mark.asyncio` **作为反例** —— 按文本扫会把它们判成违规，
    那正是「守卫把讨论当成副本」那一类。第一版就是这么写的，当场自己咬自己。
    """
    found: dict[str, list[str]] = {}
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for dec in node.decorator_list:
                # `@pytest.mark.x` 与 `@pytest.mark.x(...)` 两种形状
                target = dec.func if isinstance(dec, ast.Call) else dec
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Attribute)
                        and target.value.attr == "mark"):
                    names.add(target.attr)
        if names:
            found[path.name] = sorted(names)
    return found


def _declared() -> str:
    return (REPO / "requirements-dev.txt").read_text(encoding="utf-8").lower()


def test_no_test_uses_an_undeclared_pytest_plugin():
    declared = _declared()
    offenders = []
    for fname, names in _marks_in_tests().items():
        for name in names:
            pkg = _PLUGIN_MARKS.get(name)
            if pkg and pkg.lower() not in declared:
                offenders.append(f"{fname}: @pytest.mark.{name} 需要 {pkg}")
    assert not offenders, (
        "这些 mark 需要一个 requirements-dev.txt 里没有的插件 —— 本机可能恰好装了它，"
        "CI 不会有，deploy job 会在 Run tests 那步红并堵住 Prod 部署：\n  "
        + "\n  ".join(offenders)
        + "\n⇒ 要么改用仓里的既有写法（异步用 `asyncio.run`），要么把插件加进 requirements-dev.txt。"
    )


def test_the_guard_can_actually_see_marks():
    """阳性对照：**守卫自己得先能看见 mark**。

    🔴 没有这一条的话，解析哪天写坏、或者扫描路径指错，上面那条会在**空集**上
    恒绿——而恒绿的守卫和不存在是一回事。这里只要求「全仓至少有人用过 mark」，
    不钉具体是哪一个（钉具体的等于把现状钉成契约）。
    """
    assert _marks_in_tests(), "一个 mark 都没扫到 —— 先查 ast 解析 / 扫描路径，别信上面那条绿"
