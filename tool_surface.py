"""工具面（tool surface）的提取与漂移分类 —— CI-848。

**为什么存在**：把工具面交给外部客户端之后，我们就失去了控制权。
ChatGPT 的应用目录条目在提交时 `Scan Tools` 拍一份快照，官方原话
「Published plugins do not update those skills live」⇒ 之后我们改什么，
目录来的用户都看不见，**而平台不会通知我们条目已经过期**。
claude.ai 同族但轻一些：连接那一刻拍快照，用户重新添加连接器才刷新。

⇒ 需要一条**我们自己**发现漂移的路。本模块只做两件事，都不联网：

1. `extract_surface(tools)` —— 从 registry 提取「工具名 → 必填/可选参数」。
2. `diff_surface(baseline, current)` —— 分类漂移。

🔴 **分类是本模块的重点，不是「变了没有」那一位信号。** 两类漂移对持有旧快照的
客户端后果完全不同：

- **additive（安全）**：新增工具 · 新增可选参数 · 改描述。
  旧客户端看不见新东西，但它照旧发的调用**仍然合法**。
- **breaking（危险）**：删工具 · 改工具名 · 删参数 · 可选变必填 · （本模块看不见的）同名语义改变。
  旧客户端发出的调用要么报错，要么——实测更常见——**被静默接受而语义丢失**：
  2026-09-03 实测 `compare_sds_versions` 少了 `version_old`/`version_new` 之后，
  旧客户端传这两个参数**不报错**，pydantic 静默丢弃 ⇒ 用户拿到一份看起来正常、
  但根本没按他指定版本比较的结果。**不报错的那种更贵。**

所以判据不是「相等吗」，是「**变的是哪一类**」——危险那类必须在重交上架条目**之前**被拦住。
"""

from __future__ import annotations


def extract_surface(tools) -> dict[str, dict[str, list[str]]]:
    """registry 的 Tool 列表 → `{工具名: {"required": [...], "optional": [...]}}`。

    🔴 **直接取 `input_schema`，不用 `getattr(..., default)`**：属性改名过一次
    （CI-242 升 mcp 2.x 时 `inputSchema` → `input_schema`），带默认值的 getattr
    会让每个工具都拿到空 schema，于是**整个守卫静默变成空跑而测试照样绿**。
    直接取属性，改名就 AttributeError，人会看见。
    """
    surface: dict[str, dict[str, list[str]]] = {}
    for tool in tools:
        schema = tool.input_schema
        props = sorted((schema.get("properties") or {}).keys())
        required = sorted(schema.get("required") or [])
        surface[tool.name] = {
            "required": required,
            "optional": [p for p in props if p not in required],
        }
    return surface


def diff_surface(baseline: dict, current: dict) -> dict[str, list[str]]:
    """把 baseline→current 的差异分成 breaking / additive 两栏（各是人话字符串）。

    两栏都为空 ⇒ 工具面没动过。任一非空 ⇒ 上架条目（以及所有已连接的客户端）
    与线上不再一致，需要一次**显式决定**：更新基线，并判断要不要重交条目。
    """
    breaking: list[str] = []
    additive: list[str] = []

    for name in sorted(set(baseline) - set(current)):
        breaking.append(f"工具消失或改名：{name}（旧客户端调它会直接失败）")
    for name in sorted(set(current) - set(baseline)):
        additive.append(f"新增工具：{name}（旧客户端看不见它，但不会出错）")

    for name in sorted(set(baseline) & set(current)):
        was, now = baseline[name], current[name]
        was_all = set(was["required"]) | set(was["optional"])
        now_all = set(now["required"]) | set(now["optional"])

        for p in sorted(was_all - now_all):
            breaking.append(
                f"{name}：参数 `{p}` 消失（旧客户端仍会传它，"
                f"实测**不报错而是被静默丢弃** ⇒ 语义损失且无人发现）"
            )
        for p in sorted(now_all - was_all):
            if p in now["required"]:
                breaking.append(f"{name}：新增了**必填**参数 `{p}`（旧客户端不会传 ⇒ 调用失败）")
            else:
                additive.append(f"{name}：新增可选参数 `{p}`（旧客户端不会传，行为不变）")
        for p in sorted(set(now["required"]) & set(was["optional"])):
            breaking.append(f"{name}：参数 `{p}` 由可选变必填（旧客户端可能不传 ⇒ 调用失败）")

    return {"breaking": breaking, "additive": additive}
