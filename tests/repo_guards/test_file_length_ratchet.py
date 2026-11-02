"""棘轮：超线的文件只准变短，新文件不准超线。

往单个文件里加东西是无声动作：`server.py` 曾经单文件装下全部工具实现，改它的人越多它越长，
而没有任何东西会红。这里做两件事：
1. `_LIMIT` 以上的文件登记在 `tests/file_length_baseline.txt`，行数只准持平或下降；
2. 不在基线里的文件不准超过 `_LIMIT`。

**确需增长时**：在 PR 里把基线那一行的数字改大，并在行尾写 `# 理由`。基线是普通文本，
改动出现在 diff 里，由 review 判。

**变短了**：不红，只发 warning 提示收紧。降到线下的文件必须从基线删掉（否则它占着旧数字能长回去），
用本模块自带的命令：`python3 tests/repo_guards/test_file_length_ratchet.py --tighten`。
它只会把数字改小、删掉已降到线下的行，从不新增行、从不改大。

**作用域**：仓根、`msds_mcp/`、`scripts/`、`tests/` 下的 `.py`（`npm-package/` 与点目录不在内）。

## 记录的变异（每条都实测过）

| 变异 | 应该 |
|---|---|
| 新建一个 1501 行的 `.py` 文件 | `test_no_file_grows_past_its_baseline_or_the_limit` 红（new_oversize） |
| 基线里 `tests/tools/test_server_tools.py` 的数字改小 | 同上红（grown） |
| 基线里加一行指向一个 1000 行的文件 | 同上红（under_limit） |
| `_DIRS` 改成空元组 | `test_scanner_sees_the_tree` 红 |
| `check()` 里去掉 grown 分支 | `test_check_*` 红 |

后四条里 `test_check_*` 与 `test_scan_*` 用 `tmp_path` 喂数据，不往真目录写探针。
"""
from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_BASELINE = _REPO / "tests" / "file_length_baseline.txt"
_LIMIT = 1500
_DIRS = ("msds_mcp", "scripts", "tests")

_LINE = re.compile(r"^\s*(\d+)\s+(\S+)\s*(#.*)?$")


def count_lines(path: Path) -> int:
    """与 `wc -l` 同口径：数换行符。"""
    return path.read_bytes().count(b"\n")


def scan(base: Path = _REPO, dirs: tuple[str, ...] = _DIRS) -> dict[str, int]:
    """{相对仓根的路径: 行数}。`base` 只给变异测试用（落在 tmp_path）。"""
    files = list(base.glob("*.py"))
    for d in dirs:
        files += (base / d).rglob("*.py")
    return {p.relative_to(base).as_posix(): count_lines(p)
            for p in sorted(set(files)) if "__pycache__" not in p.parts}


def _read_rows(path: Path = _BASELINE) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(ln)
        if m:
            out[m.group(2)] = (int(m.group(1)), (m.group(3) or "").strip())
    return out


def read_baseline(path: Path = _BASELINE) -> dict[str, int]:
    return {f: n for f, (n, _) in _read_rows(path).items()}


def check(counts: dict[str, int], baseline: dict[str, int], limit: int = _LIMIT) -> dict[str, list[str]]:
    """纯函数。grown / new_oversize / missing / under_limit 会红；shrunk 只提示。"""
    res: dict[str, list[str]] = {"grown": [], "new_oversize": [], "missing": [],
                                 "under_limit": [], "shrunk": []}
    for f, n in sorted(counts.items()):
        if f in baseline:
            if n > baseline[f]:
                res["grown"].append(f"{f}: {baseline[f]} → {n}（+{n - baseline[f]}）")
            elif n <= limit:
                res["under_limit"].append(f"{f}: {baseline[f]} → {n}")
            elif n < baseline[f]:
                res["shrunk"].append(f"{f}: {baseline[f]} → {n}")
        elif n > limit:
            res["new_oversize"].append(f"{f}: {n} 行（上限 {limit}）")
    for f in sorted(set(baseline) - set(counts)):
        res["missing"].append(f)
    return res


def tightened(counts: dict[str, int], baseline: dict[str, int], limit: int = _LIMIT) -> dict[str, int]:
    """只收紧：数字取 min，降到线下的删掉，从不新增、从不改大。"""
    return {f: min(n, counts[f]) for f, n in baseline.items()
            if f in counts and min(n, counts[f]) > limit}


