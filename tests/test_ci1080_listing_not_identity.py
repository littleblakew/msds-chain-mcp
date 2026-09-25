"""CI-1080：`search_chemical_database` 的描述不许把 listing 面说成身份解析器。

这个工具落在 `GET /chemicals?q=`，返回的是**候选行**，near-match 是设计内的
（`server.py` 里 `_mismatch` / `substance_no_cas` 两段都写着「这一行不进判定」）。
描述一旦说它「给你 canonical CAS」，消费端的模型就会把第一行当成身份结论 —— 这已经
发生过一次，而同一个输入走判定面时答案是对的 ⇒ **错在描述，不在检索**。

🔴 **为什么守在描述上而不是守在代码上**：这条缺陷的载体就是那段文本，代码一行都没错。
所以这里的判据只能打在「工具注册后对外发布的那份 description」上，而不是读源文件
—— 读源文件会在 docstring 被移进常量/被 i18n 包一层之后静默失效。

🔬 **变异（做过就记下来，否则这个守卫默认当不存在）**：
- 把老那句 `or to get the canonical CAS number for a chemical name.` 加回 docstring
  → `test_description_does_not_promise_a_decided_identity` 红并点名那句。实跑过。
- 删掉 `not a decided identity` 那一句 → `test_description_frames_rows_as_candidates`
  红。实跑过。
- 空跑侧：`test_description_is_actually_fetched` 防「list_tools 改了形状 ⇒ 拿到空串
  ⇒ 上面两条永远绿」—— 那种失败形态和「描述写得很好」完全同形。实跑过（把
  `_description()` 改成返回 `""`：它与 `frames_rows_as_candidates` 同时红，但只有
  它报得出**原因**（描述长度 0），另一条报的是「没说返回的是候选」这句假话）。
"""
import asyncio
import re

import server

TOOL = "search_chemical_database"

# 声称「给你一个定下来的身份」的说法。故意按**语义骨架**写而不是抄整句：
# 换个措辞重犯同一个错时也要红。
_IDENTITY_CLAIM = re.compile(
    r"(get|obtain|retrieve|look\s*up|return)\s+the\s+"
    r"(canonical|correct|official|definitive|right)\s+CAS",
    re.IGNORECASE,
)


def _description() -> str:
    tools = asyncio.run(server.mcp.list_tools())
    for t in tools:
        if t.name == TOOL:
            return t.description or ""
    raise AssertionError(f"{TOOL} 不在 list_tools() 里 —— 工具没了还是改名了？")


def test_description_is_actually_fetched():
    """防空跑：拿不到描述时下面两条都会「通过」，且与正常完全同形。"""
    desc = _description()
    assert len(desc) > 200, f"{TOOL} 的描述只有 {len(desc)} 字符，取描述这条路多半断了"
    # 阳性对照：这几个词是本工具描述里无论怎么改写都该在的
    assert "CAS" in desc and "MSDS" in desc


def test_description_does_not_promise_a_decided_identity():
    desc = _description()
    hit = _IDENTITY_CLAIM.search(desc)
    assert hit is None, (
        f"{TOOL} 的描述又在承诺身份了：{hit.group(0)!r}。"
        "这是 listing 面，返回的是候选；要给身份就指向判定工具。"
    )


def test_description_frames_rows_as_candidates():
    """光「不说错话」不够 —— 不说清是候选，模型照样会把第一行当答案。"""
    desc = _description().lower()
    assert "candidate" in desc, f"{TOOL} 的描述没说返回的是候选"
    assert "not a decided identity" in desc, (
        f"{TOOL} 的描述没说清候选行不是身份结论"
    )
    assert "ask_chemical_safety" in desc, (
        f"{TOOL} 的描述没把要身份的调用方指向判定工具"
    )
