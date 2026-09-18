"""CI-905：零参报告路径撞 402 时，刚建好的 session 不能被丢掉。

`get_audit_report()` 不带参时先 `_build_audit_session`（**免费**），再去取 signed-url
（CI-892 之后**收 10 credits**）。余额在这中间耗尽 ⇒ `_billed_json` 抛
`Credit balance exhausted…`，而那句话里**没有 session_id**，函数也没有别的出口把它交出去
⇒ 用户充值后重问，模型只能再走一次零参路径，**又建一个 session**，前一个成孤儿行。

🔴 **这条路 2026-09-11 起在 Prod 上真的可达**：`eccb9558`（CI-892 的 review 修复）把
`select_meter("get_audit_report")` 挂到了 `GET /report/signed-url` 上，已含在 Prod 镜像
`f44689c5` 里。开票时（09-10）它还不可达，票面留的机械判据就是查这个：

    git show origin/main:backend/app/routers/report.py | grep -c 'select_meter'
    # 0 ⇒ 不可达；≥1 ⇒ 可达

🔬 **反向变异（两侧各造，都实跑过）**

· 该红的：把 `server.py` 里那个 `except ToolReason` 分支整段删掉（＝回到改动前的
  `billed = _billed_json(res)`）⇒ `test_402_after_building_hands_the_session_back` 红，
  且红的理由对（消息里没有 session id），不是「没抛异常」。
· 不该红的：把 `if built_here:` 改成 `if True:` ⇒
  `test_402_on_the_explicit_session_path_says_nothing_extra` 红。防的是
  「顺手给所有 402 都加一段复用提示」——调用方自己传 id 的那条路上什么都没丢，
  多说一句是噪音，而噪音不会有任何东西报错。

🔴 **判据打在「id 在不在消息里」，不打在「抛没抛」**：改动前也抛，只是抛的那句话不带 id
——两者在「有没有异常」上完全同形。同 `test_ci892_report_billing.py` 那条判词的形状。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential

_BUILT_SESSION = "DEMO-CI905TST"


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._p = payload
        self.status_code = status
        self.headers = headers or {}

    # reason phrase 由状态码算出来，别写死（同 CI-892 那份替身的理由）。
    def raise_for_status(self):
        if self.status_code >= 400:
            reason = {402: "Payment Required", 403: "Forbidden"}.get(
                self.status_code, "Error")
            raise RuntimeError(
                f"Client error '{self.status_code} {reason}' for url ...")

    def json(self):
        return self._p


class _FakeClient:
    """recent-chemicals 有货 ⇒ 零参路径会真的建 session；signed-url 则 402。"""

    def __init__(self, signed_url_resp, *, added=("acetone", "methanol")):
        self._signed = signed_url_resp
        self._added = list(added)

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        if "/api/v2/recent-chemicals" in url:
            return _Resp({"chemicals": self._added, "days": 7, "calls": 4})
        if "/report/signed-url" in url:
            return self._signed
        return _Resp({})

    async def post(self, url, json=None, headers=None, **kw):
        if url.endswith("/sessions"):
            return _Resp({"session_id": _BUILT_SESSION})
        if url.endswith("/chemicals"):
            requested = (json or {}).get("chemicals", [])
            return _Resp({
                "added": [{"name": c, "status": "added"} for c in requested],
                "not_found": [],
            })
        if url.endswith("/compatibility"):
            return _Resp({"matrix": [], "warnings": []})
        return _Resp({})


async def _noop_log(*a, **kw):
    return None


@pytest.fixture
def wired(monkeypatch):
    def _make(signed_url_resp, **kw):
        monkeypatch.setattr(
            server.httpx, "AsyncClient", _FakeClient(signed_url_resp, **kw))
        monkeypatch.setattr(server, "_log_call", _noop_log)
        set_caller_credential("sk-msds-test")
    yield _make
    set_caller_credential(None)


def test_402_after_building_hands_the_session_back(wired):
    """核心：零参 + 402 ⇒ 那句话里必须有刚建好的 session id。"""
    wired(_Resp({"detail": {"balance": 3}}, status=402))

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(server.get_audit_report())

    msg = str(ei.value)
    # 🔴 主判据：id 本身在消息里。没有它，重试只能再建一个。
    assert _BUILT_SESSION in msg, f"session id 没交回调用方：{msg!r}"
    # 原来那句话不能丢——用户仍要知道为什么失败、怎么解决
    assert "Credit balance exhausted" in msg, msg
    assert "Top up" in msg, msg
    # 裸状态行仍然不许漏（沿用 CI-892 的判词）
    assert "Payment Required" not in msg, f"裸状态行漏给调用方了：{msg!r}"


def test_402_message_tells_the_model_to_reuse_rather_than_rebuild(wired):
    """光有 id 不够：模型要被明确告知**复用**，否则它照样走零参路径。

    🔴 这条与上一条不是重复：上一条防「id 丢了」，这条防「id 在、但模型不知道
    拿它干什么」。CI-174 的整个设计就是让模型**优先零参调用**，所以默认行为
    正是「再问一次＝再建一个」——必须显式压过它。
    """
    wired(_Resp({"detail": {"balance": 0}}, status=402))

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(server.get_audit_report())

    msg = str(ei.value)
    assert f'get_audit_report(session_id="{_BUILT_SESSION}")' in msg, msg
    assert "second session" in msg, f"没说清重问的后果：{msg!r}"


def test_402_on_the_explicit_session_path_says_nothing_extra(wired):
    """🔴 不该红的那侧：调用方自己传了 id ⇒ 什么都没丢，别加噪音。

    误伤形态＝给所有 402 都贴一段「你的 session 已经建好了」——而这条路上
    session 是**调用方建的**，说这句话既多余又暗示我们替他建了东西。
    """
    wired(_Resp({"detail": {"balance": 3}}, status=402))

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(server.get_audit_report("DEMO-CALLER-OWNED"))

    msg = str(ei.value)
    assert "Credit balance exhausted" in msg, msg
    # 改动前这条路的文案一个字都不该变
    assert "was already created" not in msg, f"给不需要的那条路加了噪音：{msg!r}"
    assert "second session" not in msg, msg


def test_the_happy_path_is_untouched(wired):
    """阳性对照：不是 402 时，这次改动完全不参与。

    没有这条，上面三条在「`_billed_json` 永远抛」的坏实现下也会全绿。
    """
    wired(_Resp({"url": "/reports/signed/abc"}))

    res = asyncio.run(server.get_audit_report())
    text = res.content[0].text

    assert "/reports/signed/abc" in text, text
    assert res.structured_content["session_id"] == _BUILT_SESSION
    assert "was already created" not in text, text
