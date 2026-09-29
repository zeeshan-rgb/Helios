"""TTS sentence splitting must not break after abbreviations or single initials ('Dr.', 'e.g.',
'J. Smith') — those false stops made Helios pause mid-name. Decimals/versions ('3.14', 'v1.0') are
already safe because the boundary regex needs whitespace/end after the period. Targets the pure
_first_stop helper in helios.voice.tts (no audio hardware touched)."""
from __future__ import annotations

from helios.voice.tts import _first_stop


def _head(text):
    """The first speakable segment _drain_sentences would cut, or None if no real boundary yet."""
    m = _first_stop(text)
    return text[:m.end()] if m else None


def test_real_boundary_still_splits():
    assert _head("Hello world. Bye.") == "Hello world."


def test_abbrev_not_split():
    assert _head("Dr. Smith arrived. Bye.") == "Dr. Smith arrived."


def test_eg_not_split():
    assert _head("Use it, e.g. here. Done.") == "Use it, e.g. here."


def test_single_initial_not_split():
    assert _head("I met J. Robert today. Bye.") == "I met J. Robert today."


def test_decimal_never_matched():
    # period between digits is not a boundary at all -> first real stop is after "today."
    assert _head("Pi is 3.14 today. Done.") == "Pi is 3.14 today."


def test_version_not_split():
    assert _head("Use v1.0 now. Then stop.") == "Use v1.0 now."


def test_only_abbrev_waits():
    # no real sentence end yet -> None (stream waits for more; flush() speaks the remainder)
    assert _first_stop("etc. and so on") is None


def test_question_and_bang_still_split():
    assert _head("Really? Yes.") == "Really?"
    assert _head("Wow! Ok.") == "Wow!"


def test_newline_is_boundary():
    assert _head("line one\nline two") == "line one\n"
