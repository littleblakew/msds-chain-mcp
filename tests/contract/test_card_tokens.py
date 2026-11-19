r"""The card's colour roles are generated from the web app's design tokens, never typed by hand.

| guard | what reverting makes it fail |
|---|---|
| `test_tokens_match_the_sources` | edit `msds_mcp/ui_card_tokens.py` by hand, or change a source token without regenerating |
| `test_generator_reads_overrides_and_light_values` | drop the web `:root` override layer or the `html.light` layer from `collect` |
| `test_generator_refuses_a_missing_role` | let a missing role through (the card would fall back to browser defaults) |
| `test_every_role_the_template_uses_is_defined` | use a `--s-*` / `--c-*` variable in the template that the tokens do not define |

`test_tokens_match_the_sources` needs the source files (`CARD_TOKENS_SHELL_CSS`,
`CARD_TOKENS_WEB_DIR`), which a clone of this repository alone does not have; it skips there.
"""
import os
import re
import sys
from pathlib import Path

import pytest

import ui_compat_card as card

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import refresh_card_tokens as gen  # noqa: E402

_SHELL = ":root{" + "".join(f"--s-{r}: 1 1 1;" for r in gen.ROLES) + "}"
_WEB_CSS = ":root{--s-bad-ink: 9 9 9;}\nhtml.light{--s-label: 0 0 0;}\nhtml.light{--s-surface: 7 7 7;}"
_TW = "\n".join(f"{t}: {{ DEFAULT: '#0a0b0c', ink: 'x' }}," for t in gen.TONES)


@pytest.mark.skipif(not (os.environ.get("CARD_TOKENS_SHELL_CSS") and os.environ.get("CARD_TOKENS_WEB_DIR")),
                    reason="token sources not available in this checkout")
def test_tokens_match_the_sources():
    assert (ROOT / "msds_mcp" / "ui_card_tokens.py").read_text(encoding="utf-8") == gen.generate(), (
        "ui_card_tokens.py is stale: run scripts/refresh_card_tokens.py")


def test_generator_reads_overrides_and_light_values():
    dark, light, fills = gen.collect(_SHELL, _WEB_CSS, _TW)
    assert dark["bad-ink"] == "9 9 9" and dark["label"] == "1 1 1"
    assert light["label"] == "0 0 0" and light["surface"] == "7 7 7" and light["bad-ink"] == "9 9 9"
    assert fills["warn"] == "10 11 12"


def test_generator_refuses_a_missing_role():
    with pytest.raises(SystemExit):
        gen.collect(":root{--s-label: 1 1 1;}", _WEB_CSS, _TW)


def test_every_role_the_template_uses_is_defined():
    html = card.card_html("https://a.example", "https://b.example")
    used = set(re.findall(r"var\((--[sc]-[a-z0-9-]+)\)", html))
    defined = set(re.findall(r"(--[sc]-[a-z0-9-]+):", card.CARD_TOKENS_CSS))
    assert used and used <= defined, sorted(used - defined)
