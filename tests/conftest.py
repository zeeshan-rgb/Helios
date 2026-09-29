"""Make the repo root importable so `from helios import ...` resolves under pytest.

The tests target Helios's pure decision functions (permissions / router / voice.intent), which
import only `helios.conf` + stdlib — no audio, no app, no `claude`, so the suite runs fast and
offline. Putting the repo root on sys.path here means pytest can be invoked from anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent  # C:\Users\Tim\helios
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
