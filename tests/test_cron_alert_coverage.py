"""`cron-failure-alert.yml` 的监听清单必须和本仓实际的 scheduled workflow 对得上。

从 msds-chain 移植（那边是 `backend/tests/scripts/test_ci646_integrity_cron.py` 的
两条），因为**这个仓正是那两条守卫的受害者**：`live-probes-weekly` 在 msds-chain 那边
的登记里漏过一次，而本仓在 2026-08-16 建这份告警之前，`Live Probes Weekly`
（每周一真扣 credits 的 live 覆盖）失败时**没有任何人会收到通知**。

🔴 为什么必须是机械守卫：`cron-failure-alert.yml` 顶部原本把核法写成一句「新增
scheduled workflow 时记得把 `name:` 加进清单，核法＝跑这条 grep」——**靠人记得回来加
一行的清单一定会腐化**（msds-chain 那次一口气漏了 4 个）。清单要能自己发现成员。

🔴 两个方向都要查，因为**它们的失败都是静默的**：
  · 新 cron 没登记 ⇒ 它挂了没人知道；
  · 清单里留着已删的 workflow ⇒ `workflow_run` 监听一个不存在的名字**不报错，
    只是永远不触发**，而「死监听」和「它一直没失败」在文件里完全同形。

变异（改这两条之前先照造一遍，没有变异的守卫默认当它不存在）：
  a. 给 `deploy.yml` 加一段 `schedule:`（不动清单）→ 第一条必须红并点名 deploy.yml
  b. 往清单里加一个 `"Nope Weekly"`            → 第二条必须红并点名 Nope Weekly
  c. 两样都不动                                 → 两条都必须绿（基线是绿的，
     否则「它红了」不构成任何证据）
"""
from pathlib import Path

# 🔴 硬 import，**不用 `pytest.importorskip`**：缺 PyYAML 时要的是一条红，不是
# 一条 skip —— 「被跳过」和「绿」在报告里同形，而这两条守卫一旦静默跳过，
# 它防的两种失败就全部恢复成静默。PyYAML 因此显式声明在 requirements-dev.txt，
# 不靠传递依赖（同仓 httpx2 的先例就写在那个文件顶部）。
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_ALERT = _WORKFLOWS / "cron-failure-alert.yml"


def _on(doc: dict) -> dict:
    """取 workflow 的 `on:` 块。

    🔴 YAML 1.1 把裸 `on` 解析成布尔 **True**，不是字符串 "on" —— 直接
    `doc["on"]` 在多数解析器下拿不到东西，而拿不到的结果是「这个 workflow 没有
    schedule」，也就是**静默跳过**，守卫恒绿。
    """
    v = doc.get("on", doc.get(True)) or {}
    return v if isinstance(v, dict) else {k: None for k in v}


def _watched() -> set[str]:
    return set(_on(yaml.safe_load(_ALERT.read_text(encoding="utf-8")))["workflow_run"]["workflows"])


def _workflows() -> list[tuple[Path, dict]]:
    out = []
    for f in sorted(list(_WORKFLOWS.glob("*.yml")) + list(_WORKFLOWS.glob("*.yaml"))):
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        if isinstance(doc, dict):
            out.append((f, doc))
    return out


def test_the_alert_workflow_itself_is_parseable_and_watches_something():
    """阳性对照：上面两条都建立在「清单读得出来」之上。

    读不出来时 `_watched()` 会抛异常而不是返回空集，但**空集**是个更阴险的形态：
    它会让下面那条「死监听」恒绿（没有成员可判），而「新 cron 没登记」那条则会把
    **每一个** scheduled workflow 都报出来 —— 后者吵到不可能被忽略，前者不会。
    """
    watched = _watched()
    assert watched, f"{_ALERT.name} 的 workflow_run.workflows 是空的——两条守卫都会失去意义"


def test_every_scheduled_workflow_is_watched():
    """新增的 cron 忘了登记 ⇒ 它失败时没有任何人会收到通知。

    🔴 判据按 YAML 解析而不是全文 grep `cron:`：被**注释掉**的 schedule 在全文匹配
    下和真的在跑完全一样（msds-chain 的 `sessions-purge-weekly` 就是那种）。
    """
    watched = _watched()
    unwatched = []
    for f, doc in _workflows():
        if f == _ALERT:
            # 故意不监听自己：告警发送失败会自触发成告警循环（理由在该文件顶部）
            continue
        if not _on(doc).get("schedule"):
            continue
        if doc.get("name") not in watched:
            unwatched.append(f"{f.name} (name: {doc.get('name')!r})")
    assert not unwatched, (
        "这些 scheduled workflow 失败时没有任何人会收到通知 —— 把它们的 `name:` "
        f"加进 {_ALERT.name} 的 workflow_run.workflows：{unwatched}"
    )


def test_no_dead_listeners():
    """反方向：清单里不能有本仓已经不存在的 workflow 名。

    监听一个不存在的名字不会报错，只是永远不触发 ⇒ 这份文件会继续宣称在盯它。
    """
    names = {doc.get("name") for _, doc in _workflows() if doc.get("name")}
    dead = sorted(w for w in _watched() if w not in names)
    assert not dead, (
        f"{_ALERT.name} 在监听本仓不存在的 workflow —— 它们永远不会触发，"
        f"而这看起来和「它们一直没失败」一模一样：{dead}"
    )
