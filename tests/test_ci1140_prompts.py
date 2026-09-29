"""CI-1140：prompts 是给 agent 的剧本，而剧本里的每一句调用都必须是真的。

MCP 三种原语我们此前只用了 tools。`prompts` 是 workflow 的原生载体：server 把预置的
多步流程交给 host，用户在客户端打 `/` 就能选。

🔴 **这个文件守的不是「有几个 prompt」，是「剧本教的东西对不对」。** prompt 正文会被
host 的 agent 当步骤执行 ⇒ 里面写错一个工具名或一个参数名，模型会**照着调**，
然后拿到一个它无法归因的错误（对用户表现为「这个 server 坏了」）。
**而这类错误在本仓任何既有守卫里都不会红**：prompt 正文只是一个字符串常量。

🔴 **判据自己发现成员**：按 `mcp.list_prompts()` 全量扫，新 prompt 自动进来。
写死一张 prompt 名单会在第 4 个 prompt 出生时静默过期。

## 🔴 变异记录（没记变异的守卫默认当它不存在）

- 把某个 prompt 正文里的 `batch_safety_check(` 改成 `batch_safety_checks(`
  ⇒ `test_every_tool_named_in_a_prompt_is_registered` 红并点名那个不存在的工具。
- 把 `check_mixing_order(chemical_a=...)` 改成 `check_mixing_order(chemicalA=...)`
  ⇒ `test_every_kwarg_named_in_a_prompt_exists_on_that_tool` 红并点名那个参数。
  🔴 **这一条是上一条抓不到的**：工具名对、参数名错，模型照样会调失败。
  🔴🔴 **而我的第一版守卫漏掉了它，全绿** —— 正则写成 `[a-z][a-z0-9_]*`，于是
  `chemicalA=` **一个字符都没匹配到**（不是匹配错，是凭空消失），守卫看到的是
  「只有正确的参数」。**是造变异才发现的，不是读代码发现的**：变异落地了、守卫却不红，
  三种成因（守卫漏了 / 变异没落地 / 我对机制的解释是编的）里是第一种。
  ⇒ 两个正则都改成接受大写，并把判据换成「非注册工具**且含下划线或大写**才可疑」。
- 把 `batch_safety_check(` 改成 **camelCase** 的 `batchSafetyCheck(`
  ⇒ `test_every_tool_named_in_a_prompt_is_registered` 红。
  🔴 这条是上一条的镜像面：**第一版同样漏**（`[a-z][a-z0-9_]{3,}` 撞到大写就停，
  再被「必须含下划线」那条过滤掉）⇒ 同一个洞有两个出口，补一个不够。
- 把 `incident_response` 正文里那句「先答后溯源」删掉、改成先要 SDS
  ⇒ `test_incident_response_answers_before_provenance` 红。
- 往任一 prompt 里写 "not in our database"
  ⇒ `test_prompts_never_assert_absence` 红（那是断言一件我们没验证过的事）。
- 🔴 **变异方向是「集合变大」**：真正该造的是**加一个新 prompt**（它应当自动进入全部
  检查）。本文件按 `list_prompts()` 发现成员，所以这条是结构性成立的，不是我声称的。
"""
import asyncio
import inspect
import re

import pytest

import server

# `tool_name(` / `arg=` —— 剧本里教 agent 调用的写法。刻意不要求反引号：漏写反引号的
# 那句同样会被模型照着调。
#
# 🔴 **两个正则都必须接受大写，这不是风格问题**（我的第一版就栽在这，见文件头变异记录）：
# 写成 `[a-z][a-z0-9_]*` 时，`chemicalA=` 这种 camelCase 手滑**一个字符都匹配不到**
# —— 不是匹配错，是**凭空消失** ⇒ 守卫看到的是「只有正确的参数」，绿得毫无异常。
# 而 camelCase↔snake_case 恰恰是本仓最常见的那类手滑（同族：`input_schema`/`inputSchema`
# 在本仓两种写法并存，见 `docs/ops/pitfalls/mcp-server-py-editing.md` 第 5 节）。
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_KWARG = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _looks_like_a_tool_call(token: str, tools: set[str]) -> bool:
    """非注册工具、且**含下划线或大写** ⇒ 可疑。

    🔴 判据这么写是量出来的，不是拍的：当前三个 prompt 的正文里，call 形 token 里的
    非工具项**全部**是纯小写无下划线的英文散文词（`a specific pair (` 这种）⇒ 它们不误报；
    而两种真实手滑 —— `batch_safety_checks`（多个 s，带下划线）与 `batchSafetyCheck`
    （camelCase，带大写）—— 都会被抓住。
    🔴 **默认方向是「不认识就可疑」**：新散文词误报一次，人加进 `_PROSE_OK` 即可；
    反过来漏掉一个错工具名，是模型照着调、用户看到「这个 server 坏了」。
    """
    if token in tools:
        return False
    return "_" in token or any(c.isupper() for c in token)


