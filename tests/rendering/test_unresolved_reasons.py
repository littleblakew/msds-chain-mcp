"""CI-470 phase 3b — the MCP text surface says WHY a name was unresolved.

This is the surface with the only deeply-active human user, and it is a
**text** surface: `structured_output=False` tools are read by the model as the
rendered string, so a field the renderer never prints does not exist as far as
the consumer is concerned. The backend has carried `unresolved_detail` since
CI-470 phase 3b; this file pins that the renderer actually prints it.

🔴 The failure this prevents is not cosmetic. `**Unresolved:** hydrofluoric acid`
reads as "we hold no data for it" — a conclusion we are NOT entitled to when the
real cause was a lookup step that failed, or a name/CAS contradiction we refused
to answer. Flattening those into one bare list is the same "empty read as none"
shape this whole line of tickets exists to close.
"""
import server


def test_a_bare_backend_response_still_renders_the_old_single_line():
    """Older backend (or an endpoint that doesn't build detail yet) ⇒ unchanged output.

    🔴 Silence must never become an invented reason.
    """
    out = server._unresolved_block({"unresolved": ["aspirin", "xyz"]})
    assert out == ["**Unresolved:** aspirin, xyz"]


def test_each_name_carries_its_own_reason_when_the_backend_sends_one():
    data = {
        "unresolved": ["hydrofluoric acid", "xyz"],
        "unresolved_detail": [
            {"query": "hydrofluoric acid", "code": "tier_degraded",
             "reason_en": "hydrofluoric acid: one of the lookup steps did not "
                          "complete, so this 'not found' is unreliable."},
            {"query": "xyz", "code": "not_in_database",
             "reason_en": "xyz: no record found in the database."},
        ],
    }
    out = "\n".join(server._unresolved_block(data))

    assert "one of the lookup steps did not complete" in out, (
        "后端说了「这一步没跑成、这个『没找到』不可信」，渲染层把它丢了——"
        "用户/模型读到的仍然是一个我们没有资格下的结论"
    )
    assert "no record found in the database" in out
    # 两个名字各自成行，别把两种完全不同的原因拼成一句
    assert out.count("- **") == 2


def test_a_name_without_detail_is_still_listed():
    """detail 只覆盖了一部分名字时，剩下的**不能消失**——少列一个未解析项，
    读起来就是「那个我们查到了」。"""
    data = {
        "unresolved": ["a", "b"],
        "unresolved_detail": [{"query": "a", "reason_en": "a: no record found."}],
    }
    out = "\n".join(server._unresolved_block(data))
    assert "- **a**" in out and "- **b**" in out


def test_a_chinese_only_reason_is_not_spliced_into_the_english_surface():
    """后端两种语言都给（`reason` zh + `reason_en`）。这个 server 渲染英文，
    拿不到 `reason_en` 时**留空**，不拿中文顶——中文句子落进英文答复里读起来是渲染 bug。"""
    data = {
        "unresolved": ["甲醇"],
        "unresolved_detail": [{"query": "甲醇", "reason": "甲醇：库中没有找到记录。"}],
    }
    out = "\n".join(server._unresolved_block(data))
    assert "- **甲醇**" in out
    assert "库中没有找到记录" not in out


def test_nothing_unresolved_renders_nothing():
    assert server._unresolved_block({"unresolved": []}) == []
    assert server._unresolved_block({}) == []


def test_malformed_detail_does_not_break_the_whole_answer():
    """detail 里混进非 dict（后端改形状/半截响应）时，渲染不许抛——一次**本来有答案**的
    调用不该因为一个附加字段而整个失败。"""
    data = {"unresolved": ["a"], "unresolved_detail": ["not-a-dict", None]}
    out = "\n".join(server._unresolved_block(data))
    # 非 dict 全被滤掉 ⇒ 退回旧的单行形状。名字**必须还在**（少列一个未解析项，
    # 读起来就是「那个我们查到了」）；退回旧形状本身是对的，不是缺陷。
    assert "a" in out and out.startswith("**Unresolved:**")
