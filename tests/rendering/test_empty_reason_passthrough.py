"""CI-770/CI-413 —— 空结果的成因要**原样转述**，不许被那句写死的兜底盖掉。

本工具原本对任何空结果都回：
    No chemicals found matching "X" in the MSDS Chain database.
那是一句**对库存的事实断言**，而在两种情况下它是假的（后端已经能分辨，见 CI-770）：
  · `71-43` —— 按策略**没查**（CI-693 的闸）。说成「库里没有」还会把用户推向
    「上传 SDS」，而系统本来就没查，传了也不解决他的问题（Prod agent 面实测 4/4）。
  · `浓硫酸` —— 我们没有**这个形态**的条目；母体 `7664-93-9` 在 canonical 有 5 行。

⇒ 后端 `GET /chemicals?with_reason=1` 现在会带回 `unresolved.reason`；本仓的职责只有
一条：**它说得出成因时就转述它**，别用一句更强的话覆盖。

| 用例 | 变异 |
|---|---|
| `test_reason_replaces_the_hardcoded_line` | 删掉转述分支 ⇒ 回到写死那句 ⇒ 红 |
| `test_fallback_survives_a_backend_that_says_nothing` | 转述分支不判空 ⇒ 空结果变成一句空话 ⇒ 红 |
| `test_bare_list_backend_still_works` | 老后端（返回裸 list）⇒ 必须仍走兜底，不许 500 |
| `test_with_reason_is_actually_requested` | 忘了传 `with_reason` ⇒ 后端永远不会给成因 ⇒ 红 |

🔴 `test_with_reason_is_actually_requested` 是**接线**那一条：转述逻辑写得再对，
不请求那个参数就永远拿不到东西——本仓踩过的「修了但没到达」是同一个形状。
"""
import asyncio

import server


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """记下最后一次 GET 的 params，好让「接线」也能被断言。"""

    last_params: dict | None = None

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, *_a, **kw):
        _FakeClient.last_params = kw.get("params")
        return _FakeResponse(self._payload)


def _search(payload, query="anything"):
    orig_client, orig_key, orig_log = (
        server.httpx.AsyncClient, server._require_api_key, server._log_call
    )

    async def _no_log(*_a, **_k):
        return None

    server.httpx.AsyncClient = lambda *a, **k: _FakeClient(payload)
    server._require_api_key = lambda: None
    server._log_call = _no_log
    try:
        res = asyncio.run(server.search_chemical_database(query))
        return res.content[0].text if hasattr(res, "content") else res
    finally:
        (server.httpx.AsyncClient, server._require_api_key,
         server._log_call) = orig_client, orig_key, orig_log


_REASON = ('71-43: this looks like a partial number (a catalog, EC or range fragment), '
           'not a chemical identity, so we did NOT search by it.')


def test_reason_replaces_the_hardcoded_line():
    txt = _search({"chemicals": [], "unresolved": {
        "query": "71-43", "unresolved_kind": "non_identifying_query", "reason": _REASON}},
        query="71-43")

    assert txt == _REASON, f"成因没被转述，模型读到的还是那句兜底：{txt!r}"
    # 🔴 那句写死的断言不许出现——它说的是「库里没有」，而我们根本没查
    assert "No chemicals found" not in txt


def test_form_variant_reason_is_passed_through():
    reason = "浓硫酸：这指的是一个特定形态（「浓」），而我们没有这个形态的条目。"
    txt = _search({"chemicals": [], "unresolved": {
        "query": "浓硫酸", "unresolved_kind": "form_variant_no_entry", "reason": reason}},
        query="浓硫酸")
    assert txt == reason


def test_fallback_survives_a_backend_that_says_nothing():
    """后端说不出成因（`unresolved: None`）⇒ 仍走兜底那句。

    ⚠️ 这是**常态不是异常**：真实的语料缺口就是没有额外可说的那一种。
    """
    txt = _search({"chemicals": [], "unresolved": None}, query="nothingness")
    assert 'No chemicals found matching "nothingness"' in txt


def test_bare_list_backend_still_works():
    """🔴 老后端返回**裸 list** ⇒ 不许炸，仍走兜底。

    两仓各自部署，本仓可能先上线。`data.get` 在 list 上会 AttributeError，
    所以取值必须先判类型——这条就是钉那一步的。
    """
    txt = _search([], query="oldbackend")
    assert 'No chemicals found matching "oldbackend"' in txt


def test_with_reason_is_actually_requested():
    """🔴 接线：请求里必须带 `with_reason`，否则后端永远不会给成因。"""
    _search([], query="whatever")
    assert _FakeClient.last_params, "没记到 params —— 本用例的前提没成立"
    assert _FakeClient.last_params.get("with_reason"), (
        f"没请求 with_reason ⇒ 上面那些转述逻辑永远拿不到东西：{_FakeClient.last_params}")
