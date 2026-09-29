"""Directed-vs-ambient judge for the follow-up window.

After Helios answers, the mic stays hot for a few seconds so the user can follow up WITHOUT saying
"hey Helios" again. But a hot mic also catches him talking to someone else, the TV, etc. — so
anything captured in that window is checked here before it's sent to the brain.

This is deliberately a fast LOCAL heuristic (zero added latency): the wake-word path is already
"directed" by definition, so we only gate follow-ups, where speed matters more than perfect
precision. The cost of a wrong "ambient" verdict is just that the user repeats the wake word.
"""

from __future__ import annotations

import re

# Backchannel / to-someone-else noise that shouldn't become a command on its own.
_AMBIENT = {"yeah", "yes", "no", "ok", "okay", "uh huh", "mhm", "right", "sure", "what",
            "hello", "hey", "hi", "haha", "lol", "nice", "cool", "thanks", "thank you",
            "bye", "stop", "nevermind", "never mind", "nothing"}
# Signals of a real directed request: a question, an imperative verb, or addressing Helios.
_DIRECTED = re.compile(
    r"\b(helios|what|who|when|where|why|how|which|can you|could you|would you|please|"
    r"tell me|show me|find|search|open|close|launch|play|pause|stop playing|set|remind|"
    r"send|write|make|create|turn (on|off|up|down)|increase|decrease|mute|unmute|"
    r"volume|screenshot|check|look up|calculate|translate|summari[sz]e|read|list|give me|"
    r"do|run|start|switch|go to|email|calendar|schedule|add|delete|remove|update|help)\b",
    re.I,
)


def is_directed(text: str) -> bool:
    """True if a follow-up utterance looks like a genuine command for Helios."""
    t = (text or "").strip().lower().strip(" .,!?")
    if not t:
        return False
    words = t.split()
    if len(words) <= 1:
        return False                 # one word is almost always backchannel
    if t in _AMBIENT:
        return False
    if _DIRECTED.search(t):
        return True
    # No clear directed signal, but a reasonably long utterance is probably still for us.
    return len(words) >= 4
