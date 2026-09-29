"""Helios voice stack.

A fully on-device, CPU-only voice loop that runs in its OWN process (helios/voice/daemon.py),
mirroring the orb's separate-process design — so the heavy audio/ML libraries (onnxruntime,
ctranslate2) and any segfault in them can never take down the brain or the HTTP server.

Pipeline per turn:
    mic (16kHz mono)
      -> wake.py     openWakeWord "hey Helios"            (always-on, 80ms frames)
      -> vad.py      Silero VAD endpointing               (capture until trailing silence)
      -> stt.py      faster-whisper                       (CTranslate2 int8, base.en)
      -> bridge.py   POST /message                        (the existing brain turn)
      -> bridge.py   SSE /events  ->  tts.py  Kokoro      (speak the reply, sentence-streamed)

The daemon talks to the app exactly like the orb: HTTP for actions, SSE for state, and the
shared conf.auth_token(). It posts its own state to /voice/state so the orb + dashboard can
show listening/thinking/speaking.
"""
