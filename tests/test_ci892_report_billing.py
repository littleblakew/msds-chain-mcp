"""CI-892：`get_audit_report` 是唯一一个不走计费响应处理的取值型工具。

`_billed_json` 在 `server.py` 里有 19 个调用点，`get_audit_report` **一个都不在**——
它直接 `_raise_for_status_with_reason(res)` + `res.json()["url"]`。两个后果：
①后端计量了也不会有 `💳` 那行 ②`_raise_for_status_with_reason` **不处理 402**
（只管 422/401/403 后 `raise_for_status()`）⇒ 余额耗尽落到裸
`Client error '402 Payment Required'`。这不是推断，是那个函数 docstring 自己写的契约。

今天无害（这条路后端零扣费）；CI-892 要让报告收 10 credits，那一刻它变成
**扣钱且零提示**。本仓先修是安全的：后端不发 usage header 时 `_with_usage` 是 no-op。

🔴 **反向变异（两侧各一个，都实跑过）**
· 该红的：把 `billed = _billed_json(res)` 改回 `_raise_for_status_with_reason(res)`
  + `res.json()["url"]` ⇒ `test_metered_report_surfaces_the_charge` 与
  `test_exhausted_balance_gets_the_actionable_message` 双红。
· 不该红的：后端不计量（无 header）时 `test_unmetered_report_is_unchanged_for_the_caller`
  断言输出与改动前**逐字相同** —— 防的是「顺手给所有人加了一行 💳 0 credits」这类误伤。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._p = payload
        self.status_code = status
        self.headers = headers or {}

    # 🔴 reason phrase 由状态码算出来，**不许写死 "Payment Required"**。
    # `test_exhausted_balance_gets_the_actionable_message` 拿「`Payment Required`
    # 没漏进消息」当「裸状态行没泄露」的判词；写死的话，这个替身将来被复用到
    # 500/503 时会**替一个非 402 的状态伪造出那句话**，判词就不再是它自称的意思了。
    # （review 抓到的，成立。）
    def raise_for_status(self):
        if self.status_code >= 400:
            reason = {402: "Payment Required", 403: "Forbidden",
                      404: "Not Found", 500: "Internal Server Error"}.get(
                          self.status_code, "Error")
            raise RuntimeError(
                f"Client error '{self.status_code} {reason}' for url ...")

    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, report_resp):
        self._report = report_resp

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        if "/report/signed-url" in url:
            return self._report
        return _Resp({})

    async def post(self, url, json=None, headers=None, **kw):
        return _Resp({})


async def _noop_log(*a, **kw):
    return None


@pytest.fixture
def wired(monkeypatch):
    def _make(report_resp):
        monkeypatch.setattr(server.httpx, "AsyncClient", _FakeClient(report_resp))
        monkeypatch.setattr(server, "_log_call", _noop_log)
        set_caller_credential("sk-msds-test")
    yield _make
    set_caller_credential(None)


def test_metered_report_surfaces_the_charge(wired):
    """后端计量了 ⇒ 用户看得到扣了多少、还剩多少。

    这正是 CI-892 上线后的形状（报告 10 credits）。没有这条，用户被扣钱且零提示。
    """
    wired(_Resp({"url": "/reports/signed/abc"}, headers={
        "X-Msds-Credits-Cost": "10",
        "X-Msds-Credits-Balance": "40",
        "X-Msds-Credits-Reason": "report",
    }))

    res = asyncio.run(server.get_audit_report("DEMO-123"))
    text = res.content[0].text

    assert "/reports/signed/abc" in text, text
    assert "10 credits" in text, f"扣了 10 credits 却没告诉用户：{text!r}"
    assert "40 credits remaining" in text, text
    # structuredContent 那半也要有——读结构化的客户端不看文本
    assert res.structured_content["usage"]["cost"] == 10.0, res.structured_content
    assert res.structured_content["usage"]["balance"] == 40.0


def test_unmetered_report_is_unchanged_for_the_caller(wired):
    """🔴 不该红的那侧：后端没计量时输出**一个字都不能变**。

    改动前这条路径就没有 `💳`，`_with_usage` 在 `_usage` 缺席时必须 no-op。
    误伤形态＝给每个不收费的报告都加一行「Free lookup (0 credits)」。
    """
    wired(_Resp({"url": "/reports/signed/abc"}))

    res = asyncio.run(server.get_audit_report("DEMO-123"))
    text = res.content[0].text

    assert "/reports/signed/abc" in text, text
    assert "💳" not in text, f"没计量却加了计费行：{text!r}"
    assert "usage" not in (res.structured_content or {}), res.structured_content


def test_exhausted_balance_gets_the_actionable_message(wired):
    """402 ⇒ 说人话，别把裸状态行丢给模型。

    🔴 判据打在**内容**上不只是「抛了异常」：改动前也会抛，只是抛的是
    `Client error '402 Payment Required' for url ...`——两者在「有没有异常」上完全同形。
    """
    wired(_Resp({"detail": {"balance": 3}}, status=402))

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(server.get_audit_report("DEMO-123"))

    msg = str(ei.value)
    assert "Credit balance exhausted" in msg, msg
    assert "Remaining: 3 credits" in msg, msg
    assert "Top up" in msg, msg
    assert "Payment Required" not in msg, f"裸状态行漏给调用方了：{msg!r}"
