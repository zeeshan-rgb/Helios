"""Double-clap detection must ignore background sound (2026-09-30: the user reported wakes from
background noise). Rules: each clap >= sensitivity, one of the pair >= ~1.7x, sharp (decays within
0.15s), pair within 1.0s, and a grace period after Sleep."""

from __future__ import annotations

import numpy as np
import pytest

from helios.voice.clap import ClapDetector

SR, FRAME = 16000, 1280


def _run(events, total=3.0, det=None, sensitivity=0.15):
    """events: [(start_s, peak 0..1, duration_s)]. Returns True if a double-clap fired."""
    a = np.zeros(int(total * SR), dtype="int16")
    for t, peak, dur in events:
        i = int(t * SR)
        a[i:i + max(1, int(dur * SR))] = int(peak * 32767)
    det = det or ClapDetector(sensitivity=sensitivity)
    fired = False
    for i in range(0, len(a) - FRAME + 1, FRAME):
        fired = det.feed(a[i:i + FRAME]) or fired
    return fired


CLAP = 0.01   # a hand clap: ~10ms of sharp energy


def test_two_real_claps_wake():
    assert _run([(0.5, 0.45, CLAP), (0.9, 0.40, CLAP)])


def test_a_strong_clap_can_pair_with_a_softer_one():
    assert _run([(0.5, 0.37, CLAP), (0.85, 0.18, CLAP)])          # like 18:32:12 in the log


@pytest.mark.parametrize("peaks", [(0.11, 0.11), (0.12, 0.13), (0.10, 0.15), (0.17, 0.18)])
def test_soft_background_bumps_do_not_wake(peaks):
    # The false wakes in the log were pairs like these (0.10-0.15, right at the old 0.10 bar).
    assert not _run([(0.5, peaks[0], CLAP), (0.8, peaks[1], CLAP)])


def test_sustained_sounds_are_not_claps():
    # Speech / music / a TV: loud but lasting a quarter second — not a sharp clap.
    assert not _run([(0.5, 0.5, 0.25), (1.0, 0.5, 0.25)])


def test_claps_too_far_apart_do_not_pair():
    assert not _run([(0.3, 0.45, CLAP), (1.6, 0.45, CLAP)])        # 1.3s apart
    assert _run([(0.3, 0.45, CLAP), (1.2, 0.45, CLAP)])            # 0.9s apart


def test_grace_after_sleep_ignores_the_click():
    det = ClapDetector(sensitivity=0.15)
    det.reset(grace=1.0)
    assert not _run([(0.2, 0.5, CLAP), (0.6, 0.5, CLAP)], det=det)
    det = ClapDetector(sensitivity=0.15)
    det.reset(grace=1.0)
    assert _run([(1.3, 0.5, CLAP), (1.7, 0.5, CLAP)], det=det)


def test_sensitivity_setting_still_tunes_it():
    soft = [(0.5, 0.12, CLAP), (0.8, 0.12, CLAP)]
    assert not _run(soft, sensitivity=0.15)
    assert _run(soft, sensitivity=0.06)        # a very sensitive setting still catches soft claps
