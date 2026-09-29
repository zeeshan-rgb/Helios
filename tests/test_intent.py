"""Regression net for the follow-up directed-vs-ambient gate — helios/voice/intent.py.

After Helios answers, the mic stays hot for a few seconds so Tim can follow up WITHOUT the wake
word. is_directed() decides whether something caught in that hot window is a real command or just
ambient chatter / backchannel. These cases pin both the keyword path and the "4+ words is probably
for us" fallthrough, so tuning the patterns later can't silently swallow real commands (or start
acting on "ok thanks").
"""

from __future__ import annotations

import pytest

from helios.voice import intent

DIRECTED = [
    "what time is it",
    "open spotify",
    "remind me to call mom",
    "helios play music",
    "can you check my email",
    "search for hotels in miami",
    "set a timer for ten minutes",
    "tell me a joke about cats",
    "stop playing",
    "my dog ate the whole pizza",   # no keyword, but 5 words -> fallthrough = directed
]

AMBIENT = [
    "",
    "yeah",
    "ok",
    "hello",
    "thanks",
    "ok thanks",          # 2 words, no directed signal
    "thank you",          # multi-word backchannel in _AMBIENT
    "never mind",
    "uh huh",
    "cool cool cool",     # 3 words, under the 4-word fallthrough
]


@pytest.mark.parametrize("text", DIRECTED)
def test_directed(text):
    assert intent.is_directed(text) is True


@pytest.mark.parametrize("text", AMBIENT)
def test_ambient(text):
    assert intent.is_directed(text) is False
