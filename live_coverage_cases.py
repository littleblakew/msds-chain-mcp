"""CI-245：23 个 MCP 工具的 live 调用用例集（数据，不含执行逻辑）。

放在 repo 根、与 `release_metadata.py` 同一模式：**数据与执行分离**，让
`tests/test_ci245_live_coverage_cases.py` 能在**不联网、不花 credits** 的前提下
守住「用例集 == live registry」。执行逻辑在 `scripts/live_tool_coverage.py`。

## 两层断言（刻意分开，别合并）

- **可用性层**：每个工具都能调通、非 `isError`、返回非空。**这层才决定退出码。**
- **质量层**：内容是否合理（如腐蚀性酸不该只给基础 PPE）。**只报告、不决定退出码**
  ——内容质量断言天生脆（模型措辞会变），混进 gate 会让人开始忽略红灯。

## 输入从哪来

全部沿用 2026-07-31 手工 sweep 已验证可用的那批（acetone / 67-56-1 / benzene /
hydrochloric acid / dichloromethane / 硫酸+NaOH+甲醇 / 一段 protocol 文本 /
假 session_id 走错误路径），不是新编的。
"""

# 每条：args = 调用参数；writes = 是否写数据（判据是 registry 的 readOnlyHint，
# 由 tests 反查校验，不靠人记）；note = 这条用例想覆盖什么，别写成"调一下看看"。
CASES: dict[str, dict] = {
    # ── 查询 / 解析路径 ──────────────────────────────────────────
    "search_chemical_database": {
        "args": {"query": "acetone"},
        "expect_mentions": ['acetone'],
        "note": "最基础的 DB 查询路径；挂了说明解析层或库连接断了",
    },
    "get_sds_section": {
        "args": {"chemical": "acetone", "section": 4},
        "expect_mentions": ['acetone'],
        "note": "无 LLM、确定性；打 shared.canonical_sections。acetone §4 是稳定 canary",
    },
    "get_sds_document": {
        "args": {"chemical": "acetone"},
        "expect_mentions": ['acetone'],
        "note": "签名 URL 通路——可追溯性的落点，断了等于拿不出原始 PDF",
    },
    "search_msds_online": {
        "args": {"chemical_name": "benzene"},
        "expect_mentions": ['benzene'],
        "note": "PubChem 兜底（库里没有时不至于死路）；走外部网络，失败可能是对方的问题",
    },
    "check_regulatory_lists": {
        "args": {"chemical": "benzene"},
        "expect_mentions": ['benzene'],
        "note": "23 份监管清单的交叉比对",
    },
    "compare_sds_versions": {
        "args": {"chemical": "acetone"},
        "expect_mentions": ['acetone'],
        "note": "版本 diff",
    },
    # ── quick-chat 系（走 LLM，慢且真扣 credits）────────────────
    "ask_chemical_safety": {
        "args": {"question": "What PPE do I need to handle acetone?"},
        "expect_mentions": ['acetone'],
        "note": "最常用的入口工具（server instructions 明确要求优先用它）",
    },
    "check_chemical_compatibility": {
        "args": {"chemicals": ["sulfuric acid", "sodium hydroxide"]},
        "expect_mentions": ['sulfuric acid', 'sodium hydroxide'],
        "note": "强酸强碱——最经典的不相容对",
    },
    "get_chemical_risk_warnings": {
        "args": {"chemicals": ["benzene"]},
        "expect_mentions": ['benzene'],
        "note": "致癌物应给出明确危害",
    },
    "check_regulatory_compliance": {
        "args": {"chemicals": ["benzene"], "regions": ["EU", "US"]},
        "expect_mentions": ['benzene'],
        "note": "多法域合规",
    },
    "get_ppe_recommendation": {
        "args": {"chemicals": ["sulfuric acid"]},
        "expect_mentions": ['sulfuric acid'],
        "note": "腐蚀性酸；质量层会检查它没退化成通用手套建议",
    },
    "get_storage_guidance": {
        "args": {"chemicals": ["acetone"]},
        "expect_mentions": ['acetone'],
        "note": "易燃物储存",
    },
    "get_emergency_response": {
        "args": {"chemical": "hydrochloric acid", "scenario": "exposure"},
        "expect_mentions": ['hydrochloric acid'],
        "note": "急救场景。🔴 scenario 是枚举（spill/fire/exposure）——初版写了 'skin contact'，"
                "工具返回一条**文本错误**而不是 isError，可用性层用「非空」判据直接放行了",
    },
    "get_exposure_limits": {
        "args": {"chemicals": ["benzene"], "region": "US"},
        "expect_mentions": ['benzene'],
        "note": "职业接触限值",
    },
    "get_transport_classification": {
        "args": {"chemicals": ["acetone"]},
        "expect_mentions": ['acetone'],
        "note": "运输分类（UN 号 / 类别）",
    },
    "get_waste_disposal": {
        "args": {"chemicals": ["dichloromethane"]},
        "expect_mentions": ['dichloromethane'],
        "note": "卤代溶剂废弃",
    },
    "get_chemical_alternatives": {
        "args": {"chemical": "dichloromethane", "use_case": "extraction"},
        "expect_mentions": ['dichloromethane'],
        "note": "替代物推荐",
    },
    "check_mixing_order": {
        "args": {"chemical_a": "sulfuric acid", "chemical_b": "water"},
        "expect_mentions": ['sulfuric acid', 'water'],
        "note": "酸入水——加料顺序错了会喷溅，是有明确正确答案的一题",
    },
    "validate_protocol_chemicals": {
        "args": {"protocol_text": "Dissolve 5 g sodium hydroxide in 100 mL water, then slowly add 10 mL concentrated sulfuric acid while stirring."},
        "expect_mentions": ['sodium hydroxide', 'sulfuric acid'],
        "note": "从自由文本里认出化学品并判风险",
    },
    "batch_safety_check": {
        "args": {"chemicals": ["acetone", "methanol", "sodium hydroxide"]},
        "expect_mentions": ['acetone', 'methanol', 'sodium hydroxide'],
        "note": "多组分并行；票里记过这类工具耗时最长（超时上限就是为它抬的）",
    },
    # ── 需要 session 的 ────────────────────────────────────────
    "get_audit_report": {
        "args": {"session_id": "00000000-0000-0000-0000-000000000000"},
        "writes": True,   # CI-174：零参调用会建 session + 跑分析；本用例仍走假 id 的失败路径
        "expect_error_ok": True,
        "note": "🔴 故意用假 session_id 走**错误路径**：验的是「报错报得明白」而不是崩掉。"
                "⇒ 这条的可用性判据是「有响应且不是 5xx/超时」，isError 本身是预期内的。"
                "⚠️ **已知盲区**：因此报告生成的**正常路径从未被覆盖**——「23/23 通过」不等于"
                "「报告功能是好的」。要补的话得把 create_audit_session 返回的真 session_id "
                "串进来（有状态，会每跑一次多建一个 session），值不值得另议。",
    },
    # ── 写工具（默认不跑；跑了就会在 Prod 留数据）──────────────
    "create_audit_session": {
        "args": {"experiment_name": "CI-245 live coverage", "chemicals": ["acetone"]},
        "writes": True,
        "expect_mentions": ['acetone'],
        "note": "会建真 session。按 decision-no-auto-cleanup-user-data，写进去就留着 ⇒ "
                "默认只在 Dev 跑，或用测试账号并接受留存",
    },
    "upload_msds_pdf": {
        "args": {"pdf_source": "https://example.invalid/nonexistent.pdf"},
        "writes": True,
        "expect_error_ok": True,
        "note": "🔴 用**取不到的 URL** 走失败路径：既覆盖了这个工具，又不往库里灌垃圾 PDF。"
                "（真上传一份会污染 canonical 语料——CI-311 就是这么出的事）",
    },
}

