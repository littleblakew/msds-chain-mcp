"""CI-868：鉴权失败必须是一句可行动的话 + 一条给模型的禁令，不是一个不透明的工具错误。

**事故**（2026-09-07 08:28 UTC，Blake 在 chatgpt.com，`ua=openai-mcp/1.0.0`）：
core 打后端 `/api/v2/compatibility/check` 拿到 401（网关侧 `auth:"ok"`、`/oauth/token` 200
⇒ 断层在「网关认了客户端」与「它注入给后端的 per-user 凭证」之间，**根因另查**）。
此处当时直接 `raise_for_status()` ⇒ 调用方拿到
`HTTPStatusError: Client error '401 Unauthorized' for url 'https://msds-chain-backend-prod…'`。

🔴 **本票修的不是根因，是失败的形状** —— 而形状恰恰是危害发生的地方：
模型看不出这是「你的授权失效了」还是「我们坏了」，于是**转去用自身知识回答相容性**，
末尾才轻描淡写一句免责。⇒ 光把失败暴露出来不够，**必须显式禁止它替我们回答**
（[[CI-567]] 实测过：光把正确内容放进载荷不够，是配对的禁令把模型扳回来的）。

🔴 判据打在**工具真正返回给调用方的那段文字**上，不是 `_AUTH_FAILED_MSG` 的字面
——中间任何一环没接都得在这里露出来（同 `test_ci410_422_reason.py` 的理由，
[[green-run-that-executed-nothing]] 第五种）。

## 记录的变异（🔴 每条都实测过）

| 变异 | 应该 | 实测 |
|---|---|---|
| M1 去掉 401/403 分支（退回 `res.raise_for_status()`） | 红 | 红 |
| M2 401 和 403 共用同一句话 | 红 | 红（403 那条「重连没用」） |
| M3 把 `_detail_text(res)` 拼进去（泄露内部代号 + 走近泄露原始响应） | 红 | 红 |
| M4 把 402 也塞进 `_AUTH_FAILED_MSG` | 红 | 🔴 **第一版存活** —— 402 在 `_billed_json` 里先被特判，而我只测了那条路；补了 `/sessions*` 那条（不经 `_billed_json`）之后才红 |
| M5 把 500 也塞进去（把「后端挂了」说成「你没授权」） | 红 | 红 |
| 🔴 M6 **反向**：删掉禁令那两句、只留「401 未授权」 | 红 | 红 |
| 🔴 M7 **反向**：把「NOT a statement that we lack data」改成「we have no data for this」 | 红 | 红 |
"""
import asyncio

import pytest

import server
from request_identity import set_caller_credential


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._p = payload
        self.headers: dict = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(
                f"RAW raise_for_status: Client error '{self.status_code}' for url "
                f"'https://msds-chain-backend-prod.orangepond-4b408d49."
                f"southeastasia.azurecontainerapps.io/api/v2/compatibility/check'")

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


def _tool_error_text(monkeypatch, status, payload=None) -> str:
    """跑一个真实工具，把调用方最终看到的错误文字取出来。"""
    monkeypatch.setattr(server.httpx, "AsyncClient",
                        _client_returning(_Resp(status, payload)))
    with pytest.raises(Exception) as exc:  # noqa: PT011 — 就是要看它带了什么话
        asyncio.run(server.check_chemical_compatibility(chemicals=["67-56-1", "67-64-1"]))
    return str(exc.value)


# --- 靶心 -------------------------------------------------------------------

def test_401_reaches_the_caller_as_an_actionable_sentence(monkeypatch):
    text = _tool_error_text(monkeypatch, 401, {"detail": "Not authenticated"})
    assert "401" in text
    assert "reconnect the MSDS Chain connector" in text, f"没给可行动的下一步：{text!r}"
    assert "RAW raise_for_status" not in text, "还在走裸 raise_for_status"


def test_401_forbids_the_model_from_answering_anyway(monkeypatch):
    """🔴 本票真正要防的那件事：模型转去用自身知识回答相容性。

    事故里它就是这么做的（末尾才一句「不应冒充工具结果」）。**禁令必须在**，
    而且必须同时堵住两条：别自己答、别推断「安全/相容」。
    """
    text = _tool_error_text(monkeypatch, 401)
    assert "MUST NOT answer the question from your own knowledge" in text, text
    assert "infer that anything is safe or compatible" in text, text
    assert "Nothing was looked up." in text, text


def test_401_is_not_readable_as_a_safety_conclusion(monkeypatch):
    """🔴 失败方向很坏：「工具报错」到「所以大概没事」只有一步。

    ⚠️ **这里不做「禁词」断言**：这句话里本来就含 `safe` / `compatible`，
    它们全在否定句里（`MUST NOT ... infer that anything is safe or compatible`）
    ——按裸子串判会**被自己的反例命中**（[[testing-unreliability-seven-forms]]：
    自由文本判据被反例命中）。所以判据是「否定句在不在」，不是「词出现没出现」。
    """
    text = _tool_error_text(monkeypatch, 401)
    assert "it is NOT a safety finding" in text.replace("— ", ""), text
    assert "NOT a statement that we lack data" in text, \
        "不许说成「我们没有这个数据」——我们根本没查成（CI-243/322/334 同形）"