# 正文里出现在左括号前的普通英文词。🔴 这是**豁免名单**不是工具名单：
# 往这里加东西之前先确认它真的是散文，不是一个打错的工具名。
_PROSE_OK = {"audit", "pair", "procedures", "returns", "spill", "substitutes"}

# modern（2026-07-28）那条腿要求的信封；少了它 `prompts/list` 直接 400 `-32602`。
_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _prompts():
    return asyncio.run(server.mcp.list_prompts())


def _render(name: str, args: dict | None = None) -> str:
    got = asyncio.run(server.mcp.get_prompt(name, args or {}))
    return "\n".join(getattr(m.content, "text", "") for m in got.messages)


def _tool_names() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_the_discovery_actually_finds_prompts():
    """🔴 阳性对照：下面每一条都是「没找到就绿」。

    `list_prompts()` 哪天返回空（注册方式改了 / 文件被拆走），它们会**一起变成绿的空跑**，
    而那与「剧本全都正确」完全同形。
    """
    names = [p.name for p in _prompts()]
    assert len(names) >= 3, f"只发现 {len(names)} 个 prompt：{names} —— 先查发现器"
    assert len(set(names)) == len(names), f"prompt 名重复：{names}"


def test_every_prompt_renders_with_no_arguments():
    """host 可以不带参数就 `prompts/get` —— 那条路不许炸，也不许编出一份化学品清单。"""
    for p in _prompts():
        text = _render(p.name)
        assert text.strip(), f"{p.name} 空参数渲染出空正文"
        # 空参数时必须是「去问」，不是拿空列表往下走
        assert re.search(r"\bAsk\b|\bask the user\b", text), (
            f"{p.name} 在没有输入时没有让 agent 去问 —— 它会拿一个空清单去调工具，"
            f"而那个失败对用户表现为「这个 server 坏了」")


def test_every_tool_named_in_a_prompt_is_registered():
    """🔴 本票的核心判据：剧本里提到的工具名必须真的注册了。

    条件写成「正文里出现的调用都要在 `list_tools()` 里」，而不是比对一张手抄清单：
    工具改名时这条会红，而手抄清单只会静默过期。
    """
    tools = _tool_names()
    bad = {}
    for p in _prompts():
        text = _render(p.name)
        unknown = {m for m in _CALL.findall(text)
                   if m not in _PROSE_OK and _looks_like_a_tool_call(m, tools)}
        if unknown:
            bad[p.name] = sorted(unknown)
    assert not bad, (
        f"这些 prompt 教了不存在的工具：{bad}。agent 会照着调，然后拿到一个它无法归因的"
        f"错误 —— 对用户表现为「这个 server 坏了」。")


def test_every_kwarg_named_in_a_prompt_exists_on_that_tool():
    """🔴 上一条抓不到这一类：**工具名对、参数名错**，模型照样调失败。

    做法：把正文按 `tool(` 切段，只在该调用的括号内取 `xxx=`，再比对真实签名。
    """
    tools = _tool_names()
    bad = {}
    for p in _prompts():
        text = _render(p.name)
        for m in _CALL.finditer(text):
            name = m.group(1)
            if name not in tools:
                continue
            # 取这次调用的括号内容（到第一个右括号为止；我们的剧本不写嵌套调用）
            seg = text[m.end():text.find(")", m.end()) + 1 or None]
            kwargs = set(_KWARG.findall(seg))
            real = set(inspect.signature(getattr(server, name)).parameters)
            unknown = kwargs - real
            if unknown:
                bad.setdefault(p.name, []).append(f"{name}(): {sorted(unknown)}")
    assert not bad, (
        f"这些 prompt 教了该工具没有的参数名：{bad}。"
        f"判据取自 `inspect.signature`，所以工具改签名时这条会跟着红。")


