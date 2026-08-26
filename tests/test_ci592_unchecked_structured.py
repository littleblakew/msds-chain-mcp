"""CI-592 —— 「本次未检查」在 MCP 这条通道上不能只靠散文。

## 形状

CI-587 把纠正段确定性地顶到 `answer` 最前面。网页快聊 / Slack / Teams 原样贴，是真确定性；
**这条通道不是**：`_quick_result` 把 `answer` 拼成一段工具文本，交给 claude.ai / Copilot 的
客户端模型**重写**之后才到用户眼前。而 CI-587 的原始事故正是从
`validate_protocol_chemicals` 上观察到的。

## 两层，各自管一件事（别把它们读成一件）

1. **`[unchecked]` 指令行**（本文件主要守的东西）——写给那个会改写我们文本的模型看的。
   形状照抄 [[CI-567]] 的 `[protocol]`：那次实测证明「把正确内容放进载荷」不够，
   是配对的显式禁令把模型扳回来的。🔴 它必须在 `answer` **之前**：同仓已有 prod 实证
   说明这层模型按位置取舍（CI-89-followup：靠后的 SDS 链接被丢）。
2. **structuredContent 里的 `unchecked` 字段**——给不经模型改写的消费者
   （ChatGPT Apps SDK 那类）。🔴 **别把它当成「到达用户」的证据**：本仓另一处注释写着
   多数 MCP 客户端只读 text、`structuredContent` 不进模型上下文（见
   `test_ci595_raw_appendix_keeps_verdicts.py`）。

## 天花板（写在这里，免得下一个人把绿灯读成「模型照做了」）

这两层都只钉住**我们发出去的载荷**。客户端模型有没有照做，本层验不了，也不该假装验得了
——那要走 [[agent-eval-framework]] 的实调采样（报比例 + 报 n）。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential


@pytest.fixture(autouse=True)
def _credential():
    set_caller_credential("sk-msds-test")
    yield
    set_caller_credential(None)


def _payload(**over) -> dict:
    data = {
        "answer": "⚠️ The following chemicals were **not checked** ...\n\nAcetone: flammable.",
        "tool_results": [{"tool": "search_chemical", "result": {"query": "acetone"}}],
        "documents": [],
        "intent": "risk",
        "unchecked": ["toluene", "ethyl acetate"],
    }
    data.update(over)
    return data


def _directive_of(text: str) -> str:
    """从整段文本里抠出那条指令。

    🔴 别用 `text.split("\\n\\n")[0]`：那样写的话「措辞红线」这几条会**顺带**依赖指令
    的位置，于是一次挪位置的变异会让它们跟着红——红得对不上理由，就等于没告诉你哪坏了。
    """
    start = text.index("[unchecked]")
    end = text.find("\n\n", start)
    return text[start:end if end != -1 else None].lower()


def _ask(payload, monkeypatch):
    async def _fake(*a, **kw):
        return payload
    monkeypatch.setattr(server, "_quick_chat", _fake)
    return asyncio.run(server.ask_chemical_safety("what about these?"))


def test_directive_names_the_unchecked_chemicals(monkeypatch):
    """变异：把 `_unchecked_directive(...)` 从 `text` 的拼接里去掉 ⇒ 红。"""
    text = _ask(_payload(), monkeypatch).content[0].text

    assert "[unchecked]" in text
    assert "toluene" in text and "ethyl acetate" in text


def test_directive_comes_before_the_answer(monkeypatch):
    """🔴 位置是判据的一部分，不是排版偏好。

    变异：把 `_unchecked_directive(...)` 挪到 `answer` 后面 ⇒ 红。
    依据是同仓的 prod 实证（CI-89-followup）：靠后的内容被客户端模型丢掉。
    """
    text = _ask(_payload(), monkeypatch).content[0].text

    assert text.index("[unchecked]") < text.index("Acetone: flammable"), text[:400]


def test_directive_forbids_claiming_absence(monkeypatch):
    """指令必须**双向**：既要它说，又要它别把「没查」说成「没有」。

    只写「你要说一声」的话，模型完全可以照做并同时写「我们库里没有这些」——那正是
    CI-243 / CI-322 / CI-334 三次事故的原话。变异：删掉 `MUST NOT state or imply`
    那一句 ⇒ 红。
    """
    directive = _directive_of(_ask(_payload(), monkeypatch).content[0].text)

    assert "must" in directive
    assert "not state or imply" in directive


def test_directive_wording_never_asserts_absence_itself(monkeypatch):
    """🔴 与后端模板同源的措辞红线：我们自己这句也不许出现「未收录 / 建议上传」。

    变异：把指令文案改成 "we have no record of them, ask the user to upload an SDS" ⇒ 红。
    """
    directive = _directive_of(_ask(_payload(), monkeypatch).content[0].text)

    for forbidden in ("not found", "no record", "no data", "upload", "not in our database"):
        assert forbidden not in directive, f"指令自己断言了不存在：{forbidden}"


@pytest.mark.parametrize("over", [{"unchecked": []}, {}])
def test_no_directive_when_nothing_was_unchecked(monkeypatch, over):
    """空列表、以及老后端（键缺失）⇒ 都不出这段。噪声会让真出现时没人看。

    变异：把 `_unchecked_directive` 的空值判断去掉（改成无条件渲染）⇒ 红。
    """
    payload = _payload(**over)
    if not over:
        payload.pop("unchecked")
    text = _ask(payload, monkeypatch).content[0].text

    assert "[unchecked]" not in text


def test_structured_content_carries_the_field(monkeypatch):
    """给只读 structuredContent 的客户端。变异：把 `_expose` 换回手抄 dict ⇒ 红。"""
    sc = _ask(_payload(), monkeypatch).structured_content

    assert sc["unchecked"] == ["toluene", "ethyl acetate"]


def test_structured_content_passes_through_future_backend_fields(monkeypatch):
    """🔴 判据打在**失败模式本身**（照 CI-342 的写法）。

    断言「`unchecked` 在」只证明我这次记得加了它；下一个后端新增字段照样静默消失。
    用一个我们代码里根本不存在的键，才是在守「白名单没被改回来」。
    变异：把 `_quick_result` 的 structuredContent 改回逐字段手抄 ⇒ 红。
    """
    sc = _ask(_payload(__new_backend_field__="sentinel"), monkeypatch).structured_content

    assert sc.get("__new_backend_field__") == "sentinel", "后端新增的顶层字段没到客户端"


def test_internal_keys_never_leak(monkeypatch):
    """透传的代价是内部键会跟着出去 ⇒ 显式挡掉的那两个必须真的被挡住。

    变异：把 `drop=_QUICK_INTERNAL_KEYS` 去掉（用默认 `_INTERNAL_KEYS`）⇒ `_timed_out` 泄露 ⇒ 红。
    """
    sc = _ask(_payload(_usage={"cost": 1}, _timed_out=True), monkeypatch).structured_content

    assert "_usage" not in sc
    assert "_timed_out" not in sc


def test_answer_and_tool_results_still_reach_the_text(monkeypatch):
    """没动不该动的：本票是纯加法，`answer` 与原始附录必须原样还在。"""
    text = _ask(_payload(), monkeypatch).content[0].text

    assert "Acetone: flammable" in text
    assert "search_chemical" in text
