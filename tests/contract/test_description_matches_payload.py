"""CI-1088：工具描述不许宣称它自己不返回的东西；描述里指的路必须通向真实存在的工具。

**同一根因的第三次**（CI-247 文档宣称没有的 PPE 能力 → CI-1080 listing 面说成身份解析器
→ 本票）。三次都不报错：描述与载荷之间**零机械联系**，写错了既不红也不崩，
只是下游模型据此做了一个错误的选择。

本票这一处：`check_chemical_compatibility` 的描述写着它同时给「储存建议」，
而它的载荷里没有任何储存字段（storage class / cabinet / temperature /
segregation / peroxide-former 全部只在 `get_storage_guidance` 里）。
后果不是答错，是**模型认为调这个就够了，于是不去调那个真正该调的工具**。

🔴 **判据打在 `list_tools()` 发布出来的描述上，不是读源码** —— docstring 一旦被移进
常量或被包一层，读源码的守卫就静默失效（CI-1080 同理）。

🔴 **本文件只覆盖 storage 这一族。** 通用守卫（「描述里点名的能力，载荷里有没有」）
需要一个不会腐化的「能力词 → 字段」映射，设计难点记在 `pm/tickets/CI-1088.md`，
**别把本文件当成那道守卫**。

🔬 **变异（逐条实跑过，记的是实测值不是预测）**：
- M1 把 `storage recommendations` 那句宣称加回描述 → **1 红**：`..._does_not_claim_storage`。
- M2 删掉描述里指向 `get_storage_guidance` 的那句 → **1 红**：`..._points_at_the_storage_tool`。
- M3 把那个指路改成一个不存在的工具名（`get_storage_guidence`，拼错一个字母）
  → **2 红**（我预测 1，实测 2）：`..._every_referenced_tool_exists` 点名那个名字，
  **外加** `..._points_at_the_storage_tool` —— 拼错的同时也就没有正确的指路了。
  📌 记实测不记预测。
  🔴 这一条是**会自己发现成员**的那半：它扫全部工具的描述、对照真实注册表，
  以后谁给工具改名、谁在描述里新指一条路，它都管得到。
- 空跑侧 M4 让 `_descriptions()` 返回 `{}` → **3 红**，其中
  `test_descriptions_are_actually_fetched` 报得出原因（一个工具都没取到）。
"""
import asyncio
import re

import pytest

import server

COMPAT = "check_chemical_compatibility"
STORAGE = "get_storage_guidance"

# 「我也给储存建议」的语义骨架。故意不抄原句：换个措辞重犯同一个错也要红。
# 🔴 **它分不出宣称和免责**：写「It does NOT return … segregation requirements」照样红
# （实测撞到过一次）。这是刻意的 —— 让正则去理解否定是它腐化的开始。
# ⇒ **描述要说清自己不管什么，就换一种不用这些词的说法**（指向 get_storage_guidance
# 并说清那边管什么），本仓现在的写法就是按这条改出来的。
_CLAIMS_STORAGE = re.compile(
    r"(storage|cabinet|segregat\w*)\s+(recommendation|guidance|advice|requirement)s?",
    re.IGNORECASE,
)

# 描述里提到的本仓工具名（工具名都是 snake_case 且以这几个动词开头）。
_TOOL_MENTION = re.compile(r"\b((?:get|check|search|validate|compare|create|upload|ask|batch)_[a-z0-9_]+)\b")


def _descriptions() -> dict[str, str]:
    return {t.name: (t.description or "") for t in asyncio.run(server.mcp.list_tools())}


def test_descriptions_are_actually_fetched():
    """防空跑：取不到描述时，下面两条都会「通过」，且与正常完全同形。"""
    descs = _descriptions()
    assert len(descs) > 15, f"只取到 {len(descs)} 个工具，取描述这条路多半断了"
    assert COMPAT in descs and STORAGE in descs
    assert len(descs[COMPAT]) > 100


def test_compatibility_does_not_claim_storage():
    hit = _CLAIMS_STORAGE.search(_descriptions()[COMPAT])
    assert hit is None, (
        f"{COMPAT} 的描述又在宣称它给储存建议了：{hit.group(0)!r}。"
        f"它的载荷里没有任何储存字段 —— 那些只在 {STORAGE} 里，"
        "这句宣称会让模型不去调那个工具。"
    )


def test_compatibility_points_at_the_storage_tool():
    """光不说错话不够：删掉宣称之后要告诉调用方该去哪，否则它只是不知道了。"""
    assert STORAGE in _descriptions()[COMPAT], (
        f"{COMPAT} 的描述没有把要储存方案的调用方指向 {STORAGE}"
    )


def test_every_referenced_tool_exists():
    """🔴 会自己发现成员的那半：扫全部描述里提到的工具名，对照真实注册表。

    防的是「指路指向一个改过名或已删除的工具」—— 那种死链**不会报错**，
    模型只会去调一个不存在的东西然后放弃。
    """
    descs = _descriptions()
    known = set(descs)
    dangling: dict[str, list[str]] = {}
    for name, desc in descs.items():
        missing = sorted({m for m in _TOOL_MENTION.findall(desc) if m not in known})
        if missing:
            dangling[name] = missing
    assert not dangling, (
        f"描述里指向了不存在的工具（死链，运行时不会报错）：{dangling}\n"
        f"注册表里实际有：{sorted(known)}"
    )
