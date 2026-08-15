"""CI-521：每个工具参数都必须在 **schema** 里带 description，枚举型必须自带 enum。

为什么判据打在 `inputSchema` 而不是 docstring 上：docstring 进的是 `Tool.description`，
`inputSchema.properties.<arg>.description` 是**另一个字段**，客户端选参数值时读的是后者。
CI-521 之前 23 个工具的 37 个参数写了一整套 `Args:` 散文，schema 里却是 37 个 `null`——
「写了」和「送到了」是两件事（同族：memory「修了，但没到达真正的消费者」）。

守卫本身按「新增即生效」写：遍历 live registry，不维护参数清单。
手工清单会腐化——新加一个参数没人记得补，覆盖率静默下降且不报错。
"""
import asyncio

import server

# 服务端硬校验取值范围的参数：schema 必须自己声明出来，否则调用方只能靠猜。
# key = "<tool>.<param>"，value = 期望的 enum 取值集合。
# 🔴 取值来自服务端的判断本身（backend `direct_api.py` 的
# `if scenario not in ("spill", "fire", "exposure")`），不是从这里的 schema 抄的——
# 抄 schema 会让守卫和被守卫的东西同源，永远相等，等于空跑。
SERVER_VALIDATED_ENUMS = {
    "get_emergency_response.scenario": {"spill", "fire", "exposure"},
}


def _params():
    """yield (tool_name, param_name, param_schema) —— 取自 live registry。"""
    for t in asyncio.run(server.mcp.list_tools()):
        for name, schema in ((t.input_schema or {}).get("properties") or {}).items():
            yield t.name, name, schema


def test_every_param_has_a_description_in_the_schema():
    naked = [
        f"{tool}.{param}"
        for tool, param, schema in _params()
        if not (schema.get("description") or "").strip()
    ]
    assert not naked, (
        f"这些参数在 inputSchema 里没有 description：{sorted(naked)}——"
        f"写进 Annotated[..., Field(description=...)]，光写 docstring 到不了 schema"
    )


def test_server_validated_params_declare_their_enum():
    by_key = {f"{tool}.{param}": schema for tool, param, schema in _params()}
    for key, expected in SERVER_VALIDATED_ENUMS.items():
        assert key in by_key, f"{key} 不在 registry 里了——服务端校验还在吗？改这个守卫前先确认"
        declared = by_key[key].get("enum")
        assert declared is not None, (
            f"{key} 的取值被服务端硬校验，schema 却没有 enum ⇒ 调用方无从得知合法值。"
            f"把类型写成 Literal[...] 即可"
        )
        assert set(declared) == expected, (
            f"{key} 的 schema enum {sorted(declared)} 与服务端实际接受的 "
            f"{sorted(expected)} 不一致——两边必须同时改"
        )


def test_bounded_numeric_params_declare_their_bounds():
    """`section` 的合法范围（GHS-SDS 1-16）由后端硬校验，schema 必须自带上下界。"""
    by_key = {f"{tool}.{param}": schema for tool, param, schema in _params()}
    section = by_key["get_sds_section.section"]
    assert (section.get("minimum"), section.get("maximum")) == (1, 16), (
        f"get_sds_section.section 的 schema 边界是 "
        f"{section.get('minimum')}..{section.get('maximum')}，后端接受的是 1..16"
    )
