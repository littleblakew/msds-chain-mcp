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
"""
import ast
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}

# 豁免：**覆盖是有意的、且记下覆盖后的值才是对的**。加条目必须写清为什么。
_ALLOWED = {
    ("server.py", "get_audit_report", "session_id"):
        "零参调用会现建一个会话，`session_id = built[\"session_id\"]` 之后记的就是"
        "真正出报告的那个 id；两条路径靠同一行日志里的 `built_from_recent` 区分。",
}


def _python_files():
    for p in sorted(_REPO.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


def _reassigned_params(fn: ast.AST, params: set[str]) -> set[str]:
    found = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in params:
                    found.add(target.id)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            if isinstance(node.target, ast.Name) and node.target.id in params:
                found.add(node.target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if isinstance(node.target, ast.Name) and node.target.id in params:
                found.add(node.target.id)
        elif isinstance(node, ast.withitem):  # `with ... as <param>`
            if isinstance(node.optional_vars, ast.Name) and node.optional_vars.id in params:
                found.add(node.optional_vars.id)
    return found


def _scan(allowed=None):
    """→ (violations, n_functions_scanned)。返回计数是为了让空结果能被区分。"""
    allowed = _ALLOWED if allowed is None else allowed
    violations, scanned = [], 0
    for path in _python_files():
        rel = path.relative_to(_REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            if not params:
                continue
            scanned += 1
            reassigned = _reassigned_params(fn, params)
            if not reassigned:
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call)
                        and getattr(node.func, "id", None) == "_log_intent"):
                    continue
                logged = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                for name in sorted(reassigned & logged):
                    if (rel, fn.name, name) in allowed:
                        continue
                    violations.append((rel, fn.name, name, node.lineno))
    return violations, scanned


def test_no_logged_param_is_reassigned():
    violations, _ = _scan()
    assert not violations, (
        "这些入参在函数体里被覆盖，而 `finally` 的 `_log_intent` 记的是覆盖后的值 ⇒ "
        "日志会说谎且不报错。改名成局部变量，或（覆盖确实是有意的）加进 `_ALLOWED` 并写明理由：\n"
        + "\n".join(f"  {f}:{ln}  {fn}() 的入参 `{p}`" for f, fn, p, ln in violations)
    )


def test_scan_actually_visited_server_py():
    """空结果集同样让上面那条为真 —— 先证明扫描确实走到了要扫的地方。"""
    _, scanned = _scan()
    assert scanned > 100, f"只扫到 {scanned} 个带参函数，扫描范围坏了"
    assert any(p.name == "server.py" for p in _python_files()), "server.py 没被扫到"


def test_every_exemption_is_still_a_live_hit():
    """豁免表里的条目必须仍然会被扫出来 —— 否则它是死条目，掩护的是一段早已不存在的代码。"""
    raw, _ = _scan(allowed={})
    hit = {(f, fn, p) for f, fn, p, _ in raw}
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
