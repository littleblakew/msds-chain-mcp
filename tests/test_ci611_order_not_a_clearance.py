"""CI-611：共存判定不能被读成加料顺序的绿灯。

🔴 这条缺陷的形状：`check_mixing_order("硫酸","水")` 拿到
`verdict=no_known_incompatibility` —— 那是**共存问题的正确答案**，
却出现在一个**问顺序**的工具里，而这一对的全部危险恰恰在顺序上。

🔴 修法是**纯加法**：不把共存判定改红（改红是新的误伤），只显式声明「顺序未判定」。
所以守卫必须同时钉住两件事：**说了该说的** ＋ **没动不该动的**。

CI-1078：**同一个范畴错误的反向也在这里，而且之前是本文件自己钉住的。**
原来那条 `test_incompatible_pair_says_no_safe_order_instead` 要求不相容分支说
「there is no safe addition order」——而引擎没有顺序维度，这句和「能共存 ⇒ 顺序放行」
一样是从共存判定外推出来的，只是方向相反，且它还与同一次返回里的
`structuredContent.addition_order.verdict = not_determined` **直接矛盾**。
🔴 为什么这一侧的代价更贵：**有些工艺就是故意配制被判 incompatible 的对**
（强酸 + 强氧化剂的清洗液是标准例子）⇒ 一句绝对禁令对这些用户**无法执行**，
整条答案连同该读的危害信息一起被丢掉。
⇒ 现在两个分支**都不对顺序下判定**，差别在**共存那一维说了什么**；
那条守卫改成钉「既不下绝对禁令、也没变成绿灯」。

🔴 **本文件的变异（逐条跑过，下面记的是跑出来的结果不是预测）**：
  M1 把 `_order_scope_note` 不相容分支换回 CI-1078 之前那句
     （`there is no safe addition order` + `Do not combine them in either direction.`）
     ⇒ **3 红**：`..._does_not_invent_an_order_determination`（回归钉，这是主判据）
       ＋ `..._still_says_incompatible` ＋ `..._defers_to_the_structured_stance`
       （旧句子太短，顺带丢了「受控工艺」与「散文是模型产的」两句）。
  M2 让不相容分支 `return` 绿灯分支那段（两支同文案）⇒ **3 红**（同上三条）。
  M3 删掉不相容分支里的 `model-generated` 那一句
     ⇒ **1 红**：`..._defers_to_the_structured_stance`。
  M4 把绿灯分支换成不相容分支那句（两支同文案）
     ⇒ **2 红**：`test_green_verdict_is_not_presented_as_an_order_clearance`
       ＋ `test_note_lands_in_text_not_only_in_structured_content`。
  📌 我原先预测 M1/M2/M4 各只红一条，**实测各多红一条** —— 记实测值，
     免得下一个人看到「多红了」以为自己改坏了别的东西。
"""
import asyncio

import server
from mcp.types import CallToolResult

GREEN = {"answer": "推荐把酸缓慢加入水中。",
         "tool_results": [{"tool": "check_all_compatibility", "result": {"matrix": [
             {"chemical_a": "sulfuric acid", "chemical_b": "water",
              "level": "no_known_incompatibility", "verdict": "no_known_incompatibility",
              "reason": "classified, no known incompatibility (not a safety guarantee)"}]}}],
         "intent": "compatibility"}

RED = {"answer": "严禁混合。",
       "tool_results": [{"tool": "check_all_compatibility", "result": {"matrix": [
           {"chemical_a": "bleach", "chemical_b": "hydrochloric acid",
            "level": "incompatible", "verdict": "incompatible",
            "reason": "releases chlorine gas"}]}}],
       "intent": "compatibility"}


def _run(payload, a="sulfuric acid", b="water"):
    async def _fake_quick(message, **_):
        return payload
    server._quick_chat = _fake_quick
    return asyncio.run(server.check_mixing_order(a, b))


