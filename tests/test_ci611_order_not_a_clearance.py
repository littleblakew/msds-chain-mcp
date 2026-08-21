"""CI-611：共存判定不能被读成加料顺序的绿灯。

🔴 这条缺陷的形状：`check_mixing_order("硫酸","水")` 拿到
`verdict=no_known_incompatibility` —— 那是**共存问题的正确答案**，
却出现在一个**问顺序**的工具里，而这一对的全部危险恰恰在顺序上。

🔴 修法是**纯加法**：不把共存判定改红（改红是新的误伤），只显式声明「顺序未判定」。
所以守卫必须同时钉住两件事：**说了该说的** ＋ **没动不该动的**。
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


def test_incompatible_pair_says_no_safe_order_instead(monkeypatch):
    """🔴 与上一条**必须取到不同的文案**，否则这段就是恒定的免责声明、不判别任何东西。

    不相容对上说「顺序未判定」是错的——正确的话是「没有安全的加料顺序」。
    """
    monkeypatch.setattr(server, "_quick_chat", None, raising=False)
    text = _run(RED, "bleach", "hydrochloric acid").content[0].text

    assert "no safe addition order" in text
    assert "Addition order: not determined" not in text


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
