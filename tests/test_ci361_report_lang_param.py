"""CI-361 ⑥：`get_audit_report` 要把 `lang` 传到后端，而且**不能**走 `_BACKEND_LANGS`。

在此之前这条通道压根没传 `lang` ⇒ 经 MCP 拿报告的人**永远只拿得到英文**，
而两侧都不报错（后端丢弃未知 query 参数）。

## 为什么必须是一份**单独**的语言集

`_BACKEND_LANGS = ("en", "zh")` 描述的是 quick-chat 那一族（答案由 LLM 现写，实测只有
en/zh 真照做）。PDF 报告不同：标签是**静态译好的**（后端 `REPORT_TRANSLATIONS` 五种语言
各 49 个 key），动态段走翻译服务 ⇒ **五种都是真的**，⑦ 已在 Prod 实测过 `id`。
拿 `_normalize_lang` 去卡报告 ＝ 把一个真能出德语报告的通道压成英文，**且不报错**。

## 判据打在「真正发出去的那个请求」上

工具签名加对了、调用点没传，两者在返回值上完全同形（都是一个能用的 URL）。

🔴 **拦法照本仓既有写法（替掉 `httpx.AsyncClient`），刻意不用 respx**：本仓没有别的测试用
它，而本机 `all_proxy` 指向 SOCKS ⇒ respx 那条路会在建 client 时就
`ImportError: socksio`（我第一版就是这么写的，13 条全红且**红的理由与被测行为无关**）。
🔴 而且**不能「抓最后一次请求」**：`_reported` 在 `finally` 里还会 POST 一次调用日志
（同 CI-1112 踩过的坑）⇒ 按 URL 挑出 `report/signed-url` 那一发；抓不到就断言失败，
不返回空 dict（空 dict 会让下游断言假绿）。

🔴 **变异（四条，方向不同，都实跑过）：**
· 调用点删掉 `params={"lang": …}` ⇒ 前两组红
· 把 `_normalize_report_lang` 换成 `_normalize_lang`（走 `_BACKEND_LANGS`）⇒
  `test_the_three_extra_langs_are_not_squeezed_to_english` 红（**本模块重点**）
· 往 `_REPORT_LANGS` 加一个描述里没写的语言 ⇒ `test_report_langs_match_the_declared_description` 红
  （**「集合变大」方向**，撤回改动测不出它）
· 把 `_REPORT_LANGS` 改成 `("en",)` ⇒ `test_report_langs_is_a_superset_of_backend_langs` 红
"""
import asyncio

import pytest

import server


def _signed_url_request(**kwargs) -> dict:
    """调一次 `get_audit_report`，把它发给 `report/signed-url` 那一发抓回来。"""
    captured: dict = {}

    class _Resp:
        status_code = 200
        headers: dict = {}

        @staticmethod
        def json():
            return {"url": "https://example.test/sessions/X/report/pdf?t=tok"}

        @staticmethod
        def raise_for_status():
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            if "report/signed-url" in url:      # 不是调用日志那一发
                captured["url"] = url
                captured["params"] = params or {}
            return _Resp()

        async def post(self, url, json=None, headers=None):
            return _Resp()

    orig_client = server.httpx.AsyncClient
    orig_cred = server.get_caller_credential
    server.httpx.AsyncClient = _Client
    server.get_caller_credential = lambda: "sk-msds-test"
    try:
        asyncio.run(server.get_audit_report(**kwargs))
    finally:
        server.httpx.AsyncClient = orig_client
        server.get_caller_credential = orig_cred
    assert "params" in captured, (
        "没抓到 report/signed-url 那一发 —— 这条断言没跑到被测的那一步")
    return captured


@pytest.mark.parametrize("given,expected", [
    ("de", "de"), ("ja", "ja"), ("id", "id"), ("zh", "zh"), ("en", "en"),
    ("DE", "de"), (" ja ", "ja"),            # 大小写 / 空白收敛
    (None, "en"), ("fr", "en"), ("", "en"),  # 不认识的一律英文（与后端拒绝面对齐）
])
def test_lang_reaches_the_backend(given, expected):
    got = _signed_url_request(session_id="DEMO-CI361G6", lang=given)
    assert got["params"].get("lang") == expected, got


@pytest.mark.parametrize("lang", ["ja", "de", "id"])
def test_the_three_extra_langs_are_not_squeezed_to_english(lang):
    """🔴 重点：报告支持的语言比 `_BACKEND_LANGS` 多，别共用那个元组。"""
    got = _signed_url_request(session_id="DEMO-CI361G6", lang=lang)
    assert got["params"].get("lang") == lang, f"{lang} 被压成了 {got['params'].get('lang')}"
    # 阳性对照：确实是另一套 —— 否则这条用例在两种实现下都绿
    assert server._normalize_lang(lang) == "en", "_BACKEND_LANGS 变了？这条对照失效了"


def test_report_langs_match_the_declared_description():
    """模型读的是**描述**那份 ⇒ 集合与描述必须一致，少一个语言模型就不会用它。"""
    desc = server.ReportLang.__metadata__[0].description
    for lang in server._REPORT_LANGS:
        assert f'"{lang}"' in desc, f"{lang} 在 _REPORT_LANGS 里但描述没提"
    quoted = {w.strip('",').strip('"') for w in desc.split()
              if w.startswith('"') and len(w.strip('",')) == 2}
    assert quoted <= set(server._REPORT_LANGS), f"描述宣称了集合外的语言：{quoted}"


def test_report_langs_is_a_superset_of_backend_langs():
    """🔴 方向性判据：报告那份**只能更宽**。反过来说明有人把两者搞混了。"""
    assert set(server._BACKEND_LANGS) < set(server._REPORT_LANGS)
