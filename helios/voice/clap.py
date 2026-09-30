"""Double-clap detector over the same mic stream the voice daemon already captures.

A clap is a short, loud, broadband transient: a sharp onset immediately preceded by quiet. A
*double* clap is two such onsets separated by a human hand-clap gap (~0.1–1.0s) — distinctive
enough to use as a "wake Helios up" gesture without a wake word.

Detection is LEVEL-ADAPTIVE, not a fixed loudness threshold: an onset must clear both a small
absolute floor AND be several times louder than the running ambient floor (a slow estimate of room
noise). This is what makes it robust to mic differences — a foam windscreen / pop filter damps a
clap's sharp transient so it arrives much quieter, and a fixed threshold would miss it; relative-to-
ambient detection still sees the jump. (Reported by the user: claps registered far better with the
windscreen off — i.e. when claps were louder. Adapting to level fixes that.)

Timing is measured off the AUDIO clock (samples consumed / 16000), not wall-clock, so the inter-clap
gap is exact regardless of scheduling jitter and the detector is deterministic to test. We scan
~10ms sub-windows inside each 80ms mic frame for onset resolution, with a cooldown after a fire.
"""

from __future__ import annotations

import numpy as np

from .. import conf

_SUB = 160          # 10ms @ 16kHz — sub-window for onset timing
_SR = 16000
_SCALE = 32768.0


_DECAY_WINDOW = 0.15   # a clap must fall away within this long after its onset...
_DECAY_RATIO = 0.45    # ...to below this fraction of its peak (speech/music/TV sustain; claps don't)


class ClapDetector:
    def __init__(self, sensitivity: float = 0.15, ratio: float = 8.0,
                 min_gap: float = 0.1, max_gap: float = 1.0, cooldown: float = 1.2,
                 strong_ratio: float = 1.7):
        # sensitivity = the minimum ABSOLUTE peak (0..1) each clap onset must reach (lower = more
        #   sensitive; the binding bar in a quiet room).
        # strong_ratio = at least ONE clap of the pair must reach sensitivity x this — two soft
        #   background bumps can't pair up into a "double clap".
        # ratio = a clap must ALSO be at least this many times the running ambient AVERAGE level —
        #   adapts to a quiet (windscreened) vs loud mic. Based on average (not peak) ambient so the
        #   bar stays near abs_floor in a quiet room and only rises when the room is genuinely noisy.
        self.abs_floor = float(sensitivity)
        self.strong = self.abs_floor * float(strong_ratio)
        self.ratio = float(ratio)
        self.min_gap = float(min_gap)
        self.max_gap = float(max_gap)
        self.cooldown = float(cooldown)
        self.alpha = 0.02            # ambient-floor EMA speed (~0.5s time constant on quiet windows)
        self.reset()

    def reset(self, grace: float = 0.0):
        """Forget any half-heard pair. grace = ignore claps for this many seconds (e.g. right after
        Sleep, so the click that put Helios to sleep can't wake it again)."""
        self._prev_quiet = True      # was the previous sub-window clearly below the onset bar?
        self._last_onset = None      # (audio-time, peak) of the first clap, awaiting a second
        self._cand = None            # (audio-time, peak) of an onset waiting to prove it decays
        self._t = 0.0                # audio clock (seconds), advanced by each frame's length
        self._cooldown_until = float(grace) if grace else -1.0
        self._baseline = 0.02        # running estimate of the ambient (room) peak level

    def _clap(self, t: float, peak: float) -> bool:
        """A confirmed (sharp, decaying) clap at audio-time t. True if it completes a pair."""
        if self._last_onset is not None:
            t0, p0 = self._last_onset
            if self.min_gap <= (t - t0) <= self.max_gap and max(p0, peak) >= self.strong:
                self._last_onset = None
                self._cooldown_until = t + self.cooldown
                return True
        self._last_onset = (t, peak)  # first clap (or a mismatched one restarts the pair)
        return False

    def feed(self, frame_int16) -> bool:
        """Process one mic frame (int16). Returns True exactly once when a double-clap completes."""
        f = np.abs(np.asarray(frame_int16, dtype="float32")).reshape(-1) / _SCALE
        fired = False
        for i in range(0, len(f) - _SUB + 1, _SUB):
            win = f[i:i + _SUB]
            peak = float(win.max())
            avg = float(win.mean())          # average level — stable ambient estimate
            t = self._t + i / _SR
            thr = max(self.abs_floor, self._baseline * self.ratio)   # adaptive onset bar
            onset = self._prev_quiet and peak >= thr
            # The next window counts as "quiet" (re-arming an onset) only well below the bar — so a
            # gradual swell (breathing, speech) doesn't read as a sharp attack the way a clap does.
            self._prev_quiet = peak < max(self.abs_floor * 0.6, self._baseline * 4.0)
            # Track the ambient floor (average level) from non-onset windows only, so claps can't
            # inflate it.
            if peak < thr:
                self._baseline += (avg - self._baseline) * self.alpha
                self._baseline = min(max(self._baseline, 0.003), 0.1)   # clamp: never 0, never huge
            # An onset only counts once it proves to be a clap: sharp, then gone within ~150ms.
            if self._cand is not None:
                ct, cp = self._cand
                if peak < cp * _DECAY_RATIO:
                    self._cand = None
                    conf.log("voice", f"clap (peak {cp:.2f})")
                    fired = self._clap(ct, cp) or fired
                elif t - ct > _DECAY_WINDOW:
                    self._cand = None          # sustained sound (speech, music, TV): not a clap
            if onset and t >= self._cooldown_until and self._cand is None:
                # Diagnostic: log every onset + the bar it cleared, so sensitivity is tunable from
                # logs/voice.log ("clap onset" without a following "clap" = a sustained sound).
                conf.log("voice", f"clap onset (peak {peak:.2f}, bar {thr:.2f})")
                self._cand = (t, peak)
        self._t += len(f) / _SR
        # Forget a lone first clap once the window to pair it has passed.
        if self._last_onset is not None and self._t - self._last_onset[0] > self.max_gap:
            self._last_onset = None
        return fired
