"""CI-848：工具面漂移守卫 —— 外部客户端拿的是**快照**，平台不会告诉我们它过期了。

背景（细节在 `docs/pm/tickets/CI-848.md`，这里只留判据）：ChatGPT 的应用目录条目在提交时
`Scan Tools` 拍一份工具面快照，官方原话「Published plugins do not update those skills live」；
claude.ai 在**添加连接器**时拍一份。两边都不会在我们改了工具之后通知任何人。
2026-09-03 实测：ChatGPT 目录条目停在一份 2026-05-22~07-25 的快照上，`lang` / `search_msds_online`
对它完全不存在，而 `authorize_start` 100% 来自 chatgpt.com ⇒ 那是**主力通路**。

**本守卫的契约**：`published_tool_surface.json` ＝上一次有人看过并接受的工具面。
工具面一变就红，逼一次显式决定（更新基线 + 判断要不要重交上架条目）。
🔴 **红的时候要先看它说的是哪一类**：additive 只是「该重交了」，
breaking 是「已连接的旧客户端此刻就在坏，而其中一种坏法不报错」。

🔬 **变异（两侧都造，做过就记下来，否则这个守卫默认当不存在）**：
- 危险侧：给 `get_storage_guidance` 删掉 `lang` → `test_no_drift_against_baseline` 红，
  且分类进 breaking。2026-09-03 实跑过（改 server.py 真跑，不是只喂构造数据）。
- 安全侧：给 `get_storage_guidance` 加一个可选参数 `foo` → 同样红，但分类进 additive。
  2026-09-03 实跑过。**这一侧不能省**：只造「该红的」测不出分类是不是恒为 breaking。
- 空跑侧：`test_extraction_is_not_a_noop` 防「schema 字段改名 ⇒ 每个工具都提取出空集合、
  于是永远没有漂移」——那种失败形态和「一切正常」完全同形。
"""
import asyncio
import json
import pathlib

import pytest

import server
from tool_surface import diff_surface, extract_surface

BASELINE_PATH = pathlib.Path(__file__).resolve().parent.parent / "published_tool_surface.json"


def _current() -> dict:
    return extract_surface(asyncio.run(server.mcp.list_tools()))


def _baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text())["tools"]


def test_extraction_is_not_a_noop():
    """防空跑：提取器必须真的取到了参数，否则下面那条守卫永远不会红。

    判据要同时覆盖「必填」和「可选」两条路——只断言「有工具」的话，
    schema 里 `required` 字段改名会让每个工具的必填集合静默变空，而漂移比对照样通过。
    """
    current = _current()
    assert len(current) >= 20, f"注册表只提取到 {len(current)} 个工具，提取器多半坏了"
    assert any(v["required"] for v in current.values()), "没有任何工具有必填参数——required 那条路没走通"
    assert any(v["optional"] for v in current.values()), "没有任何工具有可选参数——optional 那条路没走通"


def test_no_drift_against_baseline():
    """工具面与基线不一致 ⇒ 红。红了不是坏事，是要你做一次决定。"""
    diff = diff_surface(_baseline(), _current())
    assert not (diff["breaking"] or diff["additive"]), (
        "工具面已与 `published_tool_surface.json` 不一致 ⇒ 外部客户端（ChatGPT 应用目录条目、"
        "已连接的 claude.ai 连接器）拿到的仍是旧的那份。\n"
        "🔴 危险改动（旧客户端此刻就在坏）：\n  "
        + ("\n  ".join(diff["breaking"]) or "无")
        + "\n安全改动（旧客户端看不见，但条目该重交了）：\n  "
        + ("\n  ".join(diff["additive"]) or "无")
        + "\n\n怎么办：①判断危险改动能不能接受（能不能改成只加不减）；"
        "②去 OpenAI 后台重新 Scan Tools 并提交新版本（见 CI-848）；"
        "③跑 `python3 scripts/export_tool_surface.py` 更新基线。"
    )


# ---- 分类器本身的两侧变异（构造输入，不依赖注册表现状）----

_BASE = {"t": {"required": ["a"], "optional": ["b"]}}


@pytest.mark.parametrize(
    "current,expect_breaking",
    [
        ({}, "工具消失或改名：t"),                                            # 删工具
        ({"t2": {"required": ["a"], "optional": ["b"]}}, "工具消失或改名：t"),  # 改名＝删+增
        ({"t": {"required": ["a"], "optional": []}}, "参数 `b` 消失"),         # 删参数
        ({"t": {"required": ["a", "b"], "optional": []}}, "由可选变必填"),      # 可选变必填
        ({"t": {"required": ["a", "c"], "optional": ["b"]}}, "新增了**必填**参数 `c`"),
    ],
)
def test_breaking_changes_are_classified_as_breaking(current, expect_breaking):
    diff = diff_surface(_BASE, current)
    assert any(expect_breaking in line for line in diff["breaking"]), diff


@pytest.mark.parametrize(
    "current,expect_additive",
    [
        ({"t": {"required": ["a"], "optional": ["b"]}, "n": {"required": [], "optional": []}},
         "新增工具：n"),
        ({"t": {"required": ["a"], "optional": ["b", "c"]}}, "新增可选参数 `c`"),
    ],
)
def test_additive_changes_are_not_classified_as_breaking(current, expect_additive):
    """🔴 假阳性那一侧：安全改动被判成 breaking 会让人以为线上正在坏，
    从而把这个守卫的输出整体当噪声——这才是豁免表被污染的入口。"""
    diff = diff_surface(_BASE, current)
    assert any(expect_additive in line for line in diff["additive"]), diff
    assert not diff["breaking"], f"安全改动被误判成危险：{diff['breaking']}"
