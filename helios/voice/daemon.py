"""The voice daemon — Helios's ears and mouth, in its own process.

Runs the full loop and talks to the app only over HTTP/SSE (see bridge.py), exactly like the orb.
Launched by app._launch_voice() when [voice].enabled; exits on its own if the app disappears.

State machine (one main thread drives capture; the SSE thread drives speech):

    IDLE ──"hey Helios"──▶ LISTENING ──speech+silence──▶ (STT) ──▶ send /message
      ▲                                                                  │
      │                                                                  ▼
   FOLLOWUP ◀──playback drains──  SPEAKING ◀──SSE tokens──  THINKING  (brain turn)
      │  (hot mic, no wake word, intent-gated)
      └──no/ambient speech, or window elapsed──▶ IDLE

Half-duplex echo control: while Helios speaks, the mic is muted and its tail flushed, so it can't
hear itself. Optional barge-in (config) keeps the mic live during speech so "hey Helios" can cut
Helios off mid-sentence. Panic (the brain's stop) instantly silences playback.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time

import numpy as np

# Run as a script (app launches `python helios/voice/daemon.py`): make the package importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from helios import conf, proc_util          # noqa: E402
from helios.voice import earcons            # noqa: E402
from helios.voice import intent             # noqa: E402
from helios.voice.audio import Microphone   # noqa: E402
from helios.voice.clap import ClapDetector  # noqa: E402
from helios.voice.bridge import AppBridge   # noqa: E402
from helios.voice.engines import build_speaker, build_transcriber  # noqa: E402
from helios.voice.stt import clean_for_dictation  # noqa: E402
from helios.voice.vad import Endpointer     # noqa: E402
from helios.voice.wake import WakeWord, FRAME  # noqa: E402
from helios.ambient import AmbientMonitor   # noqa: E402

_PCM_SCALE = 32768.0
_PARTIAL_INTERVAL = 0.7              # seconds between live partial transcriptions while listening
_PARTIAL_MAX_SAMPLES = 10 * 16000   # cap the audio a partial transcribes (cost bound on long speech)
_VU_INTERVAL = 0.08                 # ~12Hz live audio-level push to the orb/HUD VU meter

# Voice permission approval: words that count as yes/no. Negatives win on ambiguity, and anything
# unrecognized falls through to DENY (fail-safe), so a misheard answer never grants access.
_PERM_YES = frozenset({"yes", "yeah", "yep", "yup", "yea", "sure", "ok", "okay", "okey", "aye",
                       "affirmative", "approve", "approved", "proceed", "granted", "confirm",
                       "confirmed", "allow", "allowed", "please"})
_PERM_NO = frozenset({"no", "nope", "nah", "nay", "deny", "denied", "dont", "stop", "cancel",
                      "negative", "reject", "rejected", "decline", "declined", "never", "cancelled"})


class VoiceDaemon:
    def __init__(self):
        c = conf.voice_cfg()
        self.cfg = c
        self.followup_sec = float(c.get("followup_sec", 6) or 0)
        self.max_utterance = float(c.get("max_utterance_sec", 15))
        # After an explicit trigger (wake word / clap / talk key), how long to wait for speech.
        self.listen_sec = float(c.get("listen_sec", 8))
        self.silence_ms = int(c.get("silence_ms", 800))
        self.speak_all = bool(c.get("speak_all", False))
        self.barge_in = bool(c.get("barge_in", False))
        # While speaking, the open mic hears Helios itself, so use a STRICTER wake bar to barge in.
        self.barge_threshold = float(c.get("barge_threshold",
                                           max(0.6, float(c.get("wake_threshold", 0.5)))))
        self.live_transcript = bool(c.get("live_transcript", True))  # stream partials to the HUD
        self._stt_lock = threading.Lock()   # serialize STT calls (partials + final; CT2 isn't reentrant)
        self._mic_level = 0.0               # latest mic RMS (0..1), published by the VU loop
        self._vu_state = None               # which state the VU loop should post level for (or None)

        # Hidden/dormant startup + double-clap wake gesture.
        s = conf.startup_cfg()
        self.wake_gesture = s.get("wake_gesture", "none")
        self.clap_enabled = self.wake_gesture == "double_clap"
        self.clap = ClapDetector(sensitivity=float(s.get("clap_sensitivity", 0.15)),
                                 max_gap=float(s.get("clap_max_gap", 1.0)))
        # Boot dormant (mic listens for the gesture only) — unless the app launched us while awake.
        # HELIOS_VOICE_AWAKE=0 means the app is asleep right now (e.g. Sleep restarted the voice
        # listener so a double-clap can wake it): boot dormant even if [startup].hidden is off.
        self.dormant = self._boot_dormant(s.get("hidden", False), os.environ.get("HELIOS_VOICE_AWAKE"))

        self.mic = Microphone()
        self.wake = WakeWord(c.get("wake_word", "hey_jarvis"),
                             float(c.get("wake_threshold", 0.5)),
                             enable_speaker_verify=bool(c.get("speaker_verify", False)),
                             speaker_verify_threshold=float(c.get("speaker_verify_threshold", 0.70)))
        self.vad = Endpointer(silence_ms=self.silence_ms)
        self.stt = build_transcriber(c)   # faster-whisper (default) | openai | groq
        self.tts = build_speaker(c)        # Kokoro (default) | openai | groq
        self.bridge = AppBridge()
        self.ambient = AmbientMonitor(on_trigger=self._on_ambient_trigger)

        self._stop = threading.Event()
        self._dictation_req = threading.Event()
        self._talk_req = threading.Event()       # talk hotkey: "hey Helios" without saying it
        # Voice lock: act only on the enrolled owner's voice ([voice].speaker_verify).
        self.speaker_lock = bool(c.get("speaker_verify", False))
        self._speaker_verifier = None
        self._warned_unenrolled = False
        self._speaking = threading.Event()      # set while audio is actually playing
        self._turn_complete = threading.Event()  # brain reply finished AND playback drained
        self._done_received = False              # the turn's 'done'/'error' event has arrived
        self._expecting = False                  # a voice command is awaiting its reply
        self._spoke_this_turn = False
        self._barged = False
        self._state = "idle"
        self._perm_q: "queue.Queue[dict]" = queue.Queue()  # pending permission asks (from SSE)

        self.tts.on_speaking(self._on_speaking)

    # ------------------------------------------------------------------ UI / lifecycle
    def _set_state(self, state: str, **fields):
        self._state = state
        self.bridge.post_voice_state(state, **fields)

    def _on_speaking(self, speaking: bool):
        """Speaker callback: mute the mic while talking (echo guard), surface speaking state, and
        complete the turn once playback fully drains."""
        if speaking:
            self._speaking.set()
            if not self.barge_in:
                self.mic.mute(True)
            self._vu_state = "speaking"      # VU loop now pulses the orb/HUD from the TTS level
            self._set_state("speaking")
        else:
            self._speaking.clear()
            self._vu_state = None
            self.mic.mute(False)
            self.mic.flush()                 # drop the echo tail before re-arming
            self._check_turn_complete()

    def _check_turn_complete(self):
        if self._done_received and not self.tts.is_speaking():
            self._turn_complete.set()

    # ------------------------------------------------------------------ brain events (SSE)
    def _on_event(self, kind, data):
        if kind == "model":
            # A brain turn started. Speak it only if it's ours (voice-initiated) or speak_all.
            if self._expecting or self.speak_all:
                self._spoke_this_turn = False
                self._done_received = False
                self._turn_complete.clear()
                self.tts.begin()
                self._set_state("thinking")
        elif kind == "token":
            if (self._expecting or self.speak_all) and isinstance(data, str):
                self._spoke_this_turn = True
                self.tts.feed(data)
        elif kind == "done":
            if self._expecting or self.speak_all:
                text = (data or {}).get("text", "") if isinstance(data, dict) else ""
                if not self._spoke_this_turn and text:
                    self.tts.speak(text)     # nothing streamed (result-only) — speak it whole
                else:
                    self.tts.flush()
                self._done_received = True
                self._expecting = False
                self._check_turn_complete()  # in case there was no audio to play at all
        elif kind == "error":
            if self._expecting or self.speak_all:
                self.tts.stop()
                self.tts.speak("Sorry sir, something went wrong.")
                self._done_received = True
                self._expecting = False
                self._check_turn_complete()
        elif kind == "status":
            # The brain's panic emits a status beginning with the stop glyph — kill speech at once.
            if isinstance(data, str) and data.lstrip().startswith("■"):
                self.tts.stop()
                self._done_received = True
                self._expecting = False
                self._turn_complete.set()
        elif kind == "permission":
            # A risky tool is waiting for approval. Queue it — the MAIN thread speaks it and
            # captures the spoken yes/no (never touch the mic from this SSE thread).
            if isinstance(data, dict) and data.get("id"):
                self._perm_q.put({"id": data["id"],
                                  "summary": data.get("summary") or data.get("tool") or "an action"})
        elif kind == "control":
            # The app broadcasts wake/sleep so a tray/hotkey/power-menu action keeps the daemon's
            # dormancy in sync with the orb + dashboard (the clap path sets dormancy directly).
            action = data.get("action") if isinstance(data, dict) else None
            if action == "wake" and self.dormant:
                self.dormant = False
                self.clap.reset()
                self._set_state("idle")
                conf.log("voice", "woke from dormant (control)")
            elif action == "sleep" and not self.dormant:
                self.dormant = True
                self.clap.reset(grace=1.0)   # the click/tap that put Helios to sleep can't wake it
                self.tts.stop()
                self._turn_complete.set()  # unblock any wait so the loop returns to dormant
                conf.log("voice", "went dormant (control)")

    # ------------------------------------------------------------------ capture
    def _capture(self, start_timeout: float, max_sec: float, state: str = "listening"):
        """Record one utterance. Returns float32 16kHz audio, or None if no speech began in time.

        Endpoints on trailing silence (Silero VAD) or max_sec. While speech is ongoing it streams
        live partial transcriptions to the dashboard HUD (post_voice_state(state, partial=...)) so
        the user sees what Helios is hearing in real time; `state` is the UI state to tag them with
        ("listening" for a command, "dictation" for dictation)."""
        self.vad.reset()
        self.mic.flush()
        frames: list[np.ndarray] = []
        t0 = time.monotonic()
        last_partial = 0.0
        asleep_at_start = self.dormant
        self._vu_state = state   # VU loop now pulses the orb/HUD from the live mic level
        try:
            while not self._stop.is_set():
                if self.dormant and not asleep_at_start:
                    # Put to sleep mid-listen: abandon this capture at once so the loop goes
                    # back to listening for the double-clap (it used to keep recording and
                    # miss every clap until the capture timed out).
                    return None
                frame = self.mic.read(0.3)
                now = time.monotonic()
                if frame is None:
                    self._mic_level *= 0.5
                    if not self.vad.started and now - t0 > start_timeout:
                        return None
                    if now - t0 > max_sec:
                        break
                    continue
                # live mic level (RMS, 0..1) for the VU meter
                fa = frame.astype("float32") / _PCM_SCALE
                self._mic_level = float(np.sqrt(np.mean(fa * fa)))
                ended = self.vad.feed(frame)
                if self.vad.started:
                    frames.append(frame)
                    if self.live_transcript and now - last_partial >= _PARTIAL_INTERVAL:
                        last_partial = now
                        self._spawn_partial(frames[:], state)   # snapshot; transcribe off-thread
                if not self.vad.started and now - t0 > start_timeout:
                    return None
                if ended or now - t0 > max_sec:
                    break
            if not frames:
                return None
            return np.concatenate(frames).astype("float32") / _PCM_SCALE
        finally:
            self._vu_state = None

    def _spawn_partial(self, frames, state):
        """Transcribe the utterance-so-far on a background thread and push it to the HUD as a live
        partial. Skips if STT is already busy (one at a time — CTranslate2 isn't reentrant), so it
        naturally throttles to the model's speed instead of piling up."""
        if not self._stt_lock.acquire(blocking=False):
            return
        def work():
            try:
                pcm = np.concatenate(frames).astype("float32") / _PCM_SCALE
                pcm = pcm[-_PARTIAL_MAX_SAMPLES:]            # bound cost on long utterances
                text = self.stt.transcribe(pcm, gated=False)  # show the live guess, even if rough
                if text and not self.dormant:
                    self.bridge.post_voice_state(state, partial=text)
            except Exception as e:  # pragma: no cover
                conf.log("voice", f"partial stt error: {e}")
            finally:
                self._stt_lock.release()
        threading.Thread(target=work, daemon=True).start()

    def _send(self, text: str):
        self._expecting = True
        self._done_received = False
        self._turn_complete.clear()
        self._set_state("thinking", transcript=text)
        if not self.bridge.send_message(text):
            self._expecting = False
            self.tts.speak("I couldn't reach the brain, sir.")
            self._set_state("idle")

    def _await_turn(self):
        """Block until the brain reply has finished playing (or a generous timeout). With barge-in
        on, poll the live mic for the wake word so the user can cut Helios off mid-reply."""
        cap = float(conf.SETTINGS.get("claude", {}).get("turn_timeout", 600)) + 30
        if not self.barge_in:
            # Poll (not one long wait) so a permission ask arriving mid-turn gets spoken/answered.
            deadline = time.monotonic() + cap
            while not self._stop.is_set():
                if self._turn_complete.wait(0.3):
                    return
                if not self._perm_q.empty():
                    self._drain_permissions()
                if time.monotonic() >= deadline:
                    conf.log("voice", "turn wait timed out; re-arming")
                    self.tts.stop()
                    return
            return
        self._await_turn_barge(cap)

    def _await_turn_barge(self, cap: float):
        """Wait for the turn to finish while staying the SOLE mic consumer and listening for the
        barge wake word. The main loop is parked here for the whole turn, so reading the mic here
        can't race _capture (which only runs after this returns) — no second consumer, no handshake."""
        self.wake.reset()
        self.mic.flush()                  # drop the tail of the user's just-finished command
        deadline = time.monotonic() + cap
        try:
            while not self._stop.is_set():
                if self._turn_complete.is_set():
                    return
                if not self._perm_q.empty():
                    self._drain_permissions()  # speak/answer a mid-turn permission ask
                    continue
                if time.monotonic() >= deadline:
                    conf.log("voice", "turn wait timed out; re-arming")
                    self.tts.stop()
                    return
                frame = self.mic.read(0.3)    # blocks <=300ms -> natural pacing, no busy-spin
                if frame is None:
                    continue
                if len(frame) >= FRAME and self.wake.detect(frame, self.barge_threshold):
                    conf.log("voice", f"barge-in (score {self.wake.score:.2f})")
                    self._do_barge()
                    return
        finally:
            self.wake.reset()             # clean buffer for the next IDLE/follow-up wake gate

    def _do_barge(self):
        """the user said the barge word mid-turn. Cut speech, abort the brain if still in flight, ack,
        and hand back to the main loop, which captures his new command at once (no wake word)."""
        self._barged = True
        need_panic = self._expecting and not self._done_received   # brain still streaming/thinking?
        self.tts.stop()                   # instant playback cut -> also fires _on_speaking(False)
        self.wake.reset()
        self._expecting = False
        self._done_received = True
        self._turn_complete.set()         # unwind the wait + keep the turn flags consistent
        earcons.wake()                    # ack the barge (TTS already silenced, so no collision)
        self._set_state("listening", barged=True)
        if need_panic:
            self.bridge.panic()           # abort the dead turn: frees the lock, halts tools/compute

    # ------------------------------------------------------------------ voice permission approval
    def _drain_permissions(self):
        """Handle any queued permission asks (spoken by the MAIN thread so there's one mic user)."""
        while not self._perm_q.empty():
            try:
                perm = self._perm_q.get_nowait()
            except Exception:
                return
            self._handle_permission(perm)

    def _handle_permission(self, perm: dict):
        """Speak 'I need permission to X', listen for a spoken yes/no, and POST the decision.
        Gives two tries (re-asking 'yes or no?' if it can't tell), with a generous window — the
        hook waits up to ~120s. A clear NO, or no clear answer after both tries -> DENY (fail-safe);
        the hard rails in the hook (panic / SSRF / ~/.claude) still block regardless."""
        rid = perm.get("id")
        if not rid:
            return
        summary = perm.get("summary") or "an action"
        conf.log("voice", f"permission ask: {summary!r}")
        self.tts.stop()                      # cut any in-progress speech so the ask is heard clearly
        self._set_state("permission", transcript=summary)
        self.tts.speak(f"Sir, I need permission to {self._perm_phrase(summary)}. Shall I proceed?")
        decision = "deny"
        for attempt in (1, 2):
            self._wait_speaking_done()       # half-duplex: don't record our own question
            self.mic.mute(False)             # make sure the mic is live (avoid a speaking-race)
            self.mic.flush()
            audio = self._capture(start_timeout=12.0, max_sec=12.0, state="permission")
            answer = ""
            # Only the owner can approve by voice: a stranger's "yes" counts as no answer (-> deny).
            if audio is not None and self._owner_voice(audio, "permission answer"):
                with self._stt_lock:
                    answer = self.stt.transcribe(audio, gated=False) or ""
            verdict = self._classify_answer(answer)
            conf.log("voice", f"permission answer {answer!r} -> {verdict} (attempt {attempt})")
            if verdict in ("allow", "deny"):
                decision = verdict
                break
            if attempt == 1:                 # couldn't tell — ask once more, then give up (deny)
                self.tts.speak("Sorry sir, I didn't catch that. Yes, or no?")
        self.bridge.respond_permission(rid, decision)
        self.tts.speak("Very good, sir." if decision == "allow" else "Denied, sir.")

    def _wait_speaking_done(self, timeout: float = 20.0):
        """Block until TTS playback drains (so we don't capture Helios's own voice)."""
        t0 = time.monotonic()
        time.sleep(0.15)                     # give playback a beat to start
        while self.tts.is_speaking() and not self._stop.is_set():
            if time.monotonic() - t0 > timeout:
                break
            time.sleep(0.05)

    @staticmethod
    def _classify_answer(text: str) -> str:
        """Classify a spoken answer as 'allow' / 'deny' / 'unclear'. Negation wins over a positive
        ('not sure', 'do not') -> deny; an unrecognized or empty answer -> 'unclear' (re-askable)."""
        t = (text or "").lower()
        words = set(re.findall(r"[a-z']+", t.replace("'", "")))
        if not words:
            return "unclear"
        # "not" negates a following positive ("not sure", "do not", "not now") -> deny (fail-safe).
        if (words & _PERM_NO) or ("not" in words):
            return "deny"
        if (words & _PERM_YES) or any(p in t for p in ("go ahead", "do it", "go for it",
                                                       "permission granted")):
            return "allow"
        return "unclear"

    @staticmethod
    def _parse_yes_no(text: str) -> str:
        """'allow' / 'deny' only (unclear -> deny, fail-safe) — the final decision after re-asks."""
        return "allow" if VoiceDaemon._classify_answer(text) == "allow" else "deny"

    @staticmethod
    def _perm_phrase(summary: str) -> str:
        """Lower the leading capital of the summary so it reads naturally after 'permission to'."""
        s = (summary or "an action").strip()
        return (s[0].lower() + s[1:]) if s else "an action"

    # ------------------------------------------------------------------ speaker verification
    def _speaker(self):
        """The voice-lock verifier (lazy; exists even while the lock is off, so enrollment works)."""
        if self._speaker_verifier is None:
            from helios.voice.speaker_verify import SpeakerVerifier
            self._speaker_verifier = SpeakerVerifier(
                threshold=float(self.cfg.get("speaker_verify_threshold", 0.70)))
        return self._speaker_verifier

    def _owner_voice(self, audio, context: str) -> bool:
        """Voice lock: True if `audio` may be acted on. With [voice].speaker_verify on and a
        voiceprint enrolled, only the enrolled voice passes. Lock on but not enrolled yet -> pass
        (never lock the owner out before enrolling). A verifier failure while locked -> reject
        (fail closed: typing in the dashboard still works)."""
        if not self.speaker_lock:
            return True
        try:
            verifier = self._speaker()
            if not verifier.is_enrolled():
                if not self._warned_unenrolled:
                    self._warned_unenrolled = True
                    conf.log("voice", "voice lock is on but no voiceprint is enrolled yet — "
                                      "accepting all voices until you say 'enroll my voice'")
                return True
            ok, sim = verifier.verify(audio)
        except Exception as e:
            conf.log("voice", f"voice lock check failed ({e}) — rejecting {context} for safety")
            return False
        conf.log("voice", f"voice lock {context}: {'owner' if ok else 'NOT the owner'} (sim {sim:.2f})")
        return ok

    def _handle_speaker_enrollment(self):
        """Interactive enrollment: the user reads 3 varied sentences to build a rich voiceprint.
        On success the voice lock turns on (and is saved to settings)."""
        try:
            verifier = self._speaker()
            verifier._load_model()
        except Exception as e:
            conf.log("voice", f"speaker verification unavailable: {e}")
            self.tts.speak("Voice verification isn't available on this machine yet.")
            return

        conf.log("voice", "starting speaker enrollment")
        # Varied sentences capture more of the user's voice characteristics (pitch, accent, rhythm) than
        # repetition of a single phrase. This makes the embedding more robust to noise + variations.
        sentences = [
            "The weather is quite nice today, isn't it?",
            "I'd like to schedule a meeting for next Tuesday morning.",
            "Please remind me to check on the project status."
        ]
        self.tts.speak("Sir, I'll record three varied sentences to build a richer speaker profile.")
        samples = []
        i = 0
        misses = 0
        while i < 3:
            sentence = sentences[i]
            self.tts.speak(f"Sample {i + 1} of 3. Please read: {sentence}")
            self._wait_speaking_done()
            self.mic.mute(False)
            self.mic.flush()
            audio = self._capture(start_timeout=6.0, max_sec=8.0, state="enrollment")
            if audio is None:
                misses += 1
                if misses >= 3:  # give up rather than loop forever on a dead mic
                    self.tts.speak("I'm not hearing you — let's stop there, sir.")
                    break
                self.tts.speak(f"I didn't catch that. Let's try again: {sentence}")
                continue
            samples.append((audio * 32768.0).astype("int16"))
            if i < 2:
                self.tts.speak(f"Recorded. {2 - i} more to go.")
            else:
                self.tts.speak("Excellent, sir.")
            i += 1

        if len(samples) == 3:
            if verifier.enroll(samples):
                self.speaker_lock = True
                try:
                    conf.update_settings({"voice.speaker_verify": True})
                except Exception as e:
                    conf.log("voice", f"couldn't save voice lock setting: {e}")
                self.tts.speak("Your voice profile is ready. Voice lock is on: "
                               "from now on I'll only respond to you.")
                conf.log("voice", "enrollment complete — voice lock ON")
            else:
                self.tts.speak("Enrollment failed. Please try again later.")
        else:
            self.tts.speak("Enrollment cancelled.")

    # ------------------------------------------------------------------ ambient mode
    def _on_ambient_trigger(self, watch, message: str):
        """Callback: a watch triggered — speak the alert. (Watches are created with action='voice';
        there's no separate notify channel from the daemon, so everything is spoken.)"""
        conf.log("ambient", message)
        if watch.click_target:
            # message is already a full sentence from ambient._fire_click ("clicked 'OK' (...)"
            # or "no button matching ... was found") — speak it as-is rather than reformatting.
            self.tts.speak(f"Sir, {message}.")
        else:
            self.tts.speak(f"Watch alert: {watch.condition}")

    def _handle_voice_commands(self, text: str) -> bool:
        """Handle local voice commands. Returns True if command was handled."""
        t = text.lower().strip()

        # Speaker enrollment. "enroll" is a word faster-whisper regularly mangles on a short,
        # barge-in-captured utterance ("and role", "roll", "and the role" have all been observed) —
        # "speaker" comes through clean. Match a SHORT command containing "speaker" + an enroll-ish
        # word (word-level, ≤4 words) so a normal sentence that merely mentions a "role" or a
        # "speaker" (e.g. "what's the role of the keynote speaker?") can't hijack it.
        words = re.findall(r"[a-z]+", t)
        wset = set(words)
        if ("enroll my voice" in t or "enroll voice" in t
                or ("speaker" in wset and len(words) <= 4
                    and (wset & {"enroll", "enrol", "roll", "role", "verify"}))):
            self._handle_speaker_enrollment()
            return True

        # Ambient watch commands. "watch for X" alerts when X disappears from the CURRENT
        # foreground window (e.g. a build finishing). "watch for X and click Y" instead fires
        # the moment X appears, clicks the element named Y, then removes itself (one-shot).
        if t.startswith("watch for "):
            rest = text[10:].strip()
            click_target = None
            m = re.search(r"^(.*?)\s+and click\s+(.+)$", rest, re.IGNORECASE)
            if m:
                condition, click_target = m.group(1).strip(), m.group(2).strip()
            else:
                condition = rest
            if condition:
                pattern = self.ambient.current_window_pattern()  # pin to THIS window, not any window
                self.ambient.add_watch(pattern, condition, interval_sec=2.0, action="voice",
                                       click_target=click_target)
                if click_target:
                    self.tts.speak(f"Watching this window for {condition}. "
                                   f"I'll click {click_target} when it shows up.")
                else:
                    self.tts.speak(f"Watching this window for: {condition}")
                return True

        if any(x in t for x in ("stop watching", "clear watches", "forget watches")):
            self.ambient.clear_watches()
            self.tts.speak("All watches cleared, sir.")
            return True

        return False

    # ------------------------------------------------------------------ dictation
    def _handle_dictation(self):
        self._dictation_req.clear()
        self._set_state("dictation")
        conf.log("voice", "dictation started")
        audio = self._capture(start_timeout=6.0, max_sec=60.0, state="dictation")
        if audio is None:
            self._set_state("idle")
            return
        with self._stt_lock:                   # wait for any in-flight live partial to finish
            raw = self.stt.transcribe(audio, gated=False)
        text = clean_for_dictation(raw)
        if text:
            self._type_text(text + " ")
            conf.log("voice", f"dictated {len(text)} chars")
        self._set_state("idle")

    def _type_text(self, text: str):
        """Type a transcript into whatever window currently has focus (pynput keyboard)."""
        try:
            from pynput.keyboard import Controller
            Controller().type(text)
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"dictation typing failed: {e}")

    @staticmethod
    def _boot_dormant(hidden, awake_env: str | None) -> bool:
        """Start asleep? "0" = the app is asleep now (always dormant), "1" = the app is awake
        (never), unset = follow [startup].hidden."""
        return awake_env == "0" or (bool(hidden) and awake_env != "1")

    def _clap_is_wake(self) -> bool:
        """With no usable wake-word model, the double-clap stands in for "hey Helios" (wake AND
        start listening). Once the model exists, a clap only summons the dashboard again."""
        return self.clap_enabled and not self.wake.available

    # ------------------------------------------------------------------ main loop
    def _main_loop(self):
        in_followup = False   # True = skip the wake word (hot mic right after Helios spoke)
        barge_capture = False  # True = this hot-mic pass follows a barge-in (full timeout, no gate)
        while not self._stop.is_set():
            # A risky tool is waiting for approval — speak it + capture the yes/no (works in any
            # state, including dormant, since a background agent can trigger it while Helios sleeps).
            if not self._perm_q.empty():
                self._drain_permissions()
                continue

            if self._dictation_req.is_set() and not self.dormant:
                self._handle_dictation()
                in_followup = False
                barge_capture = False
                continue

            if self._talk_req.is_set():
                # Talk hotkey = "hey Helios": wake if asleep, cut off any speech, and listen now.
                self._talk_req.clear()
                conf.log("voice", "talk hotkey -> listening")
                if self.dormant:
                    self.dormant = False
                    self.bridge.summon()
                self.tts.stop()
                self.clap.reset()
                earcons.wake()
                in_followup = True
                barge_capture = True

            if self.dormant:
                # Hidden/dormant: the mic listens for the wake GESTURE (double-clap) OR the wake
                # WORD ("hey Helios") — either wakes Helios (app reveals the orb + dashboard + apps).
                # A hot-mic follow-up from before the sleep must not fire after a later wake.
                in_followup = barge_capture = False
                frame = self.mic.read(0.3)
                if frame is None or self._speaking.is_set():
                    continue
                if self.clap_enabled and self.clap.feed(frame):
                    conf.log("voice", "double-clap -> waking Helios")
                    self.dormant = False
                    self.clap.reset()
                    self.bridge.summon()      # app un-dormants: orb + dashboard + open_on_wake apps
                    if self._clap_is_wake():
                        earcons.wake()        # no wake word yet: the clap IS the "hey Helios"
                        in_followup = True
                        barge_capture = True
                    else:
                        self._set_state("idle")
                elif len(frame) >= FRAME and self.wake.detect(frame):
                    # Wake word from sleep: un-dormant AND go straight to capturing the command,
                    # since the user just addressed Helios (no need to say "hey Helios" twice).
                    conf.log("voice", f"wake word -> waking Helios (score {self.wake.score:.2f})")
                    self.dormant = False
                    self.wake.reset()
                    self.clap.reset()
                    self.bridge.summon()
                    earcons.wake()
                    in_followup = True        # hot mic next iteration...
                    barge_capture = True      # ...full window, skip the ambient-intent gate
                continue

            if not in_followup:
                # IDLE: wait for the wake word (a double-clap also summons the dashboard).
                if self._state != "idle":
                    self._set_state("idle")
                frame = self.mic.read(0.3)
                if frame is None or self._speaking.is_set():
                    continue
                if self.clap_enabled and self.clap.feed(frame):
                    if self._clap_is_wake():
                        conf.log("voice", "double-clap -> listening (no wake-word model)")
                        self.clap.reset()
                        earcons.wake()
                    else:
                        conf.log("voice", "double-clap -> summon dashboard")
                        self.bridge.summon()
                        continue
                elif len(frame) >= FRAME and self.wake.detect(frame):
                    conf.log("voice", f"wake (score {self.wake.score:.2f})")
                    self.wake.reset()
                    self.clap.reset()         # clear any pending clap onset before we capture
                    earcons.wake()            # "I'm listening" chime
                else:
                    continue

            # LISTENING (after wake) / FOLLOWUP (hot mic) capture. A barge capture is hot-mic but
            # gets the full listen window (the user just interrupted — give him time to state the command).
            self._set_state("listening")
            explicit = barge_capture or not in_followup   # wake word / clap / talk key, not a hot mic
            start_timeout = self.listen_sec if explicit else self.followup_sec
            if explicit:
                conf.log("voice", f"listening (up to {start_timeout:.0f}s for speech to start)")
            audio = self._capture(start_timeout=start_timeout, max_sec=self.max_utterance)
            was_followup, in_followup = in_followup, False
            this_barge, barge_capture = barge_capture, False
            if self.dormant:
                # Slept during the capture: drop whatever was heard and wait for the clap again.
                conf.log("voice", "listen cancelled — Helios was put to sleep")
                continue
            if audio is None:
                if explicit:
                    conf.log("voice", "no speech heard — back to idle")
                self._set_state("idle")
                continue
            if not self._owner_voice(audio, "command"):
                self._set_state("idle")         # someone else (or the TV): ignore silently
                continue

            with self._stt_lock:               # wait for any in-flight live partial to finish
                text = self.stt.transcribe(audio)
            if not text:
                conf.log("voice", f"speech captured ({len(audio) / 16000:.1f}s) but not understood")
                if not was_followup:
                    earcons.miss()             # heard speech but couldn't make it out
                self._set_state("idle")
                continue
            # Follow-up speech must look directed; wake-word AND barge-in speech always are.
            if was_followup and not this_barge and not intent.is_directed(text):
                conf.log("voice", f"ignored ambient follow-up: {text!r}")
                self._set_state("idle")
                continue

            conf.log("voice", f"heard: {text!r}")
            # Check for local voice commands before sending to brain
            if not self._handle_voice_commands(text):
                self._send(text)
                self._await_turn()
            # If the user barged in mid-reply, go straight back to capture for his new command — hot mic,
            # no wake word, full window, no ambient gate (the barge already signalled intent).
            if self.barge_in and self._barged:
                self._barged = False
                in_followup = True
                barge_capture = True
                continue
            # After the reply, optionally keep the mic hot for a no-wake follow-up.
            in_followup = self.followup_sec > 0 and not self._stop.is_set()

    # ------------------------------------------------------------------ VU meter
    def _vu_loop(self):
        """Push a live audio level (~12Hz) so the orb + dashboard HUD pulse with the sound — the
        mic level while listening, the TTS output level while speaking. Scaled up since speech RMS
        is small. Posts only when there's something to show (listening/speaking)."""
        while not self._stop.is_set():
            st = self._vu_state
            try:
                if st == "speaking":
                    self.bridge.post_level("speaking", min(1.0, self.tts.level() * 3.0))
                elif st:
                    self.bridge.post_level(st, min(1.0, self._mic_level * 4.0))
            except Exception:
                pass
            time.sleep(_VU_INTERVAL)

    # ------------------------------------------------------------------ watchdog
    def _watchdog(self):
        misses = 0
        while not self._stop.is_set():
            time.sleep(5.0)
            if self.bridge.health():
                misses = 0
            else:
                misses += 1
                if misses >= 4:  # ~20s gone -> the app is down, don't linger
                    conf.log("voice", "app gone; voice daemon exiting")
                    self._clear_pid()
                    os._exit(0)

    # ------------------------------------------------------------------ pid file
    def _write_pid(self):
        try:
            conf.VOICE_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            conf.VOICE_PID_FILE.write_text(
                json.dumps({"pid": os.getpid(), "ctime": proc_util.own_creation_time()}),
                encoding="utf-8")
        except Exception:
            pass

    def _clear_pid(self):
        try:
            conf.VOICE_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------ run
    def run(self):
        self._write_pid()
        if not self.mic.start():
            self._set_state("disabled")
            conf.log("voice", "no microphone — voice daemon idle (engines not loaded)")
            # Stay alive so the app can still manage us, but do nothing.
            threading.Thread(target=self._watchdog, daemon=True).start()
            self._stop.wait()
            return

        # Warm the models up off the main loop so the first interaction is snappy.
        threading.Thread(target=self._warmup, daemon=True).start()
        # SSE: receive brain events to speak + track turn lifecycle.
        threading.Thread(target=self.bridge.listen, args=(self._on_event, self._stop.is_set),
                         daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._vu_loop, daemon=True).start()
        self._start_hotkey()
        # Start ambient monitor
        self.ambient.start()
        if self.dormant:
            conf.log("voice", "voice daemon started (dormant — listening for the wake gesture)")
        else:
            self.bridge.post_voice_state("idle")  # tell the UI voice is live (mic button lights up)
            conf.log("voice", "voice daemon started")
        try:
            self._main_loop()
        finally:
            self.close()

    def _warmup(self):
        for w in (self.wake.warmup, self.vad.warmup, self.stt.warmup, self.tts.warmup):
            if self._stop.is_set():
                return
            try:
                w()
            except Exception:
                pass

    def _hotkey_map(self) -> dict:
        """Global voice hotkeys: dictation into the focused field, and 'talk to Helios' (the
        keyboard equivalent of saying the wake word)."""
        keys = {}
        if self.cfg.get("dictation_hotkey"):
            keys[self.cfg["dictation_hotkey"]] = self._dictation_req.set
        talk = self.cfg.get("talk_hotkey", "<ctrl>+<alt>+h")
        if talk:
            keys[talk] = self._talk_req.set
        return keys

    def _start_hotkey(self):
        keys = self._hotkey_map()
        if not keys:
            return
        def run():
            try:
                from pynput import keyboard
                with keyboard.GlobalHotKeys(keys) as h:
                    h.join()
            except Exception as e:  # pragma: no cover
                conf.log("voice", f"voice hotkeys failed: {e}")
        threading.Thread(target=run, daemon=True).start()

    def close(self):
        self._stop.set()
        try:
            self.ambient.stop()
        except Exception:
            pass
        try:
            self.tts.close()
        except Exception:
            pass
        try:
            self.mic.close()
        except Exception:
            pass
        self._clear_pid()


def main():
    try:
        VoiceDaemon().run()
    except Exception as e:  # pragma: no cover
        conf.log("voice", f"voice daemon crashed: {e}")
    finally:
        try:
            conf.VOICE_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
