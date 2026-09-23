"""CI-822 —— 本仓对后端 `/api/v2` 契约的守卫（vendored 分发的消费侧）。

后端（PRIVATE 仓 `msds-chain`）导出一份 canonical surface；本仓（**PUBLIC**）
用 `scripts/refresh_backend_surface.py` 把它裁剪成 `backend_surface.vendored.json`。
测试分两层，**分层的理由是它们的失效方向不同**：

| 层 | 跑在哪 | 抓什么 | 拿不到后端文件时 |
|---|---|---|---|
| A 契约 | 任何地方（纯离线） | `server.py` 的调用与 vendored 契约对不上 | 照跑 |
| B 新鲜度 | 后端文件在手时 | 后端动了而我们没刷新 | **skip（在输出里显形）** |

🔴 **为什么不是「一个守卫全包」**：A 层只看得见**我们已经知道的那些端点**——那是
「守卫只检查它自己登记过的成员」这个空跑形状。发现「后端新增了成员」结构上需要
后端那份文件在手，而公开仓里没有它、**也不该有**。

🔴 **比对键是后端未裁剪的全量 `api_v2_sha256`**，不是裁完之后的 sha。原因在刷新脚本里：
裁剪会把「本仓不调用的端点」丢掉，所以后端新增这类端点时，裁完再哈希是**不变**的
——「后端动过」会被自己的裁剪吃掉。

## 变异（🔴 没记变异的守卫默认当它不存在；八条都实跑过）

| 测试 | 什么改动会让它红 |
|---|---|
| `test_every_called_endpoint_is_in_the_contract` | 往 `server.py` 加一个调用契约里没有的端点 |
| `test_no_phantom_params` | 给某个 `_direct_*` 的 `json={}` 加一个后端没声明的键 |
| `test_required_params_are_supplied` | 后端给某个已存在端点**加一个必填参数**，刷新后 `server.py` 没跟 |
| `test_vendored_copy_carries_no_internal_surface` | 刷新脚本不裁剪，整份后端文件原样写进 vendored |
| `test_vendored_copy_holds_nothing_we_do_not_call` | 同上（裁剪边界那一半） |
| `test_backend_has_not_moved_since_we_vendored` | ①后端**新增**一个端点 ②后端**改动已有**端点的契约 ③把 vendored 的戳改旧 ④把戳整个删掉 |

🔴 最后一行四条缺一不可，因为它们是四种不同的坏法：
- ①**清单多了一个** —— 会被本仓的裁剪丢掉 ⇒ **只有契约层的话，这条变异什么都测不出来**。
  这正是比对键必须选「后端未裁剪的全量 sha」的原因。
- ②**已有成员的契约变了** —— 清单一个字不变，**任何「只保留已知成员」的裁剪都留得住它**，
  所以它同时点亮契约层（刷新之后 `test_required_params_are_supplied` 会指名那个新必填参数）。
  ⇒ ①②方向相反，**只造其中一条会漏掉整整一侧**。
- ③漂了 / ④**从没刷新过** —— 在只比内容的守卫里**同形**，那是 vendored 分发的固有失效方向。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from refresh_backend_surface import server_calls  # noqa: E402

VENDORED_PATH = REPO / "backend_surface.vendored.json"


@pytest.fixture(scope="module")
def vendored() -> dict:
    assert VENDORED_PATH.exists(), (
        "backend_surface.vendored.json 不存在 —— 跑 scripts/refresh_backend_surface.py"
    )
    return json.loads(VENDORED_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def calls() -> dict:
    return server_calls((REPO / "server.py").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def contract(vendored) -> dict:
    return {(e["path"], e["method"]): e for e in vendored["endpoints"]}


# ---------------------------------------------------------------- A 层：契约

def test_extraction_actually_found_something(calls):
    """阳性对照：先证明抽取器没坏。

    🔴 没有这条的话，`server_calls` 返回空 dict 会让下面每一条**全部空跑并通过**
    ——「零命中」与「全都对」在断言层面完全同形。
    """
    assert len(calls) >= 15, f"只从 server.py 抽到 {len(calls)} 个 /api/v2 调用点，抽取器多半坏了"


def test_every_called_endpoint_is_in_the_contract(calls, contract):
    """`server.py` 调用的每个端点都必须在后端契约里。"""
    missing = sorted(f"{m} {p}" for (p, m) in calls if (p, m) not in contract)
    assert not missing, (
        f"server.py 调用了后端契约里没有的端点：{missing}\n"
        "要么后端删/改了它（去看 backend/tool_surface.canonical.json），"
        "要么 vendored 副本过期了（跑 scripts/refresh_backend_surface.py）。"
    )


def test_no_phantom_params(calls, contract):
    """送出去的参数名必须都是后端声明过的 —— 幽灵参数会被后端静默忽略。"""
    bad = []
    for key, sent in calls.items():
        ep = contract.get(key)
        if ep is None or "*" in sent:
            continue
        declared = set(ep.get("body_params") or {}) | set(ep.get("query_params") or {})
        for name in sorted(sent - declared):
            bad.append(f"{key[1]} {key[0]} → {name!r}")
    assert not bad, (
        f"server.py 送了后端没声明的参数（会被静默忽略，失败方向是「看起来正常」）：{bad}"
    )


def test_required_params_are_supplied(calls, contract):
    """后端标了 required 的参数，必须真的送出去。"""
    bad = []
    for key, sent in calls.items():
        ep = contract.get(key)
        if ep is None or "*" in sent:
            continue  # 参数是变量 ⇒ 静态看不全，别假阳
        required = {n for n, spec in (ep.get("body_params") or {}).items()
                    if spec.get("required")}
        for name in sorted(required - sent):
            bad.append(f"{key[1]} {key[0]} → 缺 {name!r}")
    assert not bad, f"必填参数没送：{bad}"


# ------------------------------------------------- A 层：IP（本仓是 PUBLIC）

def test_vendored_copy_carries_no_internal_surface(vendored):
    """vendored 副本不许携带后端的内部面。

    🔴 本仓 PUBLIC，**git 历史删不掉** ⇒ 这条守的不是「别推错」，是「推错了不可撤销」。
    判据用**白名单**（只放行我明确要的顶层键），不用黑名单——后端将来加第三个面时，
    黑名单会安静地放它过去。
    """
    assert set(vendored) == {"_ci822", "endpoints", "source"}, (
        f"vendored 副本出现了白名单外的顶层键：{sorted(set(vendored) - {'_ci822', 'endpoints', 'source'})}"
    )
    blob = json.dumps({"endpoints": vendored["endpoints"], "source": vendored["source"]},
                      ensure_ascii=False)
    for forbidden in ("agent_tools", "msds_admin_action", "msds_quality_stats",
                      "fork_session", "review_msds_quality", "recheck_report",
                      "finalize_and_get_contributions", "check_readiness"):
        assert forbidden not in blob, f"公开仓的 vendored 副本里出现了内部面 {forbidden!r}"


def test_vendored_copy_holds_nothing_we_do_not_call(vendored, calls):
    """裁剪边界：契约里不许有 `server.py` 不调用的端点。

    这条是「净新增对外暴露 ＝ 0」那个结论的机械化身：后端 `api_v2` 里含有
    只被私有网关调用的端点，不裁的话会被带进公开仓，而它们对守卫零价值。
    """
    extra = sorted(f"{e['method']} {e['path']}" for e in vendored["endpoints"]
                   if (e["path"], e["method"]) not in calls)
    assert not extra, (
        f"vendored 副本里有本仓不调用的端点：{extra}\n"
        "它们对守卫零价值，却是净新增的对外暴露。"
    )


# --------------------------------------------------- B 层：新鲜度（需要后端文件）

def _backend_doc():
    env = os.environ.get("CI822_BACKEND_SURFACE")
    candidates = [Path(env)] if env else [
        REPO.parent / "msds-chain" / "backend" / "tool_surface.canonical.json",
    ]
    for p in candidates:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")), p
    return None, candidates[0]


def test_backend_has_not_moved_since_we_vendored(vendored):
    """后端的 api_v2 面动过而我们没刷新 ⇒ 红。

    🔴 **比对键是后端未裁剪的全量 sha**，不是我裁完之后的 sha：裁完再哈希的话，
    后端新增一个我们不调用的端点时我这边 sha 不变 —— 「后端动过」被自己的裁剪吃掉。
    🔴 **两种坏法要分开报**：sha 对不上＝漂了；戳根本不在＝从没刷新过。
    只比内容的守卫看不出后者，而那正是 vendored 分发最常见的失效方向。
    """
    doc, path = _backend_doc()
    if doc is None:
        pytest.skip(f"后端 canonical surface 不在手（{path}）—— B 层不跑。"
                    "它在 PRIVATE 仓 msds-chain 里；用 CI822_BACKEND_SURFACE=<path> 指定。")

    stamp = (vendored.get("source") or {}).get("api_v2_sha256")
    assert stamp, (
        "vendored 副本里没有 source.api_v2_sha256 —— 这不是「一致」，是**从没刷新过**。"
        "跑 scripts/refresh_backend_surface.py。"
    )

    if stamp == doc["api_v2_sha256"]:
        return

    ours = {(e["path"], e["method"]) for e in vendored["endpoints"]}
    theirs = {(e["path"], e["method"]) for e in doc["api_v2"]}
    added = sorted(f"{m} {p}" for (p, m) in theirs - ours)
    removed = sorted(f"{m} {p}" for (p, m) in ours - theirs)
    pytest.fail(
        "后端的 /api/v2 面自我们 vendored 之后变了，重跑 scripts/refresh_backend_surface.py。\n"
        f"  我们记的 sha : {stamp[:16]}…\n"
        f"  后端现在的   : {doc['api_v2_sha256'][:16]}…\n"
        f"  后端有而我们没有：{added or '（无）'}\n"
        f"  我们有而后端没有：{removed or '（无）'}\n"
        "（端点集合相同却 sha 不同 ⇒ 变的是某个端点的参数/方法，逐个 diff 那份文件。）"
    )