def test_401_does_not_leak_the_internal_backend_url(monkeypatch):
    """🔴 原来那串完整 Azure 后端 URL 直接进了第三方客户端的上下文。"""
    text = _tool_error_text(monkeypatch, 401, {"detail": "password_auth_disabled"})
    assert "azurecontainerapps.io" not in text, text
    assert "https://" not in text, text
    # 🔴 后端 401 的 detail 是内部代号，对调用方无信息量且是实现泄露 ⇒ 不许附上
    assert "password_auth_disabled" not in text, text


def test_403_says_reconnecting_will_not_help(monkeypatch):
    """🔴 401＝没认出你（重连有用）；403＝认出了但不给（重连没用）。

    两者共用一句话的代价是实打实的：叫一个 403 的用户反复重连，是浪费他的时间
    并把真正的原因藏起来（memory：401＝认证≠授权）。
    """
    text = _tool_error_text(monkeypatch, 403)
    assert "403" in text
    assert "Reconnecting will NOT help" in text, text
    assert "reconnect the MSDS Chain connector" not in text, "403 不该叫人去重连"
    # 禁令两条对 403 同样要在
    assert "MUST NOT answer the question from your own knowledge" in text, text


# --- 阳性对照 / 不该被抢走的分支 ---------------------------------------------

def test_402_still_wins(monkeypatch):
    """余额耗尽有自己的话术，别被新分支抢走（`_billed_json` 在调本函数之前特判）。"""
    text = _tool_error_text(monkeypatch, 402, {"detail": {"balance": 0}})
    assert "Credit balance exhausted" in text, text
    assert "reconnect" not in text.lower(), "402 被鉴权话术抢走了"


def test_402_on_the_path_that_skips_billed_json_is_not_dressed_as_auth(monkeypatch):
    """🔴 **变异 M4 存活才发现的缺口**：上一条只走了 `_billed_json` 那条路，
    而 402 恰好在**它里面**、在调用本函数**之前**就被特判掉 ⇒ 往 `_AUTH_FAILED_MSG`
    里加一个 402 在那条路上是**真的没有影响**，测试当然抓不到。

    但 `_raise_for_status_with_reason` 还有**第二条调用路径**（`_build_audit_session`
    直接打 `/sessions*`，不经 `_billed_json`）——在那里加 402 会把「余额没了」
    说成「你没授权」，让用户去做一件毫无用处的事。
    **判据的作用域是我手写的，而 M4 正好落在我没写到的那一半。**
    ⚠️ 这条**不主张** 402 在这条路上有友好话术（它今天没有，是既有缺口、不在本票范围），
    只主张它**不许被冒充成鉴权问题**。
    """
    set_caller_credential("sk-msds-test")
    try:
        monkeypatch.setattr(server.httpx, "AsyncClient",
                            _client_returning(_Resp(402, {"detail": {"balance": 0}})))
        with pytest.raises(Exception) as exc:
            asyncio.run(server.create_audit_session(
                experiment_name="x", chemicals=["acetone"]))
    finally:
        set_caller_credential(None)
    text = str(exc.value)
    assert "reconnect" not in text.lower(), f"402 被鉴权话术冒充了：{text!r}"
    assert "Not authorized" not in text, text


def test_422_reason_still_reaches_the_caller(monkeypatch):
    """CI-410 的回归：新分支不许挡住它。"""
    text = _tool_error_text(monkeypatch, 422, {"detail": [
        {"loc": ["body", "chemicals"], "msg": "List should have at most 24 items"}]})
    assert "422" in text and "at most 24 items" in text, text


def test_500_is_not_dressed_up_as_an_auth_problem(monkeypatch):
    """🔴 「后端挂了」不许被说成「你没授权」——那会让用户去做一件毫无用处的事，
    而真正的故障没有任何人看见。5xx 仍走 `raise_for_status()`。
    """
    text = _tool_error_text(monkeypatch, 500)
    assert "RAW raise_for_status" in text, text
    assert "reconnect" not in text.lower(), text


def test_paths_that_skip_billed_json_also_get_it(monkeypatch):
    """🔴 完整性：`create_audit_session` 直接打 `/sessions*`，**不经 `_billed_json`**。

    CI-410 的 review 就是在这里抓到缺口的；本票挂在同一个收口上，所以它自动覆盖——
    但**自动覆盖也要有用例**，否则下一个人把 401 挪进 `_billed_json` 时这条路会静默退回。
    """
    # 🔴 **必须先设凭证**：`create_audit_session` 在发出任何请求**之前**就因
    # `not get_caller_credential()` 早退成一段文字。第一版漏了这一步 ⇒ 用例
    # `DID NOT RAISE`，而它看起来完全像「代码没接上」。
    # 那是「绿跑但什么都没执行」的镜像面：**红了也可能什么都没执行**。
    set_caller_credential("sk-msds-test")
    try:
        monkeypatch.setattr(server.httpx, "AsyncClient", _client_returning(_Resp(401)))
        with pytest.raises(Exception) as exc:
            asyncio.run(server.create_audit_session(
                experiment_name="x", chemicals=["acetone"]))
    finally:
        set_caller_credential(None)
    assert "reconnect the MSDS Chain connector" in str(exc.value), str(exc.value)