# 只有这些工具**故意**不做 live 覆盖时才写进来，并且必须写理由。
# 空 dict 意味着「23 个全覆盖」，测试会据此判定。
INTENTIONALLY_UNCOVERED: dict[str, str] = {}

# 质量层：只报告、不决定退出码。每条是 (人话描述, 判定函数)。
# 🔴 判定写成「必须出现某类内容」，不要写成「不能出现某个词」——后者被换个措辞就失效，
#   而且容易变成恒真断言（memory: 判据用自由文本匹配 → 被含同关键词的反例命中）。
QUALITY_CHECKS: dict[str, list[tuple[str, object]]] = {
    "get_ppe_recommendation": [
        (
            "腐蚀性酸应提到面部/眼部防护，而不是只给通用手套",
            lambda text: any(k in text.lower() for k in ("face shield", "goggles", "面罩", "护目")),
        ),
    ],
    "check_mixing_order": [
        (
            "酸入水这题应明确指出把酸加进水里（反过来会喷溅）",
            lambda text: "acid" in text.lower() and "water" in text.lower(),
        ),
    ],
    "check_chemical_compatibility": [
        (
            "强酸强碱应判为不相容/放热，而不是无已知风险",
            lambda text: any(k in text.lower() for k in
                             ("incompatible", "exotherm", "violent", "heat", "不相容", "放热")),
        ),
    ],
}
