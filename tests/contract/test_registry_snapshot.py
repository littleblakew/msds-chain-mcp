"""MCP 注册面（工具名 / 描述 / schema / annotations / 注册顺序 / prompts / resources / instructions）
与 `registry_snapshot.json` 逐字一致。

拆文件、搬函数这类重构不许改变对外注册面；有意改工具面时跑
`python3 scripts/dump_registry_snapshot.py` 重新生成，并同时处理 `published_tool_surface.json`。

变异：改任一工具 docstring 的一个字符 / 调换两个工具模块的 import 顺序 ⇒ 本条红。
"""
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from dump_registry_snapshot import snapshot  # noqa: E402

FIXTURE = ROOT / "tests" / "contract" / "registry_snapshot.json"


def _normalise(doc) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True, default=str)


def test_registry_matches_snapshot():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = json.loads(_normalise(asyncio.run(snapshot())))
    assert len(actual["tools"]) >= 20 and expected["tools"], "快照是空的，这条守卫在空跑"
    assert [t["name"] for t in actual["tools"]] == [t["name"] for t in expected["tools"]], "工具注册顺序变了"
    for a, e in zip(actual["tools"], expected["tools"]):
        assert a == e, f"工具 {e['name']} 的对外定义变了"
    assert actual == expected
