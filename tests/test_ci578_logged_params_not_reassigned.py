"""CI-578：**被日志读的入参，不许在函数体里被覆盖**。

`_log_intent` 写在 `finally` 里，跑在函数体**之后**。任何在中间把入参重新赋值的写法，
记进 `mcp_call_logs.input_params` 的就不是调用方传的那个值 —— 而这类错**不会报错**：
用户看到的答案完全正确，只有事后读日志的人被骗。

已实际发生的两次（都由本守卫的扫描找出来）：

| 位置 | 症状 |
|---|---|
| `get_exposure_limits` | 渲染循环里 `region = lim.get("region", "")` 盖掉了过滤条件 ⇒ 日志记成**最后一条限值的** region（`region="EU"` 的调用被记成 `{"region": "US-XYZ"}`） |
| `validate_protocol_chemicals` | 就地截断 `protocol_text` ⇒ 记下的 `protocol_text_length` 对所有超长输入**恒等于 4015**，分不出 4.5k 和 200k |

🔴 **扫描自己发现成员，不维护清单**：判据是「repo 里所有 `.py` 的所有函数」，
新加一个工具照样被扫到。豁免必须写进 `_ALLOWED` 并附理由。

| 守卫 | 让它红的最小改动（都实测过） |
|---|---|
| `test_no_logged_param_is_reassigned` | ①把 `lim_region` 改回 `region` ②把 `sent_text` 改回就地覆盖 `protocol_text` ③**加成员**：新写一个工具，在体内覆盖一个会被 `_log_intent` 记的入参 |
| `test_scan_actually_visited_server_py` | 扫描函数被改成扫不到东西（路径写错 / 过滤过头）—— 空结果集同样让「没有违规」为真 |
| `test_every_exemption_is_still_a_live_hit` | 把 `get_audit_report` 里的 `session_id = built[...]` 改个名 ⇒ 豁免变成死条目，会掩护未来一段真的违规 |
| `test_exposure_limits_logs_the_callers_region_not_the_last_rows` | 同①，日志里当场变成 `US-XYZ`（静态扫描只答「有没有覆盖」，这条答「日志到底写了什么」） |
| `test_protocol_length_logged_is_the_real_input_length` | 同②，长度当场变成 `4015` |

## 🔴 review 打红的三处（第一版真的漏，都已实测复验）

- **自检本身是空跑**：第一版写 `scanned > 100`（全仓总数），而 `tests/` 一处就有 380+
  ⇒ 把 `server.py` 整个排除掉，五条全绿。现在判据是**逐文件**的。
- **只认最朴素的赋值形状**：`chemicals[:] = chemicals[:12]`（本仓 CI-570 就在用）、
  元组解包、walrus、`chemicals.append(...)`、`posonlyargs`/`*args`/`**kwargs` 全会溜过去。
- **看不见隔一层的日志**：`upload_msds_pdf` 记的是 `logged_source`（从 `pdf_source` 派生），
  只看 `_log_intent` 调用节点里的名字会漏掉整整一类。现在做**一跳**污点。

⚠️ 修 review 的 finding 4（别下潜进嵌套 `def`）时我**自己又漏了一半**：嵌套 def 直接躺在
`fn.body` 里，只过滤 children 挡不住它 —— 是变异 F（闭包里一个同名局部，**不该**被报）
把这个漏洞打红的。⇒ 假阴性的变异要造，**假阳性的变异也要造**。
"""
import ast
import pathlib
import subprocess

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent

# 就地改内容也算「覆盖」——`chemicals[:] = chemicals[:12]` 之后日志记的就不是调用方传的
# 那一串了，而这正是本仓在用的写法（CI-570 的截断）。
_MUTATORS = {"append", "extend", "insert", "pop", "remove", "clear",
             "sort", "reverse", "update", "setdefault", "popitem", "add", "discard"}

# 豁免：**覆盖是有意的、且记下覆盖后的值才是对的**。加条目必须写清为什么。
_ALLOWED = {
    ("server.py", "get_audit_report", "session_id"):
        "零参调用会现建一个会话，`session_id = built[\"session_id\"]` 之后记的就是"
        "真正出报告的那个 id；两条路径靠同一行日志里的 `built_from_recent` 区分。",
}


