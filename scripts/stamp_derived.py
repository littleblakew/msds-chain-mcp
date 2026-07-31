#!/usr/bin/env python3
"""Stamp DERIVED release metadata (tool count + primary endpoint + transport)
into every client-facing surface, from the live registry and release_metadata.

Invoked by scripts/release.sh right after it stamps VERSION. Byte-minimal: only
the count digits / endpoint host / transport value change; the surrounding copy
is left exactly as-is. Each stamp asserts it found something to change — a
surface that stops carrying its literal fails loudly rather than silently
drifting (the failure mode CI-237 exists to kill).
"""
import asyncio
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import release_metadata as rm  # noqa: E402
import server  # noqa: E402


def _read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def _write(rel, text):
    with open(os.path.join(ROOT, rel), "w") as f:
        f.write(text)


def _fail(msg):
    sys.exit(f"❌ stamp_derived: {msg}")


def stamp_tool_count(count):
    for rel in rm.TOOL_COUNT_SURFACES:
        text = _read(rel)
        total = 0
        for pat in rm.TOOL_COUNT_PATTERNS:
            def repl(m):
                # Rewrite only the captured number, byte for byte otherwise.
                s, e = m.span(1)
                start = m.start()
                return m.group(0)[: s - start] + str(count) + m.group(0)[e - start:]

            text, n = re.subn(pat, repl, text, flags=re.IGNORECASE)
            total += n
        if total == 0:
            _fail(f"{rel}: no tool-count literal found to stamp — fix the copy "
                  f"or remove it from TOOL_COUNT_SURFACES")
        _write(rel, text)
        print(f"  tool count -> {count}: {rel} ({total} spot(s))")


def stamp_endpoint():
    for rel in rm.ENDPOINT_URL_SURFACES:
        text = _read(rel)
        text, n = rm.ENDPOINT_URL_RE.subn(rm.PRIMARY_ENDPOINT, text)
        if n == 0:
            _fail(f"{rel}: no primary '/mcp' endpoint URL found to stamp")
        _write(rel, text)
        print(f"  endpoint -> {rm.PRIMARY_ENDPOINT}: {rel} ({n} spot(s))")


def stamp_transport():
    for rel, transport in rm.TRANSPORT_BY_SURFACE.items():
        text = _read(rel)
        text, n = rm.TRANSPORT_TYPE_RE.subn(
            lambda m: m.group(1) + transport + m.group(2), text
        )
        if n == 0:
            _fail(f"{rel}: no transport 'type' next to an /mcp url found to stamp")
        _write(rel, text)
        print(f"  transport -> {transport!r}: {rel} ({n} spot(s))")


def main():
    count = len(asyncio.run(server.mcp.list_tools()))
    print(f"▶ Derived tool count from live registry: {count}")
    stamp_tool_count(count)
    stamp_endpoint()
    stamp_transport()
    print(f"✅ Derived metadata stamped (count={count}, endpoint={rm.PRIMARY_ENDPOINT})")


if __name__ == "__main__":
    main()