def _write(baseline: dict[str, int], path: Path = _BASELINE) -> None:
    head = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("#")]
    reasons = {f: r for f, (_, r) in _read_rows(path).items()}
    body = [f"{n:>6}  {f}" + (f"  {reasons[f]}" if reasons.get(f) else "")
            for f, n in sorted(baseline.items())]
    path.write_text("\n".join(head + body) + "\n", encoding="utf-8")


def test_scanner_sees_the_tree():
    """阳性对照：扫描器坏了时，下面那条会因「全都在线下」而空绿。"""
    counts = scan()
    assert len(counts) >= 60, f"只扫到 {len(counts)} 个 .py——先怀疑扫描器，别去改基线"
    for must in ("server.py", "msds_mcp/calls.py", "tests/conftest.py"):
        assert must in counts, f"{must} 没被扫到"
    seen = sum(1 for f in read_baseline() if f in counts)
    assert seen == len(read_baseline()), "基线里有文件扫不到"


def test_no_file_grows_past_its_baseline_or_the_limit():
    res = check(scan(), read_baseline())
    msgs = []
    if res["grown"]:
        msgs.append("这些超线文件变长了（基线只准持平或下降）：\n  " + "\n  ".join(res["grown"])
                    + "\n⇒ 新逻辑放进新模块；确需增长就在 PR 里改基线数字并在行尾写 `# 理由`。")
    if res["new_oversize"]:
        msgs.append(f"这些文件超过 {_LIMIT} 行且不在基线里：\n  " + "\n  ".join(res["new_oversize"])
                    + "\n⇒ 拆开，别往基线里加新行（基线只记存量）。")
    if res["under_limit"]:
        msgs.append(f"这些文件已降到 {_LIMIT} 行以下，但基线还留着旧数字（它能一路长回去）：\n  "
                    + "\n  ".join(res["under_limit"])
                    + "\n⇒ 跑 `python3 tests/repo_guards/test_file_length_ratchet.py --tighten` 把它们从基线删掉。")
    if res["missing"]:
        msgs.append("基线里这些文件已不存在（改名 / 删除）：\n  " + "\n  ".join(res["missing"])
                    + "\n⇒ 改名的话把那一行的路径改成新路径；删了就删行。")
    assert not msgs, "\n\n".join(msgs)
    if res["shrunk"]:
        warnings.warn("这些文件变短了，可以收紧基线（--tighten）：\n  " + "\n  ".join(res["shrunk"]),
                      stacklevel=1)


def test_check_flags_each_direction():
    base = {"a.py": 2000, "b.py": 1800}
    res = check({"a.py": 2001, "b.py": 1400, "c.py": 1501, "d.py": 1500}, base)
    assert res["grown"] and res["under_limit"] == ["b.py: 1800 → 1400"]
    assert res["new_oversize"] == ["c.py: 1501 行（上限 1500）"]
    assert check({"a.py": 1999, "b.py": 1800}, base)["shrunk"] == ["a.py: 2000 → 1999"]
    assert check({"b.py": 1800}, base)["missing"] == ["a.py"]


def test_tightened_never_grows_or_adds():
    base = {"a.py": 2000, "b.py": 1800}
    assert tightened({"a.py": 1700, "b.py": 1400, "c.py": 9000}, base) == {"a.py": 1700}
    assert tightened({"a.py": 2500, "b.py": 1800}, base) == {"a.py": 2000, "b.py": 1800}


def test_scan_counts_a_file_dropped_in_a_tmp_tree(tmp_path):
    (tmp_path / "msds_mcp").mkdir()
    (tmp_path / "msds_mcp" / "big.py").write_text("x = 1\n" * 1501)
    (tmp_path / "top.py").write_text("x = 1\n")
    counts = scan(tmp_path)
    assert counts["msds_mcp/big.py"] == 1501 and counts["top.py"] == 1
    assert check(counts, {})["new_oversize"]


if __name__ == "__main__":
    if sys.argv[1:] != ["--tighten"]:
        sys.exit("用法：python3 tests/repo_guards/test_file_length_ratchet.py --tighten")
    old = read_baseline()
    new = tightened(scan(), old)
    _write(new)
    print(f"基线 {len(old)} → {len(new)} 行；收紧 {sum(1 for f in new if new[f] < old[f])} 处")
