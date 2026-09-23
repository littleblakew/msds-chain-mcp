#!/usr/bin/env python3
"""CI-822 —— 把后端导出的 `/api/v2` 契约刷新成本仓的 vendored 副本。

后端（PRIVATE 仓 `msds-chain`）在 `backend/tool_surface.canonical.json` 里导出**两个面**：
`api_v2`（HTTP 端点契约）与 `agent_tools`（进程内 agent 工具注册表，含管理动作）。
本仓**是 PUBLIC 的**，所以这个脚本的第一职责不是「拷贝」，是**裁剪**。

## 🔴 裁剪必须发生在**写盘这一步**

后端那份文件里有 `_for_mcp_guard: "api_v2"` 标记，方向是对的，但它是**给消费者的提示**——
要求消费者记得去读。提示挡不住「直接 cp 一份过来」，而公开仓的 git 历史删不掉：
内部面一旦推上去，撤回的 commit 只是让它不在 HEAD 上，内容永远留在历史里。
⇒ 闸在**落盘之前**（`assert_safe_to_write`），不在 review 之后。

## 🔴 裁剪边界：`api_v2` ∩「公开 `server.py` 真的调用的端点」

只取 `api_v2` 还不够。后端 `api_v2` 里含有**只被私有网关调用**的端点
（当前是 `GET /api/v2/whoami`），它在公开 `server.py` 里零命中。
按「本仓真的调用的端点」求交集之后，**净新增对外暴露 ＝ 0**。

这比「那条端点价值低，可以接受」强一档：**前者是事实，后者是判断**，
而判断会在下一个人手里重新做一遍，事实不会。
📌 未被调用的端点对守卫**零价值**（它不是 MCP 工具，没有可比对的调用点）⇒ 裁掉零损失。

## 🔴 结构性的东西走 `ast`，不走正则

调用点是 `client.post(f"{API_URL}/api/v2/x", json={...})` 这种**结构**，不是文本。
正则会栽在多层装饰器、跨行实参这类形状上，而**它的失败方向是「少认出几个」**
——少认出的结果是守卫更松，且与「本来就只有这么多」完全同形。

用法：

    python3 scripts/refresh_backend_surface.py                 # 默认找隔壁的 msds-chain
    python3 scripts/refresh_backend_surface.py --backend <path to tool_surface.canonical.json>
    python3 scripts/refresh_backend_surface.py --check         # 只校验不写
"""
from __future__ import annotations

import argparse
import ast
import datetime as _dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER_PY = REPO / "server.py"
VENDORED = REPO / "backend_surface.vendored.json"

# 后端仓通常就在隔壁（两个仓都在 products/ 下）。找不到时由 --backend 指定。
DEFAULT_BACKEND = REPO.parent / "msds-chain" / "backend" / "tool_surface.canonical.json"

API_PREFIX = "/api/v2"

# 🔴 顶层键用**白名单**不用黑名单：后端将来新增第三个面时，
#    黑名单（挡掉我想得到的）会安静地放它过去，白名单（只放行我明确要的）不会。
ALLOWED_TOP_LEVEL = {"_ci822", "endpoints", "source"}

# 🔴 内部面的哨兵名。它们出现在数据段里 ＝ 裁剪失效。
_INTERNAL_MARKERS = (
    "agent_tools", "msds_admin_action", "msds_quality_stats", "fork_session",
    "review_msds_quality", "recheck_report", "finalize_and_get_contributions",
    "check_readiness", "update_session_info",
)


# --------------------------------------------------------------------------
# 从 server.py 抽「我们真的调用了哪些端点、带哪些参数」
# --------------------------------------------------------------------------

def _fstring_path(node: ast.AST) -> str | None:
    """`f"{API_URL}/api/v2/risk-warnings"` → `/api/v2/risk-warnings`。

    只认「插值 + 字面量后缀」这一种形状；认不出就返回 None，**不要在这里猜**
    ——猜错会静默少算一个端点，而少算的方向是「守卫更松」。
    """
    if not isinstance(node, ast.JoinedStr):
        return None
    tail = "".join(v.value for v in node.values if isinstance(v, ast.Constant)
                   and isinstance(v.value, str))
    return tail if tail.startswith(API_PREFIX) else None


def server_calls(src: str) -> dict[tuple[str, str], set[str]]:
    """→ {(path, METHOD): {调用时送出的参数名}}。"""
    calls: dict[tuple[str, str], set[str]] = {}
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr not in ("post", "get"):
            continue
        if not node.args:
            continue
        path = _fstring_path(node.args[0])
        if path is None:
            continue
        sent: set[str] = set()
        for kw in node.keywords:
            if kw.arg not in ("json", "params"):
                continue
            if isinstance(kw.value, ast.Dict):
                for k in kw.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        sent.add(k.value)
            else:
                # 实参是变量/推导式 ⇒ 静态**看不全**送了什么。
                # 🔴 记成哨兵而不是空集：空集会让「必填参数没送」那条检查假阳性地通过。
                sent.add("*")
        calls.setdefault((path, fn.attr.upper()), set()).update(sent)
    return calls


