"""PreInvocation hook for Helios's Antigravity brain: proof that Helios's hooks.json loaded.

agy silently drops the WHOLE hooks.json if any entry is malformed — taking the PreToolUse gate with
it. This hook lives in the same file and writes the per-session marker named by HELIOS_AGY_MARKER
before every model call; Helios kills a turn whose marker is missing at the first model step.
"""

import os
import pathlib

target = os.environ.get("HELIOS_AGY_MARKER")
if target:
    pathlib.Path(target).write_text("ok", encoding="ascii")
print("{}")
