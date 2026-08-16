"""CI-410：422 的原因在响应体里，而错误路径从不读它。

此前调用方拿到的是 `Client error '422 Unprocessable Entity' for url …` —— **不是哑失败**
（调用确实可见地失败了），但**不可行动**：模型看不出是「化学品超过 24 个」还是「参数名写错」，
于是要么原样重试、要么放弃。而 pydantic 早就把原因放在 `detail` 里了。同 [[CI-523]] 一族：
**信息在，只是没到达读它的人**。

🔴 判据打在**工具真正返回给调用方的那段文字**上，不是 `_detail_text` 的返回值：
中间任何一环没接（`_billed_json` 没特判、异常没带上原因）都得在这里露出来。
今天刚在告警脚本上栽过一次「测试自己把两个函数拼起来 ⇒ 生产接线没被测」
（见 [[green-run-that-executed-nothing]] 第五种）。
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential


class _Resp:
    def __init__(self, payload, status=422):
        self._p = payload
        self.status_code = status
        self.headers: dict = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("不该走到 raise_for_status——422 应该在它之前被特判")

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


def _client_returning(resp):
    class _C:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return resp
        async def get(self, *a, **kw): return resp
    return _C


@pytest.fixture(autouse=True)
def _cred(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(server, "_log_call", _noop)
    set_caller_credential("sk-msds-test")
    yield
    set_caller_credential(None)


def _tool_error_text(monkeypatch, payload) -> str:
    """跑一个真实工具，把调用方最终看到的错误文字取出来。"""
    monkeypatch.setattr(server.httpx, "AsyncClient", _client_returning(_Resp(payload)))
    with pytest.raises(Exception) as exc:  # noqa: PT011 — 就是要看它带了什么话
        asyncio.run(server.check_chemical_compatibility(chemicals=["a", "b"]))
    return str(exc.value)


def test_pydantic_validation_reason_reaches_the_caller(monkeypatch):
    """🔴 本票的正题，用 CI-367 真实加的那个上限当用例（`chemicals` 最多 24 个）。"""
    text = _tool_error_text(monkeypatch, {"detail": [
        {"loc": ["body", "chemicals"], "msg": "List should have at most 24 items",
         "type": "too_long"}]})
    assert "422" in text
    assert "at most 24 items" in text, f"原因没到调用方手里：{text!r}"
    assert "chemicals" in text, f"没说是哪个字段：{text!r}"


def test_httpexception_string_detail_also_reaches_the_caller(monkeypatch):
    """后端另一种形状：`HTTPException(422, detail="…")` ⇒ detail 是字符串。"""
    text = _tool_error_text(monkeypatch, {"detail": "protocol_text is empty"})
    assert "protocol_text is empty" in text, text


def test_unparseable_body_says_so_without_dumping_it(monkeypatch):
    """🔴 解析不了就说「后端没给出原因」，**绝不把原始响应打出来**——响应体可能带凭证
    或客户文档字节（同族 [[ps-leaks-credentials-from-command-lines]]）。"""
    text = _tool_error_text(monkeypatch, None)
    assert "no machine-readable reason" in text, text
    assert "422" in text


def test_the_message_is_bounded(monkeypatch):
    """这句话会进工具返回值；一个超长响应体不能把真正的原因埋掉。"""
    text = _tool_error_text(monkeypatch, {"detail": "y" * 5000})
    assert len(text) < 600, len(text)
    assert "…" in text, "没截断"


def test_paths_that_skip_billed_json_also_carry_the_reason(monkeypatch):
    """🔴 review 抓到的完整性缺口：`create_audit_session` / `upload_msds_pdf` 直接打
    `/sessions*`，**不经过 `_billed_json`** ⇒ 补之前它们的 422 仍是裸状态行。

    同一张票的同一种缺陷，只是发生在两个不路由到计费包装的工具上。
    反向变异：把 `_build_audit_session` 里的 `_raise_for_status_with_reason` 换回
    `res.raise_for_status()`，本条必红。
    """
    monkeypatch.setattr(server.httpx, "AsyncClient", _client_returning(
        _Resp({"detail": [{"loc": ["body", "experiment_name"], "msg": "field required"}]})))
    with pytest.raises(Exception) as exc:
        asyncio.run(server.create_audit_session(experiment_name="x", chemicals=["acetone"]))
    text = str(exc.value)
    assert "422" in text and "field required" in text, text


def test_402_special_case_still_wins(monkeypatch):
    """阳性对照：402（余额耗尽）有自己的话术，别被新分支抢走。"""
    monkeypatch.setattr(server.httpx, "AsyncClient",
                        _client_returning(_Resp({"detail": {"balance": 0}}, status=402)))
    with pytest.raises(Exception) as exc:
        asyncio.run(server.check_chemical_compatibility(chemicals=["a", "b"]))
    assert "Credit balance exhausted" in str(exc.value)
