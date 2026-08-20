"""CI-588 —— `check_mixing_order` 的输出形状要钉住，别让模型自由成段。

## 为什么不是改措辞

Prod 实测出过自我缠绕的开头：「推荐把 稀释剂（少量的酸性溶液中逐滴加入水的原则）按常规
实验室惯例：将酸慢慢将酸加入大量水中可降低局部放热」——空格错位、括号里塞进一条与主句
相反的原则、「将酸慢慢将酸」重复。**结论方向是对的，但不能见人。**

🔴 2026-08-21 复采两次**没有复现**那一句 ⇒ 它是**间歇**的。追着措辞改会得到一个
「这次看起来好了」的假象；能真正减少它的是把**形状**钉住：固定小节 + 首行一句话 +
字数上限，让「把推荐顺序和危险顺序揉进同一句」这件事没有发生的余地。

## 这层测试守得住什么、守不住什么

守得住：**送给后端的指令仍然要求那个形状**（有人改文案时会红）。
🔴 守不住：模型这次听不听话。那只能靠 live 采样，而且**判据必须是多次**——
单次通过不算修好（本票就是被单次「看起来正常」耽误过一轮的）。
"""
import re

import server


def _message_for(chemical_a="A", chemical_b="B", context=""):
    """把工具体内那段 message 拼装原样取出来——不打网络。"""
    src = server.check_mixing_order.__doc__ or ""
    assert src, "工具没有 docstring，说明取错了对象"
    # 真正的判据在源码里的 message 常量上，用 inspect 拿。
    import inspect
    body = inspect.getsource(server.check_mixing_order)
    m = re.search(r'message = \((.*?)\n        \)', body, re.S)
    assert m, "没找到 message 拼装块——它被改名或重构了，这条守卫要跟着改"
    return m.group(1)


def test_the_two_orders_must_live_in_separate_blocks():
    """🔴 本票的靶子：推荐顺序与危险顺序**不许出现在同一句**。"""
    msg = _message_for()
    assert "RECOMMENDED ORDER" in msg and "DANGEROUS ORDER" in msg, msg[:200]
    assert "same sentence" in msg, (
        "缺少「不许写进同一句」的明令——那正是 Prod 上出问题的形状"
    )


def test_the_answer_starts_with_a_one_line_verdict():
    """首行必须是一句话结论（demo 截图第一眼要能看懂），且禁止复述问题。"""
    msg = _message_for()
    assert "single sentence" in msg
    assert "No preamble" in msg


def test_there_is_a_length_cap():
    """冗长是这条工具**更现实**的 demo 杀手：两次采样都是七八百字。"""
    msg = _message_for()
    assert re.search(r"under \d+ words", msg), msg[-200:]


def test_the_skeleton_is_closed():
    """别让模型自行加节——开放式结构是它把两件事揉一起的入口。"""
    msg = _message_for()
    assert "nothing else" in msg or "Do not add sections" in msg
