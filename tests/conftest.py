"""Make the repo root importable so `from helios import ...` resolves under pytest.

The tests target Helios's pure decision functions (permissions / router / voice.intent), which
import only `helios.conf` + stdlib — no audio, no app, no `claude`, so the suite runs fast and
offline. Putting the repo root on sys.path here means pytest can be invoked from anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent  # the repo root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _isolate_runtime_flags(tmp_path_factory, monkeypatch):
    """No test may touch the live app's cross-process flags in logs/ (a brain.panic() in a test
    used to leave a real panic stop behind). Tests that want a flag patch it again themselves."""
    from helios import conf
    d = tmp_path_factory.mktemp("flags")
    monkeypatch.setattr(conf, "ABORT_FLAG", d / "abort.flag")
    monkeypatch.setattr(conf, "YOLO_FLAG", d / "yolo.flag")
    monkeypatch.setattr(conf, "SCREEN_LOCK", d / "screen.lock")


@pytest.fixture(autouse=True)
def _no_real_agy(monkeypatch):
    """Tests never launch the real Antigravity CLI (slow, networked, uses the user's quota).
    A scripted stand-in (a .py 'bin', see test_antigravity's fake_agy) still works."""
    from helios import agy_cli
    real = agy_cli.command

    def guarded():
        cmd = real()
        return cmd if cmd and cmd[-1].lower().endswith(".py") else []
    monkeypatch.setattr(agy_cli, "command", guarded)
