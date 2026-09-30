"""CI-1135：会话容器是批量分析的**副产物**，而副产物有三条不许违反的性质。

## 为什么是副产物而不是「下一步」

同一个「会话＝分析容器」在 Web 上是主力，在 MCP 上是 0。前两次尝试都失败：
① day-1 在返回里挂一句「想要报告就调 `create_audit_session`」⇒ 6 个外部用户、0 个会话；
② CI-174 让 `get_audit_report()` 零参可调、自动从最近分析建会话 ⇒ 至今外部 1 次。
**共同形状是「还要求用户/agent 多走一步」** —— 而在 MCP 里编排权永远在 host 的 agent，
任何多走一步的设计都会被它省掉。⇒ 本票取消这一步。

## 这个文件钉的四件事

1. **并发，不串行** —— 会话那条路与 batch 各自最多吃 `TIMEOUT_MULTI`；串起来等于把这个
   工具的延迟预算翻倍。🔴 串成 `await` 之后**功能完全正常**，只是慢一倍 ⇒ 没有守卫就
   没人会发现。
2. **失败绝不影响答案** —— 会话超时/报错时，用户照样要拿到安全数据。
3. **匿名调用不建会话** —— `POST /sessions` 把会话绑到 key 所有者；匿名建出来的会话
   没有归属、用户取不到报告，只会往 `demo.sessions` 堆垃圾行。
4. 🔴 **前向红线：它今天不额外收费，而那是配置不是代码。** 后端把
   `create_audit_session` 记作 `_zero_priced("session_create")`，而那段注释明写
   「把 `VALUE_CREDITS[result_type]` 改成非 0 就开始收费，**不需要改任何代码**」
   ⇒ 定价那天，这个静默副产物会**开始向没要求它的用户收费，而没有任何东西会报警**。

## 🔴 变异记录（没记变异的守卫默认当它不存在）

- 把 `asyncio.gather(...)` 拆成两个顺序 `await` ⇒ `test_batch_and_session_run_concurrently` 红。
- 把 `except Exception` 收窄成 `except ValueError` ⇒
  `test_session_failure_never_breaks_the_answer[TimeoutError]` 与 `[RuntimeError]` 红。
- 把尾部改回「请你去调 `create_audit_session`」⇒ `test_the_tail_states_a_fact_not_a_request` 红。
🔴 **本文件第一版用 `@pytest.mark.asyncio` 写，被 `test_ci977_no_unlisted_pytest_plugin` 拦下**
  ——`pytest-asyncio` 不在 `requirements-dev.txt` 里，**我本机恰好装了、CI 不会有**，
  而它会在 deploy 的 Run tests 那步红并**堵住 Prod 部署**。改成仓里既有写法（`asyncio.run`，
  133 个文件都这么写）。📌 **重写之后上面四条变异全部重跑过** —— 换了写法的守卫，
  旧的变异记录不算数。
- 去掉 `if not get_caller_credential(): return None` ⇒ `test_anonymous_callers_get_no_session` 红。
- 🔴 第 4 条那条前向红线**没法在本仓变异**（价目表在后端仓）⇒ 它钉的是「本仓对那个前提的
  依赖被写下来了」，判据是**本仓源码里有没有那句依赖声明**。这是它能达到的上限，
  写在这里免得下一个人以为它验过了后端。
"""
import asyncio
import inspect
import re

import pytest

import server
from request_identity import set_caller_credential


@pytest.fixture(autouse=True)
def _anon():
    set_caller_credential(None)
    yield
    set_caller_credential(None)


def test_batch_and_session_run_concurrently():
    """🔴 判据打在**源码结构**上，因为「串行」在行为上与并发无法区分 —— 只是慢一倍。

    两条路各自最多吃 `TIMEOUT_MULTI`（45s）。串起来，这个工具的延迟预算就从 45s 变 90s，
    而返回值、字段、文案**一模一样** ⇒ 没有任何功能测试会红，用户只会觉得「有点慢」。
    """
    src = inspect.getsource(server.batch_safety_check)
    assert "asyncio.gather(" in src, (
        "批量分析与会话容器不再并发 —— 串行不会改变任何输出，只会把这个工具的延迟预算"
        "翻倍（两条路各自 TIMEOUT_MULTI），而那不会被任何功能测试抓到。")
    # 阳性对照：确认我们真的读到了函数体（装饰器换了写法时这条会红，而不是静默空跑）
    assert "_direct_batch(" in src and "_session_from_batch(" in src, src[:200]