def test_prompts_never_assert_absence():
    """🔴 措辞红线（与工具文案同源，CI-243 / CI-322 / CI-334 三次同形事故）。

    「未收录 / 没有数据 / 建议上传」是在断言一件我们**没验证过**的事：查不到只说明
    这一轮没查到，不说明它不存在。说得出口的只有「这一轮没查」与「怎样才能让它被查」。
    """
    banned = [
        "not in our database", "not in the database", "no record exists",
        "we do not have data", "we don't have data", "no data available",
    ]
    hits = {}
    for p in _prompts():
        low = _render(p.name).lower()
        found = [b for b in banned if b in low]
        if found:
            hits[p.name] = found
    assert not hits, (
        f"这些 prompt 断言了「库里没有」：{hits} —— 我们只知道这一轮没查到。")


def test_incident_response_answers_before_provenance():
    """🔴 这个 prompt 的顺序与别的**相反**，而顺序就是它的全部价值。

    别的流程可以先摆出处再展开；这条路上的人可能正站在泼洒物旁边 ⇒ 先给可执行步骤，
    再给溯源。把它「统一」成和别的一样，不会有任何东西报错。
    """
    text = _render("incident_response")
    i_steps = text.find("get_emergency_response")
    i_doc = text.find("get_sds_document")
    assert i_steps != -1 and i_doc != -1, "两个关键步骤有一个不见了"
    assert i_steps < i_doc, (
        "急救步骤被排到了原件链接后面 —— 这条 prompt 的存在理由就是相反的顺序")
    assert re.search(r"emergency services|poison control", text, re.I), (
        "没有让用户同时联系专业急救 —— 这些步骤是补充，不是替代")


@pytest.mark.parametrize("name,args", [
    ("lab_protocol_audit", {"protocol_text": "Dissolve 5 g NaOH in 100 mL water."}),
    ("lab_protocol_audit", {"chemicals": "acetone, bleach"}),
    ("ehs_compliance_review", {"chemicals": "acetone", "regions": "EU,US,JP"}),
    ("incident_response", {"chemical": "hydrofluoric acid", "scenario": "skin contact"}),
])
def test_arguments_actually_reach_the_rendered_text(name, args):
    """参数得真的进正文 —— 声明了参数却不用它，与「没有这个参数」对 agent 完全同形。"""
    text = _render(name, args)
    for v in args.values():
        assert v.split(",")[0].strip() in text, f"{name} 的入参 {v!r} 没出现在渲染结果里"


def test_default_regions_are_disclosed_as_a_default():
    """🔴 静默的默认值会被读成「查遍了所有法域」（CI-61 给工具定的同一条规矩）。

    这里守的是**剧本有没有把那条规矩传达给 agent**——我们管不到 agent 听不听，
    但「我们根本没说」和「说了它没听」是两件事，只有前者是我们的缺陷。
    """
    text = _render("ehs_compliance_review", {"chemicals": "acetone"})
    assert "EU,US" in text or "EU, US" in text, "默认区域没出现在正文里"
    assert re.search(r"default", text, re.I), (
        "没有告诉 agent「这是默认值、要说出来」—— 用户会把它读成全覆盖")


# 🔴 **传输层那条测试在 `test_dual_transport.py`，不在本文件** —— 它得和别的
# `live_client` 用例住在一起。理由不是归类美观：`live_client` 是 **session 级**单例，
# 而在 mcp **2.0.0** 上，**谁第一个用它**会决定后面的人炸不炸
# （`RuntimeError: Task group is not initialized`）。本文件名排序靠前，放在这里就会
# 把自己变成第一个消费者，进而打翻 `test_ci515_cache_hints` 与 `test_dual_transport`
# 共 6 条。🔴 **那 6 条单独跑、两文件一起跑都是绿的，只有全量一起跑才炸**，
# 而 **2.2.0 上全绿** ⇒ 三个「绿」都不构成安全的证据。
# ⇒ **要加 `live_client` 用例，就加进已经在用它的那个文件。**
