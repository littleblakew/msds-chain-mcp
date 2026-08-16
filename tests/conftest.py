import os
import sys

# Make the server modules (server.py, server_remote.py) importable
# from the repo root regardless of where pytest is invoked.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ---------------------------------------------------------------------------
# 🔴 测试跑起来**默认打的是 Prod**，必须在 import server 之前掐掉
# ---------------------------------------------------------------------------
# `server.API_URL` 的默认值就是 Prod 后端（`MSDS_API_URL` 没设时），而不是每个测试都
# stub 了 httpx / `_log_call`：`test_upload_missing_local_file_message_is_actionable`
# 这类「只断言返回文案」的测试会让 `_log_call` 真的发出去，fire-and-forget 又把失败
# 吞成一条日志 ⇒ **没有任何人会注意到**。
#
# 2026-08-16 在 Prod 上量到的后果：`platform.mcp_call_logs` + `contribution_funnel_events`
# 里 2026-07-29 以来 82 条 `upload_msds_pdf` 失败记录，**79 条是我们自己的测试**
# （CI 每次 Deploy 约 2 条，本地跑一次也各来一条；`input_params` 里留着
# `/tmp/pytest-of-runner/...` 这种一眼假的路径）。真实外部用户只有 1 条。
# 而这两张表正是 [[CI-174]]/[[CI-136]] 判断「用户还卡不卡得住」的依据 ——
# 不掐掉它，读这个漏斗的人会把自己的测试当成用户需求。
#
# 指向一个必然拒连的地址（丢弃端口），而不是 stub：**任何**没 stub 干净的真实请求
# 都会立刻失败并留在测试输出里，而不是安静地写进生产。
os.environ.setdefault("MSDS_API_URL", "http://127.0.0.1:9")


# ---------------------------------------------------------------------------
# 🔴 `live_client` 必须是 **session 级**，且只能有这一份
# ---------------------------------------------------------------------------
# `StreamableHTTPSessionManager.run()` 每个实例只能调一次，而 `server_remote.app` 是模块级
# 单例 ⇒ 两个测试文件各建一个「进 lifespan」的 TestClient，第二个必然
# `RuntimeError: task group already ...`。
#
# 这个坑**单文件跑不出来**：CI-515 的新测试自己跑 6 passed、`test_dual_transport` 自己跑
# 4 passed，只有全量一起跑才炸（2026-08-15 就是这样才发现的）。⇒ 新增需要 lifespan 的
# 测试文件时，**复用这个 fixture，别在自己文件里再 `with TestClient(app)`**。
#
# 另：modern（2026-07-28）那条腿**必须**在 lifespan 内才能用——不进 lifespan 直接 500。
import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def live_client():
    from server_remote import app
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client
