"""Pick the brain implementation per [brain].engine config.

`build_brain` returns either the full Claude-Code brain (engine="claude", the default) or the
native lite brain (engine="lite"). Both expose the identical run_turn/panic/busy/new_conversation/
set_session surface + SSE contract, so the app/server/scheduler/telegram/voice code is unchanged.
The lite_brain import is deferred so a Claude-only install never needs the `openai` dependency.
"""

from __future__ import annotations

from . import conf


def build_brain(emit, perms=None):
    if conf.brain_engine() == "antigravity":
        from .agy_brain import AntigravityBrain
        conf.log("app", "brain engine = antigravity (official agy CLI, Google account)")
        return AntigravityBrain(emit=emit, perms=perms)
    if conf.brain_engine() == "gemini":
        from .gemini_brain import GeminiBrain
        conf.log("app", "brain engine = gemini (Gemini CLI, Google account)")
        return GeminiBrain(emit=emit, perms=perms)
    if conf.brain_engine() == "lite":
        from .lite_brain import LiteBrain
        conf.log("app", f"brain engine = lite ({conf.brain_cfg().get('provider', '?')})")
        return LiteBrain(emit=emit, perms=perms)
    from .brain import Brain
    return Brain(emit=emit, perms=perms)
