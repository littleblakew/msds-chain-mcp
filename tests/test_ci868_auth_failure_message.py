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
| 🔴 M8 review 后补：`upload_msds_pdf` 的外部 URL 分支改回 `_raise_for_status_with_reason` | 红 | 红 |
| M9 review 后补：去掉 `logger.warning("auth_failed"…)` | 红 | 红 |
| M10 review 后补：401 措辞去掉 `MSDS_API_KEY` 那半 | 红 | 红 |
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


# ---------------------------------------------------------------------------
# 🔴 review 抓到的三条（全部先回代码核实再改）
# ---------------------------------------------------------------------------

def test_external_url_403_is_not_blamed_on_our_account(monkeypatch, caplog):
    """🔴 **本轮最贵的一条**：`upload_msds_pdf` 也调 `_raise_for_status_with_reason`，
    但它那次调用打的是**调用方给的、指向别人主机**的 URL。

    供应商门户的 403（Cloudflare / 需登录 / 签名链接过期）会因此收到
    「你的 MSDS Chain 账号没有权限，重连也没用」——**自信、具体、而且错**，
    正是本票要消灭的那种失败，只是换了个地方发生。而且它比改之前**更差**：
    裸 `raise_for_status()` 至少带着那个外部 URL，成因还追得回来。
    [[feedback-safety-fix-made-it-worse]]
    """
    set_caller_credential("sk-msds-test")
    try:
        monkeypatch.setattr(server.httpx, "AsyncClient", _client_returning(_Resp(403)))
        # 🔴 用例第一版假设它**返回**一段文字，实际是**抛出** —— 两者在「消息内容对不对」
        # 上完全同形，只有跑起来才分得开。判据仍打在调用方最终看到的那段字上。
        with pytest.raises(Exception) as exc:
            asyncio.run(server.upload_msds_pdf(
                pdf_source="https://supplier.example.com/sds/acetone.pdf"))
    finally:
        set_caller_credential(None)
    text = str(exc.value)
    assert "from the source host, not from MSDS Chain" in text, text
    # 🔴 三条都不许出现：它们全是关于**我们账号**的断言
    assert "Reconnecting will NOT help" not in text, text
    assert "reconnect the MSDS Chain connector" not in text, text
    assert "not permitted to make this call" not in text, text


def test_external_url_401_also_not_blamed_on_our_account(monkeypatch):
    """同上，401 那半（基本认证的链接）。两个状态码各测一次——
    只测一个的话，另一个的分支被改回去时这份守卫是绿的。"""
    set_caller_credential("sk-msds-test")
    try:
        monkeypatch.setattr(server.httpx, "AsyncClient", _client_returning(_Resp(401)))
        # 🔴 用例第一版假设它**返回**一段文字，实际是**抛出** —— 两者在「消息内容对不对」
        # 上完全同形，只有跑起来才分得开。判据仍打在调用方最终看到的那段字上。
        with pytest.raises(Exception) as exc:
            asyncio.run(server.upload_msds_pdf(
                pdf_source="https://supplier.example.com/sds/acetone.pdf"))
    finally:
        set_caller_credential(None)
    text = str(exc.value)
    assert "from the source host, not from MSDS Chain" in text, text
    assert "reconnect" not in text.lower(), text


def test_backend_detail_is_logged_even_though_it_is_not_told_to_the_model(monkeypatch, caplog):
    """🔴 初版那句注释是**假话**：它写着「排障要的信息在 `mcp_call_logs.error_message` 里」，
    而实际上这里既不记也不留，且 401 样板本身 593 字符 > `_error_text` 的 500 上限
    ⇒ 那一列存的是**被截断的禁令、零后端信号**。

    方向尤其坏：CI-868 的根因至今未定，而能把「token 过期 / key 吊销 / 10-key 上限挤掉」
    分开的**唯一线索就是这个 `detail`**——初版把它在唯一看得见它的地方丢掉了。
    ⚠️ 记的是抽出来的 `detail`，**不是原始响应体**。
    """
    import logging
    with caplog.at_level(logging.WARNING, logger="msds_mcp"):
        text = _tool_error_text(monkeypatch, 401, {"detail": "invalid_api_key"})
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "auth_failed" in logged and "invalid_api_key" in logged, logged
    assert "status=401" in logged, logged
    # 反向：仍然不许告诉模型
    assert "invalid_api_key" not in text, text


def test_401_remedy_covers_self_hosted_stdio_callers(monkeypatch):
    """🔴 本 server 也服务 stdio 自托管调用方（直接设 `MSDS_API_KEY`）——
    他们**没有 connector、也没有授权流可以重跑**。初版无条件叫人「重连 connector」，
    等于让他们去做一件对他们不存在的事。
    """
    text = _tool_error_text(monkeypatch, 401)
    assert "MSDS_API_KEY" in text, text
    assert "reconnect the MSDS Chain connector" in text, "远程那半也要留着"
