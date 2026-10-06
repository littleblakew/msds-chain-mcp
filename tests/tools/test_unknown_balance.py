"""CI-904：余额未知时不许说「没扣费」——那是拿假话代替未知，且假在对用户有利那侧。

**缺陷**：`_parse_usage` 在 `X-Msds-Credits-Balance` 缺失时默认 `-1`，而 `_usage_line`
把 `bal < 0` 当**订阅哨兵** ⇒ 同一个取值编码了两件事：「订阅，没扣费」与「我不知道余额」。
于是一次**真的扣了 10 credits** 的调用，逐字印出
`💳 Included in your plan (no credits deducted).`

🔴 **为什么必须靠守卫**：说错的方向**对用户有利** —— 被多扣钱的人会投诉，被告知「没扣费」
的人不会。**这条缺陷没有用户反馈通道**（同族 [[feedback-safety-fix-made-it-worse]]：
「最坏的输出是什么」要显式问一遍）。

🔴 **判据打在真实返回给调用方的那行文字上**，不是 `_usage_line` 的单元返回值：
本票的载体是工具输出末尾那一行，中间任何一环（`_with_usage` 没接、`_billed_json` 没附
`_usage`）断掉都得在这里露出来。

🔴 **四种输入各自断言**，只验「未知」那一格会漏掉误伤方向：
① 订阅 ⇒ 仍要说「Included in your plan」  ② 余额已知 ⇒ 要报余额
③ **余额未知且 cost>0** ⇒ 要说扣了多少、**不许出现 no credits deducted**、也不许编一个余额
④ 后端没计量（无 `X-Msds-Credits-Cost`）⇒ 一行都不加

🔴 **反向变异（实测，结论和我预期的不一样，所以逐条写下来）**：

| 变异 | 结果 |
|---|---|
| ① `_parse_usage` 默认改回 `"-1"` | **仍绿** |
| ② `_usage_line` 哨兵改回 `or bal < 0` | **仍绿** |
| ①+② **合并** | **③ 与 ⑤ 红**（＝完全退回事故代码） |
| ③ compliance 的 `_saw_usage` 改回 `_usage_bal is not None` | ⑤ 红 |

🔴 **①②单独变异仍绿不等于这条守卫是空跑** —— 修复是**两层**的，撤掉任一层都被另一层吸收：
默认值退回 `-1` 时，`_usage_line` 已经按「负数也算未知」处理；哨兵退回 `bal < 0` 时，
默认值已经是 `None`。**据此写下「这条守卫没用」会删掉一个真的在防事故的守卫**
（本仓 [[my-own-guards-are-often-no-ops]] 的镜像面：要造**合并变异**）。
⇒ 下一个人重验时**请直接跑合并变异**，别只撤一层。
"""
import asyncio

import pytest

import server
from mcp.types import CallToolRequestParams
from request_identity import set_caller_credential

_PAYLOAD = {"results": [{"chemical_name": "acetone", "cas": "67-64-1",
                         "storage_class": "general"}]}


class _Resp:
    status_code = 200

    def __init__(self, headers):
        self.headers = headers

    def json(self):
        return _PAYLOAD

    def raise_for_status(self):
        pass


def _client(headers):
    class _C:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp(headers)
        async def get(self, *a, **k): return _Resp(headers)
    return _C


@pytest.fixture(autouse=True)
def _cred(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(server, "_log_call", _noop)
    set_caller_credential("sk-msds-test")
    yield
    set_caller_credential(None)


# 🔴 **必须挑真的会渲染用量行的工具**。只有 5 个工具走 `_with_usage`
# （`check_chemical_compatibility` / `get_chemical_risk_warnings` /
# `check_regulatory_compliance` / `get_audit_report` / `batch_safety_check`）；
# 别的工具走 `_strip_usage`，**压根不打印这一行** ⇒ 拿它们当用例，
# 「没说没扣费」会恒真，守卫全程绿而什么都没测（本仓
# [[guard-cases-must-let-later-key-terms-decide]]：用例要让该决胜的条件真的决胜）。
# 我第一版挑的就是 `get_storage_guidance`，三条当场红在「连用量行都没有」上。
def _text_of(monkeypatch, headers, tool="get_chemical_risk_warnings", args=None):
    """跑一个真实工具，返回调用方最终看到的那段文字。"""
    monkeypatch.setattr(server.httpx, "AsyncClient", _client(headers))
    res = asyncio.run(server.mcp._handle_call_tool(
        None, CallToolRequestParams(name=tool, arguments=args or {"chemicals": ["acetone"]})))
    return "".join(c.text for c in res.content if getattr(c, "text", None))


_NO_DEDUCT = "no credits deducted"


def test_subscription_still_says_included_in_plan(monkeypatch):
    """① 真订阅：这句话本来就是对的，别在修复里把它弄丢。"""
    text = _text_of(monkeypatch, {"X-Msds-Credits-Cost": "10",
                                  "X-Msds-Credits-Balance": "-1",
                                  "X-Msds-Credits-Reason": "subscription"})
    assert _NO_DEDUCT in text, f"订阅那句丢了：{text[-200:]!r}"


def test_known_balance_is_reported(monkeypatch):
    """② 余额已知：照常报数（这条守的是「别把正常路径也改哑了」）。"""
    text = _text_of(monkeypatch, {"X-Msds-Credits-Cost": "10",
                                  "X-Msds-Credits-Balance": "40",
                                  "X-Msds-Credits-Reason": ""})
    assert "used 10 credits" in text, text[-200:]
    assert "Balance: 40 credits remaining" in text, text[-200:]


def test_unknown_balance_never_claims_nothing_was_deducted(monkeypatch):
    """③ 🔴 本票正题：余额 header 缺失 + cost>0。"""
    text = _text_of(monkeypatch, {"X-Msds-Credits-Cost": "10",
                                  "X-Msds-Credits-Reason": ""})
    assert _NO_DEDUCT not in text, (
        f"余额未知却声称没扣费——而这一次扣了 10 credits：{text[-200:]!r}")
    assert "used 10 credits" in text, f"扣了多少也没说：{text[-200:]!r}"
    # 不许凭空编一个余额（`-1` 曾经就是那个编出来的数）
    assert "Balance:" not in text, f"余额未知却报了余额：{text[-200:]!r}"


def test_unmetered_call_adds_no_usage_line(monkeypatch):
    """④ 后端根本没计量（连 cost header 都没有）⇒ 一行都不该加。"""
    text = _text_of(monkeypatch, {})
    assert "💳" not in text, f"没计量却加了用量行：{text[-200:]!r}"


def test_compliance_still_reports_usage_when_balance_is_unknown(monkeypatch):
    """⑤ 🔴 隐藏的第三处：`check_regulatory_compliance` 自己聚合 usage。

    它原来用 `if _usage_bal is not None` 当「这轮有没有计量」的判据 —— 把默认值从 `-1`
    改成 `None` 之后，那个判据会**把整条用量行吞掉**，而那次调用其实是扣了钱的。
    **修一个地方的默认值，会让另一个地方拿它当哨兵的判据静默失效**：这一格就是为它写的。
    """
    text = _text_of(monkeypatch,
                    {"X-Msds-Credits-Cost": "10", "X-Msds-Credits-Reason": ""},
                    tool="check_regulatory_compliance",
                    args={"chemicals": ["acetone"], "regions": ["EU"]})
    assert "used 10 credits" in text, f"用量行被吞掉了：{text[-300:]!r}"
    assert _NO_DEDUCT not in text, text[-300:]
