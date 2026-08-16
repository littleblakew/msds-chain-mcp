"""测试自己不许写生产——2026-08-16 量出来的，不是假想。

`server.API_URL` 默认就是 Prod 后端，而 `_log_call` 是 fire-and-forget（失败吞成一条
日志）⇒ 任何一个「只断言返回文案、没 stub 干净」的测试都会安静地往 Prod 写一行调用日志，
**本地和 CI 都看不出来**。Prod 上的实测：`platform.mcp_call_logs` 里 2026-07-29 以来
82 条 `upload_msds_pdf` 失败，**79 条是我们自己的测试**（`input_params` 里留着
`/tmp/pytest-of-runner/...`），真实外部用户只有 1 条。

为什么这值得一条守卫而不只是「记得 stub」：脏的是 [[CI-174]]/[[CI-136]] 用来判断
「用户还卡不卡得住」的那两张表——**污染不会报错，只会让下一个读漏斗的人把自己的
测试当成用户需求**。同族形状：[[CI-527]] 里启动预热把身份解析表冲垮。
"""
import server


_PROD_HOSTS = ("msds-chain-backend-prod", "msdschain.lagentbot.com", "mcp.lagentbot.com")


def test_api_url_under_test_is_not_prod():
    """反向变异：删掉 conftest 里那行 `setdefault("MSDS_API_URL", ...)`，本条必红。"""
    assert not any(h in server.API_URL for h in _PROD_HOSTS), (
        f"测试进程的 API_URL 指着生产（{server.API_URL}）——任何没 stub 干净的调用都会"
        f"写进 Prod 的 mcp_call_logs / contribution_funnel_events，而那是漏斗判据的来源。"
        f"conftest 应把它指向丢弃端口。"
    )


def test_api_url_points_somewhere_that_refuses_connections():
    """指向本机丢弃端口，而不是某个会成功的地址：漏网的真实请求要**立刻失败并可见**，
    不能改成写到另一个能收的地方（那只是把污染搬了个家）。"""
    assert server.API_URL.startswith("http://127.0.0.1:"), server.API_URL