# --------------------------------------------------------------------------
# 裁剪
# --------------------------------------------------------------------------

def canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def build(backend_doc: dict, calls: dict[tuple[str, str], set[str]]) -> dict:
    wanted = set(calls)
    kept = [ep for ep in backend_doc["api_v2"] if (ep["path"], ep["method"]) in wanted]

    return {
        "_ci822": (
            "本仓是 PUBLIC。这份文件只含后端 /api/v2 契约中**本仓真的调用的那些端点**；"
            "后端的内部工具注册表面**刻意不在这里**，见 "
            "scripts/refresh_backend_surface.py 的模块 docstring。"
        ),
        "endpoints": sorted(kept, key=lambda e: (e["path"], e["method"])),
        "source": {
            # 🔴 戳的是**后端未裁剪的全量 sha**，不是裁完之后的 sha：
            #    裁完再哈希的话，后端新增一个我们不调用的端点 → 这边 sha 不变 →
            #    「后端动过」这件事被自己的裁剪吃掉。
            "api_v2_sha256": backend_doc["api_v2_sha256"],
            "api_v2_endpoint_count": backend_doc["api_v2_endpoint_count"],
            # 旁证元数据，**不参与任何比对**（比对键只有上面那个 sha）
            "backend_commit": (backend_doc.get("_metadata") or {}).get("backend_commit"),
            "refreshed_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
            "kept": len(kept),
        },
    }


def assert_safe_to_write(doc: dict) -> None:
    """写盘前的硬闸：失败就 raise。

    🔴 **只 print 警告的检查不是闸门是日志** —— 它会在动作之后才出声，
    而这里动作本身（写进公开仓）是不可撤销的。
    """
    extra = set(doc) - ALLOWED_TOP_LEVEL
    if extra:
        raise SystemExit(f"[CI-822] 拒绝写盘：出现了白名单外的顶层键 {sorted(extra)}")

    # 🔴 只扫**数据段**，不扫 `_ci822` 那句说明 —— 说明里必须能点名「内部面刻意不在这里」，
    #    否则下一个人不知道少了什么。检查面开得比污染面宽时，它会对**自己的散文**报警，
    #    而一条「可以忽略的报警」比没有报警更糟。
    blob = json.dumps({"endpoints": doc["endpoints"], "source": doc["source"]},
                      ensure_ascii=False)
    for forbidden in _INTERNAL_MARKERS:
        if forbidden in blob:
            raise SystemExit(f"[CI-822] 拒绝写盘：数据段里出现了 {forbidden!r}，"
                             "那是后端内部面，不能进公开仓")

    for ep in doc["endpoints"]:
        if not ep["path"].startswith(API_PREFIX):
            raise SystemExit(f"[CI-822] 拒绝写盘：{ep['path']} 不在 {API_PREFIX} 下")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default=str(DEFAULT_BACKEND),
                    help="后端 tool_surface.canonical.json 的路径")
    ap.add_argument("--check", action="store_true",
                    help="只比对不写：vendored 副本与后端当前状态是否一致")
    args = ap.parse_args()

    backend_path = Path(args.backend)
    if not backend_path.exists():
        print(f"[CI-822] 找不到后端导出：{backend_path}\n"
              f"        它在 PRIVATE 仓 msds-chain 里（backend/tool_surface.canonical.json）。\n"
              f"        用 --backend <path> 指定，或把那个仓 checkout 到隔壁。", file=sys.stderr)
        return 2

    backend_doc = json.loads(backend_path.read_text(encoding="utf-8"))
    calls = server_calls(SERVER_PY.read_text(encoding="utf-8"))
    doc = build(backend_doc, calls)
    assert_safe_to_write(doc)

    rendered = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    if args.check:
        if not VENDORED.exists():
            print("[CI-822] vendored 副本不存在 —— 跑一次不带 --check 的刷新")
            return 1
        cur = json.loads(VENDORED.read_text(encoding="utf-8"))
        # 🔴 只比内容，不比 refreshed_at（旁证，天天都会变）
        a = {**cur, "source": {k: v for k, v in cur["source"].items() if k != "refreshed_at"}}
        b = {**doc, "source": {k: v for k, v in doc["source"].items() if k != "refreshed_at"}}
        if canonical_bytes(a) != canonical_bytes(b):
            print("[CI-822] vendored 副本与后端当前状态不一致 —— 重跑刷新脚本")
            return 1
        print("[CI-822] vendored 副本是最新的")
        return 0

    VENDORED.write_text(rendered, encoding="utf-8")
    print(f"[CI-822] 已写 {VENDORED.name}："
          f"后端 {backend_doc['api_v2_endpoint_count']} 个端点 → 保留 {doc['source']['kept']} 个"
          f"（server.py 真的调用的那些）；内部工具注册表面已丢弃。\n"
          f"          比对键 api_v2_sha256={doc['source']['api_v2_sha256'][:12]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