def test_green_verdict_is_not_presented_as_an_order_clearance(monkeypatch):
    """绿灯共存判定 ⇒ 文本里必须出现「顺序未判定 / 不是放行」。"""
    monkeypatch.setattr(server, "_quick_chat", None, raising=False)
    res = _run(GREEN)
    text = res.content[0].text

    assert isinstance(res, CallToolResult)
    assert "Addition order: not determined" in text
    assert "not* a clearance" in text or "not a clearance" in text.replace("*", "")
    # 🔴 **没动不该动的**：共存判定本身必须原样保留（改红是新的误伤）
    assert "no known incompatibility" in text.lower() or "no_known_incompatibility" in text
    # 结构化侧也要有，供只读 structuredContent 的客户端
    assert res.structured_content["addition_order"]["verdict"] == "not_determined"


def test_incompatible_branch_still_says_incompatible(monkeypatch):
    """🔴 与绿灯那条**必须取到不同的文案**，否则这段就是恒定的免责声明、不判别任何东西。

    不相容那一维要照说（这是两支里唯一带 INCOMPATIBLE 的），且不许退化成绿灯分支的话。
    """
    monkeypatch.setattr(server, "_quick_chat", None, raising=False)
    text = _run(RED, "bleach", "hydrochloric acid").content[0].text

    assert "INCOMPATIBLE" in text
    assert "documented, engineered procedure" in text
    # 两支不同文案：绿灯分支那句抬头不许出现在这里
    assert "Addition order: not determined by any structured source" not in text


def test_incompatible_branch_does_not_invent_an_order_determination(monkeypatch):
    """🔴 CI-1078：不相容 ⇒「不存在安全的加料顺序」是**外推**，引擎没有顺序维度。

    它还与同一次返回的 `addition_order.verdict = not_determined` 自相矛盾，
    而 SPM / SC-1 / SC-2 这些**故意配制**的工艺对恰恰都被判 incompatible。
    """
    monkeypatch.setattr(server, "_quick_chat", None, raising=False)
    res = _run(RED, "bleach", "hydrochloric acid")
    text = res.content[0].text

    # 🔴 回归钉：CI-1078 之前那两句字面
    assert "there is no safe addition order" not in text.lower()
    assert "do not combine them in either direction" not in text.lower()
    # 文本与结构化层不许再互相矛盾：两边都停在「未判定」
    assert res.structured_content["addition_order"]["verdict"] == "not_determined"
    assert "no addition order has been determined" in text


def test_incompatible_branch_defers_to_the_structured_stance(monkeypatch):
    """🔴 散文里的顺序建议**和禁令**都是模型产的，这句得说出来。

    只说「建议是模型产的」不够——CI-1078 实测被当成作业指令的恰恰是**禁令**那一半。
    """
    monkeypatch.setattr(server, "_quick_chat", None, raising=False)
    text = _run(RED, "bleach", "hydrochloric acid").content[0].text

    assert "model-generated" in text
    assert "prohibition" in text


def test_structured_note_states_why_the_evidence_cannot_exist(monkeypatch):
    """🔴 「未判定」必须说清是**结构性的**，不是这次恰好没查到。

    否则下一个人会去「补数据」，而系统里根本没有可补的那个维度。
    """
    monkeypatch.setattr(server, "_quick_chat", None, raising=False)
    ao = _run(GREEN).structured_content["addition_order"]
    assert "no addition-order dimension" in ao["reason"]
    assert "model-generated" in ao["reason"]
    assert "COEXIST" in ao["not_a_clearance"]


def test_note_lands_in_text_not_only_in_structured_content(monkeypatch):
    """🔴 多数 MCP 客户端**只读 text** ⇒ 只塞进 structuredContent 等于没修。"""
    monkeypatch.setattr(server, "_quick_chat", None, raising=False)
    res = _run(GREEN)
    assert "not determined" in res.content[0].text