def test_anonymous_callers_get_no_session(monkeypatch):
    """匿名调用不建会话 —— 建出来的没有归属，用户也取不到报告。"""
    called = []

    async def _should_not_run(**kw):
        called.append(kw)
        return {"session_id": "S-NOPE"}

    monkeypatch.setattr(server, "_build_audit_session", _should_not_run)
    set_caller_credential(None)
    assert asyncio.run(server._session_from_batch(["acetone", "bleach"])) is None
    assert not called, f"匿名调用也去建会话了：{called}"


def test_authenticated_callers_get_a_session(monkeypatch):
    """反方向：有凭证时必须真的建 —— 否则上面那条会在「永远不建」时也绿。"""
    async def _ok(**kw):
        assert kw["chemicals"] == ["acetone", "bleach"], kw
        return {"session_id": "DEMO-ABC123"}

    monkeypatch.setattr(server, "_build_audit_session", _ok)
    set_caller_credential("sk-msds-test")
    assert asyncio.run(server._session_from_batch(["acetone", "bleach"])) == "DEMO-ABC123"


@pytest.mark.parametrize("boom", [
    TimeoutError("slow"),
    RuntimeError("backend said no"),
    ValueError("garbage json"),
])
def test_session_failure_never_breaks_the_answer(monkeypatch, boom):
    """🔴 副产物的第一性质：它坏了，答案照样要出。

    会话那条路多走两个 HTTP 端点，失败面比 batch 大。把它的异常放出来，就等于让一个
    **附加功能**去决定用户能不能拿到安全数据 —— 方向完全错了。
    """
    async def _boom(**kw):
        raise boom

    monkeypatch.setattr(server, "_build_audit_session", _boom)
    set_caller_credential("sk-msds-test")
    assert asyncio.run(server._session_from_batch(["acetone"])) is None


def test_missing_session_id_in_response_is_treated_as_no_session(monkeypatch):
    """后端回了 200 但没给 `session_id` ⇒ 当作没有，别把 `None` 拼进文案。

    🔴 「200 不等于那件事做成了」在本仓已有先例（CI-1137 的清单上传）。
    """
    for payload in ({}, {"session_id": None}, {"session_id": ""}, None):
        async def _weird(**kw):
            return payload
        monkeypatch.setattr(server, "_build_audit_session", _weird)
        set_caller_credential("sk-msds-test")
        assert asyncio.run(server._session_from_batch(["acetone"])) is None, payload


def test_the_tail_states_a_fact_not_a_request():
    """🔴 前两次失败都是因为那句话是「请你再调一个工具」。

    会话是**已经建好的** ⇒ 文案必须陈述事实。判据不是「读起来像不像请求」（那没法机械判），
    而是**那句话里必须出现已建好的 session id**，且不得出现 `create_audit_session`
    —— 后者正是前两次那句「你去调它」的载体。
    """
    src = inspect.getsource(server.batch_safety_check)
    tail = src[src.index("if session_id:"):]
    assert "{session_id}" in tail, "尾部没有把已建好的 session id 放进去"
    assert "create_audit_session" not in tail, (
        "尾部又在让用户去调 `create_audit_session` —— 那是 day-1 与 CI-174 两次失败的"
        "同一个形状（要求多走一步，会被 agent 省掉）")


def test_the_zero_price_precondition_is_written_down():
    """🔴🔴 **前向红线，钉在「会被解除的那个条件」上，不是钉在手边的符号上。**

    这个副产物之所以可以静默发生，前提是后端把 `create_audit_session` 记作
    `_zero_priced("session_create")` ⇒ **0 credits**。而那是**配置不是代码**：后端那段
    注释明写「把 `VALUE_CREDITS[result_type]` 改成非 0 就开始收费，**不需要改任何代码**」。

    ⇒ 定价那天，`batch_safety_check` 会**开始向没有要求它的用户收费**，而
    **本仓不会有任何东西红** —— 价目表在另一个仓，跨仓 CI 看不见。

    **这条守卫能做到的上限**：钉住「本仓把这个依赖写下来了」。它证明不了后端今天仍是 0。
    ⇒ 谁要给 `session_create` 定价，**必须先回答「批量分析可不可以静默产生一笔费用」**，
    而这段文字就是那个问题的存放处。**别因为它「只是检查注释」就删掉它**：
    这个依赖没有别的守卫，也没有票。
    """
    src = inspect.getsource(server._session_from_batch) + inspect.getsource(server)[:0]
    module = inspect.getsource(inspect.getmodule(server._session_from_batch))
    block = module[:module.index("async def _session_from_batch")]
    assert "_zero_priced" in block or "_zero_priced" in src, (
        "本仓不再写明「这个副产物依赖后端把 session_create 记作零价」—— "
        "那个依赖没有别的守卫，也没有票，这段文字是它唯一的存放处")
    assert re.search(r"VALUE_CREDITS", block + src), (
        "没写明「定价只需改配置、不需改代码」这一半 ⇒ 读的人会以为零价是代码保证的")
