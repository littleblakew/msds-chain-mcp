"""CI-596（MCP 侧）—— 拼接的组合名不许进 intent 日志的化学品列。

## 为什么这半必须做，而不是「后端修了就行」

`_chemicals_from_response` 收的名字经 `_log_intent` → `POST /mcp/call-log` 写进
`McpCallLog.chemicals`，而 `direct_api` 按 `_REPORTABLE_TOOLS` 选这些行拼「最近分析过
的化学品」，最终进 **`get_audit_report` 那份签名合规 PDF**。⇒ 拼接串留在这里的后果不是
一条错链接，而是**一份签名文件把「bleach+ammonia」列成用户分析过的化学品**。
（同一批行还喂 `ChemicalQueryPair`，也就是排爬取优先级的需求语料。）

原有的过滤只丢**带逗号**的名字（`",".join` 存储的反解析问题），`+` 一路畅通。

## 判据

后端（msds-chain `456aa734`）给结构化 `chemicals` + `kind="pair"`。这里的规矩是
**看见 `kind="pair"` 就绝不把 `chemical` 当身份**——包括成员缺失的时候（那时一个都不收）。
按「非空才走结构化」写会让未知升级成乐观分支，那正是 review 在后端侧抓到的 finding。
"""
from __future__ import annotations

import server

PAIR = ["bleach", "ammonia"]
JOINED = "bleach+ammonia"


def _resp(warning: dict) -> dict:
    return {"tool_results": [{"result": {"warnings": [warning]}}]}


def test_joined_pair_label_never_becomes_a_chemical():
    names = server._chemicals_from_response(_resp({
        "level": "high", "kind": "pair", "chemical": JOINED, "chemicals": PAIR,
        "description": "chloramine gas", "mitigation": "-", "reference": "-",
    })) or []
    assert JOINED not in names, (
        f"{JOINED!r} 进了调用日志的化学品列——下游是签名审计报告的化学品清单"
    )
    assert set(PAIR) <= set(names), "两个成员本身是真化学品，该照常留痕"


def test_pair_without_members_records_nothing_rather_than_the_joined_label():
    """未知不许升级成乐观分支：成员取不到就一个都不记（少记 < 记错）。"""
    for broken in ({}, {"chemicals": []}, {"chemicals": None}, {"pair_chemicals": PAIR}):
        names = server._chemicals_from_response(_resp({
            "level": "high", "kind": "pair", "chemical": JOINED,
            "description": "chloramine gas", "mitigation": "-", "reference": "-",
            **broken,
        })) or []
        assert JOINED not in names, f"成员={broken} 时拼接串又被当成身份"


def test_ordinary_warnings_still_record_their_chemical():
    """反向误伤：普通 warning 的 `chemical` 仍是身份，别一并掐掉。"""
    names = server._chemicals_from_response(_resp({
        "level": "high", "chemical": "acetone",
        "description": "flammable", "mitigation": "-", "reference": "-",
    })) or []
    assert "acetone" in names
