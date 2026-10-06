"""CI-586：柜型按另一份 SDS 加严时，那句「是哪一家说的」必须进**文本**面。

后端（msds-chain）在分类层看见分歧后会把 `storage_class` 按更严的那份给，并附
`hazard_classification_conflict_note`（点名 supplier + 修订日期）。但 `get_storage_guidance`
是 `structured_output=False` —— 多数客户端只把 TextContent 喂给模型 ⇒ 不渲染就等于：
模型看到一个**没有出处的加严**，而它引用的 supplier 恰恰是没有这个分类的那一份。
（CI-553/CI-408/CI-360/CI-572 已经是同一形状的第五次。）
"""
import asyncio

import server

_NOTE = ("⚠️ The SDS cited above does not carry this classification, but another SDS we hold "
         "for the same CAS does: H314（Alfa Aesar / 2018-07-27）. The storage class here is set "
         "from the MORE severe classification, not from the cited sheet.")


def _run(payload):
    async def _fake(*_a, **_k):
        return payload
    orig = server._direct_storage
    server._direct_storage = _fake
    try:
        res = asyncio.run(server.get_storage_guidance(["sodium hydroxide"]))
        return res.content[0].text if hasattr(res, "content") else res
    finally:
        server._direct_storage = orig


def _payload(note):
    return {"results": [{
        "chemical_name": "Sodium hydroxide", "cas": "1310-73-2",
        "storage_class": "corrosive_base", "storage_class_label": "Corrosive Bases",
        "cabinet_color": "White (separate from acids)",
        "recommended_cabinet": "Base storage cabinet (separated from acid cabinet)",
        "storage_requirements": ["Never store with acids"],
        "hazard_classification_conflict": {"codes": [{"code": "H314"}]} if note else None,
        "hazard_classification_conflict_note": note,
    }], "unresolved": []}


def test_the_conflict_note_reaches_the_text_the_model_reads():
    txt = _run(_payload(_NOTE))
    assert _NOTE in txt, f"加严的依据没进文本面 —— {txt!r}"
    # 反向守卫的一侧：披露不能挤掉这条工具本来的正文。
    assert "Corrosive Bases" in txt and "Base storage cabinet" in txt


def test_no_conflict_means_silence():
    """🔴 没有分歧时一个字都不许多说——警告通胀会让真警告被忽略。"""
    txt = _run(_payload(None))
    assert "another SDS" not in txt
    assert "⚠️" not in txt
    assert "Corrosive Bases" in txt