def _python_files():
    """只扫 **git 追踪的** `.py`。

    🔴 别用 `rglob` —— 贡献者的 `env/` / `.tox/` / `site-packages` 会被一起 `ast.parse`，
    其中任何一个非 UTF-8 或 py2 文件都会让本守卫**因为与它无关的理由报错**。
    """
    out = subprocess.run(["git", "-C", str(_REPO), "ls-files", "*.py"],
                         capture_output=True, text=True, check=True)
    return [_REPO / line for line in out.stdout.splitlines() if line]


def _own_nodes(fn):
    """只走这个函数**自己**的体，不下潜进嵌套 def / lambda。

    `ast.walk` 会下潜 ⇒ 闭包里一个同名局部会被算成外层函数的违规（假阳性，而它只能靠
    往 `_ALLOWED` 里加一条什么都没说明的条目来消音）；反向，嵌套 def 里的 `_log_intent`
    会被记到外层头上。
    """
    _NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    # 🔴 初始栈也要过滤：嵌套 def 就直接躺在 `fn.body` 里，只过滤 children 的话它照样
    # 被压进去（review 的 finding 4 修到一半 —— 变异 F 当场把这个漏洞打红了）。
    stack = [n for n in list(fn.body) + list(fn.decorator_list)
             if not isinstance(n, _NESTED)]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _NESTED):
                continue
            stack.append(child)


def _param_names(fn) -> set[str]:
    args = fn.args
    names = {a.arg for a in
             args.posonlyargs + args.args + args.kwonlyargs}
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            names.add(extra.arg)
    return names


def _bound_names(target) -> set[str]:
    """一个赋值目标绑定了哪些名字（含元组解包、切片就地赋值）。"""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out = set()
        for elt in target.elts:
            out |= _bound_names(elt)
        return out
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
        return {target.value.id}          # `chemicals[:] = …` 改的是同一个对象
    return set()


def _reassigned_params(fn, params: set[str]) -> set[str]:
    found = set()
    for node in _own_nodes(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                found |= _bound_names(target) & params
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            found |= _bound_names(node.target) & params
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            found |= _bound_names(node.target) & params
        elif isinstance(node, ast.NamedExpr):          # `(region := …)`
            found |= _bound_names(node.target) & params
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                found |= _bound_names(node.optional_vars) & params
        elif isinstance(node, ast.Call):               # `chemicals.append(…)`
            fnode = node.func
            if (isinstance(fnode, ast.Attribute) and fnode.attr in _MUTATORS
                    and isinstance(fnode.value, ast.Name) and fnode.value.id in params):
                found.add(fnode.value.id)
    return found


def _logged_params(fn, params: set[str]) -> set[str]:
    """`_log_intent` 那一行**实际会说出**哪些入参 —— 含隔一层局部变量的情况。

    `upload_msds_pdf` 记的是 `logged_source`（从 `pdf_source` 脱敏派生出来的局部），
    直接看调用节点里的名字会漏掉这一类。这里做**一跳**污点：局部 = 含某入参的表达式
    ⇒ 该局部出现在日志里，等同于那个入参出现在日志里。
    """
    derived: dict[str, set[str]] = {}
    for node in _own_nodes(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            value = node.value
            if value is None:
                continue
            src = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)} & params
            if not src:
                continue
            for t in targets:
                for name in _bound_names(t):
                    derived.setdefault(name, set()).update(src)

    logged = set()
    for node in _own_nodes(fn):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_log_intent"):
            continue
        for n in ast.walk(node):
            if not isinstance(n, ast.Name):
                continue
            if n.id in params:
                logged.add(n.id)
            logged |= derived.get(n.id, set())
    return logged


