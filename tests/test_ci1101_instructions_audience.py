"""CI-1101: there are TWO `instructions` documents, and this repo holds the one
that hosted users never see.

- **This file's copy** (the `instructions=` ctor arg in `server.py`) is what a
  **self-hosted / stdio** install serves. Real, but a minority of installs.
- **The hosted deployment** (`mcp.lagentbot.com`) is fronted by the distribution
  gateway documented in README, and that gateway **replaces `result.instructions`
  wholesale** on the `initialize` response. Every client that connects to the
  hosted endpoint reads the gateway's onboarding copy instead of this one.

🔴 The reason this needs a guard: editing the copy below to change what users see
is a **silent no-op** for hosted clients. Tests pass, the deploy is green, the
wire is unchanged. That outcome is indistinguishable from "I changed the prompt
and the model ignored it" — which is a wrong and expensive conclusion to reach,
because it argues that prose-level changes don't work at all.

⚠️ The two copies are **meant** to differ (CI-405); do not merge them. The goal
here is only that changing the wrong one makes a noise.

## What the two tests do

1. A **positive control**: the ctor arg actually reaches the object the
   `initialize` handshake is built from. Without it, the pin below could stay
   green forever while the wire served nothing (`instructions=` dropped, renamed
   by an SDK upgrade, …) — the pin alone cannot tell "unchanged" from "gone".
2. A **tripwire**: the served text is pinned. Any edit turns it red once, and the
   failure message is the whole point — it asks which audience you meant.

🔴 Read through `create_initialization_options()`, not `server.mcp.instructions`:
that is the object the handshake is serialized from, so it stays honest if the
ctor arg ever stops reaching the wire (the same reason `test_version.py` reads
`opts.server_version` instead of the constant).

🔴 **Mutations (both run):**
· change one character of the `instructions=` text in `server.py` ⇒ tripwire red
· drop the `instructions=` ctor arg entirely ⇒ **positive control** red as well
  (the pin alone would also go red, but it would blame the wrong thing — it reads
  like "someone edited the copy", not "the copy stopped being served")
"""
import hashlib

import server

# The pin. Update it **only after** answering the question in the failure
# message; a blind refresh here re-arms the exact trap the test exists to catch.
_PINNED_LEN = 2136
_PINNED_SHA256 = "cbff71bff0a965592da8dec4d84414a4b0d4399426d77ff33628e1f8e95de056"


def _wire_instructions() -> str:
    """The instructions as the `initialize` handshake will serialize them."""
    opts = server.mcp._lowlevel_server.create_initialization_options()
    return opts.instructions or ""


def test_instructions_actually_reach_the_initialize_handshake():
    """Positive control for the tripwire below — see this module's docstring."""
    text = _wire_instructions()
    assert text.strip(), (
        "the `initialize` handshake would serve NO instructions — the "
        "`instructions=` ctor arg in server.py is not reaching the low-level "
        "server (dropped, or renamed by an SDK upgrade)"
    )
    # A cheap sanity anchor on content, so "served something" can't be satisfied
    # by an empty-ish placeholder.
    assert "ask_chemical_safety" in text


def test_changing_these_instructions_is_a_deliberate_choice_of_audience():
    """The `instructions` served by THIS repo changed. Which audience did you mean?

      · self-hosted / stdio installs  → this file is the right place; re-pin below.
      · anyone on mcp.lagentbot.com   → WRONG FILE. The distribution gateway replaces
        `result.instructions` wholesale on `initialize`, so this edit reaches nobody
        who connects to the hosted endpoint. Change the gateway's onboarding copy
        instead (private gateway repo), then decide separately whether this one
        should follow.
      · both                          → change both; they are allowed to differ
        (CI-405), so keep each one written for its own audience.

    Re-pin with:
      python -c "import hashlib,server; \
t=server.mcp._lowlevel_server.create_initialization_options().instructions or ''; \
print(len(t), hashlib.sha256(t.encode()).hexdigest())"

    🔴 This text is the assertion message on purpose (a long string constant would
    trip `test_no_pasted_prose_in_fixtures`, which exempts docstrings) — so keep it
    a docstring, and keep it written as instructions to the person reading the red.
    """
    text = _wire_instructions()
    got = (len(text), hashlib.sha256(text.encode()).hexdigest())
    assert got == (_PINNED_LEN, _PINNED_SHA256), (
        test_changing_these_instructions_is_a_deliberate_choice_of_audience.__doc__
    )
