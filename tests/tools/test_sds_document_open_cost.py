"""`get_sds_document`：发链接不扣费、打开链接才扣，价格要在用户点之前说出来。

后端在 `/sds-document-url` 上给 `open_cost_credits`（0 ＝不收费）。多数客户端只把 text
喂给模型 ⇒ 价格必须进 text，structuredContent 里也原样带上。打开链接时 GET 拿到的是
确认页、POST 才拿到 PDF ⇒ 给脚本的用法提示不能再是 `curl -O`。
"""
import asyncio

import pytest

import server


class _Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self, body):
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        return _Resp(self._body)


async def _no_log(*_a, **_kw):
    pass


def _call(monkeypatch, **extra):
    from request_identity import set_caller_credential
    set_caller_credential("sk-msds-test")
    body = {"available": True, "chemical_name": "Acetone", "cas": "67-64-1",
            "supplier": "Sigma-Aldrich", "pdf_url": "/msds/token/abc123", **extra}
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: _Client(body))
    monkeypatch.setattr(server, "_log_call", _no_log)
    res = asyncio.run(server.get_sds_document("acetone"))
    return res.content[0].text, res.structured_content


def test_states_the_open_price_before_the_click(monkeypatch):
    text, sc = _call(monkeypatch, open_cost_credits=1)
    assert "charges 1 credit;" in text, text
    assert "same day" in text, text
    assert sc["open_cost_credits"] == 1


@pytest.mark.parametrize("extra", [{"open_cost_credits": 0}, {}])
def test_says_no_price_when_free_or_unknown(monkeypatch, extra):
    text, sc = _call(monkeypatch, **extra)
    assert "charges" not in text, text
    assert sc["open_cost_credits"] == extra.get("open_cost_credits")


def test_tells_scripts_to_post(monkeypatch):
    text, _ = _call(monkeypatch)
    assert "curl -O" not in text
    assert "POST" in text
