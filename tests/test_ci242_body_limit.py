"""CI-242：传输层的请求体上限必须容得下我们对外承诺的内联上传大小。

`upload_msds_pdf` 的 schema、描述、`_MAX_INLINE_PDF_BYTES` 三处都写着「内联 base64
最大 10 MB（解码后）」。10 MB 的 base64 是 ~13.4 MB，而 MCP SDK 的
`DEFAULT_MAX_REQUEST_BODY_SIZE` 是 **4 MiB** ⇒ 不显式抬高的话，超过 ~3 MB 的 PDF 会在
`RequestBodyLimitMiddleware` 被裸 413 拒掉，MCP 层根本不执行——工具代码里那些
「超过 10 MB 就好好报错」的分支永远到不了。

⚠️ 这个坑在 mcp 1.29 就存在（1.x 只是不让你改这个参数），不是 2.x 引入的。

判据打在**真发一个 HTTP 请求**上，不是打在常量算术上：只比较两个由同一个
`_MAX_INLINE_PDF_BYTES` 推导出来的数字，等于自己证明自己（恒真），中间件到底装没装、
装的是哪个值，都测不出来。
"""
import base64

import pytest
from starlette.testclient import TestClient

import server
import server_remote


def _post(body: bytes):
    # 不进 lifespan：中间件在 ASGI 链上，413 在任何 MCP 处理之前就返回了。
    client = TestClient(server_remote.app, raise_server_exceptions=False)
    return client.post(
        "/mcp",
        content=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )


def test_body_just_over_the_sdk_default_is_not_rejected():
    """4 MiB（SDK 默认上限）之上、我们承诺的上限之内的请求体，不能是 413。"""
    payload = b"A" * (5 * 1024 * 1024)          # > 4 MiB 默认值
    assert len(payload) < server_remote._MAX_BODY_BYTES
    r = _post(b'{"jsonrpc":"2.0","id":1,"params":{"x":"' + payload + b'"}}')
    assert r.status_code != 413, (
        "5 MiB 的请求体被传输层拒了 ⇒ 承诺的 10 MB 内联上传实际上做不到。"
        "检查 server_remote.streamable_http_app(max_request_body_size=...)"
    )


def test_full_size_inline_upload_fits():
    """按承诺的 10 MB 解码上限造一个真实大小的 base64 载荷，必须过得去传输层。"""
    encoded_len = len(base64.b64encode(b"\0" * server._MAX_INLINE_PDF_BYTES))
    body = b'{"jsonrpc":"2.0","id":1,"params":{"pdf_source":"' + b"A" * encoded_len + b'"}}'
    r = _post(body)
    assert r.status_code != 413, (
        f"承诺 10 MB 解码 ⇒ base64 {encoded_len} 字节，加信封 {len(body)} 字节，"
        f"却被传输层以 413 拒绝（当前上限 {server_remote._MAX_BODY_BYTES}）"
    )


# 抬高不等于取消：配置值必须留在一个理智的区间里。
# 🔴 这个上界还有第二个作用——**给下面那条用例的内存分配封顶**。初版直接
# `b"A" * (_MAX_BODY_BYTES + 1MB)`，探测大小跟着被测常量走；把常量变异成 10**12
# 做反向验证时，用例当场去要 1 TB 内存，把整台机器拖进内存压力（2026-08-15 实际发生，
# 连带 macOS 撤了进程的桌面访问授权）。**探测用的尺寸永远要有独立于被测值的封顶。**
_SANE_CEILING = 32 * 1024 * 1024


def test_the_limit_is_a_sane_number():
    """配置值必须在「够用」和「还算个限制」之间。先跑这条，它同时是下条的安全带。"""
    assert server._MAX_INLINE_PDF_BYTES < server_remote._MAX_BODY_BYTES <= _SANE_CEILING, (
        f"传输层上限 {server_remote._MAX_BODY_BYTES} 不在合理区间 "
        f"({server._MAX_INLINE_PDF_BYTES}, {_SANE_CEILING}] —— 要么装不下承诺的内联上传，"
        f"要么等于把这道防线拆了"
    )


def test_the_limit_is_still_a_limit():
    """超出上限的请求体仍应被传输层挡在门外，而不是被读进内存。

    没有这条，把上限设成无穷大也会让前两条变绿（那就等于删掉了防线）。
    """
    probe = min(server_remote._MAX_BODY_BYTES, _SANE_CEILING) + 1024 * 1024  # 封顶见上
    assert _post(b"A" * probe).status_code == 413


@pytest.mark.parametrize("attr", ["_MAX_INLINE_PDF_BYTES"])
def test_transport_limit_is_derived_not_hand_copied(attr):
    """上限必须由应用层常量推导。手抄一个数字 ⇒ 两边各改各的，迟早又对不上。"""
    assert server_remote._MAX_BODY_BYTES > getattr(server, attr), (
        "传输层上限必须严格大于应用层的解码上限（base64 会膨胀 4/3）"
    )
