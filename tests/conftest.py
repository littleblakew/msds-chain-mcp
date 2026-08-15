import os
import sys

# Make the server modules (server.py, server_remote.py) importable
# from the repo root regardless of where pytest is invoked.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


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
