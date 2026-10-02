"""Every wrapper table's `zh-TW` entry is derived from its `zh` entry with OpenCC s2twp.

The traditional-Chinese strings are a copy of the simplified ones. A copy kept by hand
drifts: someone edits `zh`, forgets `zh-TW`, and Traditional users silently get the old
wording. This test re-derives each entry and compares, so the copy checks itself.

The conversion is the same one the frontend uses (`frontend/scripts/gen-zh-tw.py`), plus
the `_OVERRIDES` below for words s2twp gets wrong.

Mutations (run):
- edit one `zh` string in `_REG_LIST_STRINGS` without touching `zh-TW` => this test fails.
- delete the `zh-TW` key from `_UNRESOLVED_BOOLEAN_NOTE` => the coverage test fails.
- drop `_normalize_catalog_lang(...)` from the timeout wrapper => the timeout test fails.
"""
import asyncio

import httpx
import opencc  # declared in requirements-dev: a missing converter must fail, not skip
import pytest

import server

_OVERRIDES = {"副本里": "副本裡"}

TABLES = [
    "_DIRECT_TIMEOUT_MSG", "_DIRECT_TIMEOUT_HINT_BATCH", "_TIMEOUT_ANSWER",
    "_REG_LIST_COVERAGE_NOTE", "_REG_LIST_STRINGS", "_UNRESOLVED_BOOLEAN_NOTE",
    "_SDS_DRAFT_STRINGS",
]

_cc = opencc.OpenCC("s2twp")


def _derive(v):
    if isinstance(v, str):
        out = _cc.convert(v)
        for a, b in _OVERRIDES.items():
            out = out.replace(a, b)
        return out
    if isinstance(v, dict):
        return {k: _derive(x) for k, x in v.items()}
    return v


@pytest.mark.parametrize("name", TABLES)
def test_zh_tw_entry_is_derived_from_zh(name):
    table = getattr(server, name)
    assert table["zh-TW"] == _derive(table["zh"]), (
        f"{name}['zh-TW'] no longer matches s2twp({name}['zh']). Re-derive it; do not hand-edit.")


def test_every_lang_keyed_table_has_zh_tw():
    """Positive control for the list above: any module-level table keyed by both `en` and
    `zh` must also carry `zh-TW`, so a new table cannot slip past the derivation test."""
    missing = [n for n, v in vars(server).items()
               if isinstance(v, dict) and "en" in v and "zh" in v and "zh-TW" not in v]
    assert not missing, f"tables without zh-TW: {missing}"
    found = {n for n, v in vars(server).items()
             if isinstance(v, dict) and "en" in v and "zh" in v}
    assert set(TABLES) <= found


@pytest.mark.parametrize("given", ["zh-TW", "zh-tw", " ZH-TW "])
def test_timeout_message_follows_the_normalized_language(given):
    """Any accepted spelling of zh-TW gets the Traditional timeout text, not English."""
    async def slow(chemicals, lang=None):
        raise httpx.ReadTimeout("t")

    res = asyncio.run(server._graceful_timeout(slow)(["x"], lang=given))
    assert res.content[0].text.startswith(server._DIRECT_TIMEOUT_MSG["zh-TW"])
