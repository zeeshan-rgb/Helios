"""Regression net for automatic model routing — helios/router.py choose_model().

choose_model() picks a Claude tier (light=haiku / medium=sonnet / heavy=opus) per turn from cheap
heuristics. These cases pin the tier decision so a pattern tweak can't accidentally start sending
trivial chat to opus (cost) or screen/integration work to the weak tier (it hallucinates
procedures). Triage is off by default, so every case is deterministic and offline (no claude_cli
call). Assumes the committed [router] defaults (auto=true).
"""

from __future__ import annotations

import pytest

from helios import router

# (message, expected_tier)
TIER_CASES = [
    # light: short, low-stakes chat / lookups
    ("hi", "light"),
    ("thanks", "light"),
    ("what time is it", "light"),
    ("good morning", "light"),
    ("play music", "light"),            # 'play' is light and NOT a _GUI word -> stays light
    # heavy: real reasoning / multi-step / engineering
    ("debug this function", "heavy"),
    ("refactor the auth module", "heavy"),
    ("why is my build broken", "heavy"),
    ("fix this ```py\nx=1\n```", "heavy"),   # code fence -> heavy
    ("a" * 401, "heavy"),                      # very long -> heavy
    # GUI lift: a light-looking phrase that drives the screen must not stay on the weak tier
    ("open spotify", "medium"),
    ("mute the volume", "medium"),
    # integration lift: app/tool tasks need a stronger model
    ("send an email", "medium"),
    ("check my calendar", "medium"),
    # genuinely ambiguous middle band
    ("tell me a fun fact about the ocean and its many creatures", "medium"),
]


@pytest.mark.parametrize("message,tier", TIER_CASES)
def test_tier(message, tier):
    assert router.choose_model(message)["tier"] == tier


def test_override_opus():
    r = router.choose_model("/opus refactor this thing")
    assert r["tier"] == "heavy"
    assert r["model"] == "opus"
    assert r["message"] == "refactor this thing"   # the /override prefix is stripped


def test_override_haiku():
    r = router.choose_model("/haiku hi there")
    assert r["tier"] == "light"
    assert r["model"] == "haiku"


def test_override_sonnet():
    assert router.choose_model("/sonnet do the thing")["tier"] == "medium"
