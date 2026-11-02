"""`server.<name>` 的 setattr 必须落到每个持有该名字的实现模块上。

实现按工具组拆在 `msds_mcp/` 下，每个模块在自己的全局里查名字；只改 `server` 模块自己的属性，
调用点不会变，而测试的 patch 也不报错。本文件钉住两件事：
1. patch 经 `server` 进入所有持有该名字的模块，退出后全部还原；
2. 由 `global` 语句重绑的模块级状态，不允许被别的模块 `from … import` 成过期副本。

变异：把 `_ServerModule.__setattr__` 里的传播循环删掉 ⇒ 第 1 条红（且 call_logging/ 下大量测试红）。
"""
import ast
import pathlib

import pytest

import server

PKG = pathlib.Path(server.__file__).resolve().parent / "msds_mcp"


def _modules():
    return [m for m in server._MODULES]


def test_patch_through_server_reaches_every_holder(monkeypatch):
    original = server._quick_chat
    holders = [m for m in _modules() if vars(m).get("_quick_chat") is original]
    assert len(holders) >= 2, "需要至少两个持有者才能证明传播"

    async def fake(*a, **k):
        return {}

    monkeypatch.setattr(server, "_quick_chat", fake)
    assert all(vars(m)["_quick_chat"] is fake for m in holders)
    monkeypatch.undo()
    assert all(vars(m)["_quick_chat"] is original for m in holders)
    assert server._quick_chat is original


def test_live_counter_is_not_copied(monkeypatch):
    from msds_mcp import calls
    # patch 打在属主 `calls` 上：pytest 9.2 的 monkeypatch 对 `server` 静态取不到旧值，
    # 撤销时会对 facade 走 delattr。经 `server` 的读写两个方向下面各测一次。
    monkeypatch.setattr(calls, "_call_log_post_failures", 7)
    assert server._call_log_post_failures == 7
    server._call_log_post_failures = 9
    assert calls._call_log_post_failures == 9
    assert "_call_log_post_failures" not in vars(server)


def test_globally_rebound_names_are_never_imported_elsewhere():
    rebound = {}
    imported = {}
    for path in sorted(PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                for n in node.names:
                    rebound[n] = path.name
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("msds_mcp"):
                for a in node.names:
                    imported.setdefault(a.name, []).append(path.name)
    assert rebound, "没找到任何 `global` 重绑，扫描在空跑"
    stale = {n: imported[n] for n in rebound if n in imported}
    assert not stale, f"这些由 `global` 重绑的名字被别的模块导入成了过期副本：{stale}"