def _scan(allowed=None):
    """→ (violations, {文件: 扫到的带参函数数})。

    🔴 返回**逐文件**计数而不是总数：仓里 `tests/` 一处就有 380+ 个带参函数，一个
    「总数 > 100」的自检在 `server.py` 被整个漏掉时照样是绿的（review 实测过）。
    """
    allowed = _ALLOWED if allowed is None else allowed
    violations: list[tuple] = []
    scanned: dict[str, int] = {}
    for path in _python_files():
        rel = path.relative_to(_REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            params = _param_names(fn)
            if not params:
                continue
            scanned[rel] = scanned.get(rel, 0) + 1
            reassigned = _reassigned_params(fn, params)
            if not reassigned:
                continue
            for name in sorted(reassigned & _logged_params(fn, params)):
                if (rel, fn.name, name) in allowed:
                    continue
                violations.append((rel, fn.name, name))
    return violations, scanned


def test_no_logged_param_is_reassigned():
    violations, _ = _scan()
    assert not violations, (
        "这些入参在函数体里被覆盖，而 `finally` 的 `_log_intent` 记的是覆盖后的值 ⇒ "
        "日志会说谎且不报错。改名成局部变量，或（覆盖确实是有意的）加进 `_ALLOWED` 并写明理由：\n"
        + "\n".join(f"  {f}  {fn}() 的入参 `{p}`" for f, fn, p in violations)
    )


def test_scan_actually_visited_server_py():
    """空结果集同样让上面那条为真 —— 先证明扫描确实走到了**要扫的那个文件**。

    🔴 判据必须是**逐文件**的。第一版写的是 `scanned > 100`（全仓总数），而 `tests/`
    一处就有 380+ ⇒ 把 `server.py` 整个排除掉，这条自检仍然是绿的（review 实测）。
    这正是本文件 docstring 声称要防的那种「跑了但什么都没跑到」。
    """
    _, scanned = _scan()
    assert scanned.get("server.py", 0) >= 90, (
        f"server.py 只扫到 {scanned.get('server.py', 0)} 个带参函数（应 ~99）——扫描没覆盖生产文件")
    assert scanned.get("server_remote.py", 0) >= 1, "server_remote.py 没被扫到"


def test_every_exemption_is_still_a_live_hit():
    """豁免表里的条目必须仍然会被扫出来 —— 否则它是死条目，掩护的是一段早已不存在的代码。"""
    raw, _ = _scan(allowed={})
    hit = set(raw)
    dead = sorted(k for k in _ALLOWED if k not in hit)
    assert not dead, f"这些豁免已经没有对应的代码了，删掉：{dead}"


# ── 行为面：静态扫描只看得到「有没有覆盖」，看不到「日志里到底写了什么」 ────────────
# 下面两条按票面的原始复现来写，让日志的值本身成为判据。

@pytest.fixture
def logged(monkeypatch):
    import server as _s
    calls: list[dict] = []

    async def _fake_log(tool_name, chemicals, duration_ms, success,
                        error_message=None, input_params=None, response_text=None):
        calls.append({"tool": tool_name, "input_params": input_params})

    monkeypatch.setattr(_s, "_log_call", _fake_log)
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")
    yield calls
    set_caller_credential(None)


def test_exposure_limits_logs_the_callers_region_not_the_last_rows(logged, monkeypatch):
    """票面原始复现：`region="EU"` 的调用，撞上一条 `region="US-XYZ"` 的限值。"""
    import asyncio
    import json
    import server as _s

    async def fake(chemicals, region):
        return {"results": [{"chemical_name": "hydrofluoric acid", "cas": "7664-39-3",
                             "region_filter": region,
                             "limits": [{"source": "OSHA", "type": "TWA", "value": 3,
                                         "unit": "ppm", "region": "US-XYZ"}]}]}

    monkeypatch.setattr(_s, "_direct_exposure", fake)
    asyncio.run(_s.get_exposure_limits(chemicals=["hydrofluoric acid"], region="EU"))

    params = json.loads(logged[-1]["input_params"])
    assert params["region"] == "EU", (
        f"日志记的是渲染循环里最后一条限值的 region，不是调用方传的过滤条件：{params}")


def test_protocol_length_logged_is_the_real_input_length(logged, monkeypatch):
    """就地截断会让所有超长输入都被记成 4015 —— 分不出 4.5k 和 200k。"""
    import asyncio
    import json
    import server as _s

    async def fake(*a, **kw):
        return {"answer": "ok", "chemicals": []}

    monkeypatch.setattr(_s, "_quick_chat", fake)
    asyncio.run(_s.validate_protocol_chemicals(protocol_text="x" * 200_000))

    params = json.loads(logged[-1]["input_params"])
    assert params["protocol_text_length"] == 200_000, (
        f"记下的是截断后的长度，不是调用方真的发了多少：{params}")
