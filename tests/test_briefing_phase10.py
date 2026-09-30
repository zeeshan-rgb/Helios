"""Phase 10 morning briefing: built from last night's record (per project, learning, research,
approvals; completed / observed / suggested / failed / skipped apart), honest about missed or
partial nights, a short spoken version, once-a-day preparation at the configured time, read aloud on
the first wake, and the voice listener's announcement queue."""

from __future__ import annotations

import importlib.util
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from helios import briefing, conf, memory_store
from helios.night_mode import scheduler as sched

_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 1, 8, 30)
NIGHT = "2026-10-01"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for d in ("data", "vault", "projects"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(conf, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path / "vault")
    monkeypatch.setattr(conf, "projects_dir", lambda: tmp_path / "projects")
    monkeypatch.setattr(sched, "cfg", lambda: {"enabled": True, "start": "01:00", "end": "05:00"})
    bcfg = {"enabled": True, "time": "08:00", "notify": True, "speak_on_wake": True}
    monkeypatch.setattr(briefing, "cfg", lambda: bcfg)
    return SimpleNamespace(tmp=tmp_path, bcfg=bcfg)


def _health(verdict="ATTENTION", tests="PASS (30s, 7 h ago)", status="ok", issues=("no tests configured",)):
    return [{"project": "Helios", "verdict": verdict, "issues": list(issues), "rows": [
        {"row": "Build", "status": "none", "text": "not configured"},
        {"row": "Tests", "status": status, "text": tests},
        {"row": "Git status", "status": "ok", "text": "main · clean"}]}]


def _record(status="completed", **over):
    rec = {"night": NIGHT, "kind": "scheduled", "status": status, "report": "C:/vault/Night Reports/x.md",
           "tasks": {
               "sync_projects": {"status": "ok", "summary": "1 scanned", "completed": ["scanned 1 project(s)"],
                                 "observed": ["Helios: 2 commit(s)"], "suggested": [], "approval": [],
                                 "failed": [], "data": {"Helios": {"git": True, "commits": 2, "uncommitted": 3, "files": 0}}},
               "run_checks": {"status": "ok", "summary": "1 passed", "completed": ["Helios test: PASS (30s)"],
                              "observed": [], "suggested": [], "approval": [], "failed": [],
                              "data": {"health": _health()}},
               "research": {"status": "ok", "summary": "2 topic(s): 3 new", "completed": [], "observed": [],
                            "suggested": [], "approval": [], "failed": [],
                            "data": {"new": 3, "duplicates": 1, "top": [
                                {"topic": "Gemini", "title": "Gemini 4 enters post-training",
                                 "source_name": "9to5Google", "confidence": 0.9, "source_check": "ok",
                                 "id": "res-20261001-abcdef"}]}},
               "extract_memories": {"status": "ok", "summary": "2 new", "completed": ["learned (preferences): Likes tea"],
                                    "observed": [], "suggested": [], "approval": ["new rule: Keep answers short (`helios learn approve rule-1`)"],
                                    "failed": [], "data": {"learned": [
                                        {"category": "preferences", "text": "Likes tea", "status": "active", "id": "pref-1"},
                                        {"category": "rules", "text": "Keep answers short", "status": "pending", "id": "rule-1"}]}},
           }}
    rec.update(over)
    return rec


# ------------------------------------------------------------------ building + rendering

def test_briefing_sections_from_a_completed_night(env):
    memory_store.remember("Keep answers short", "rules", status="pending")
    b = briefing.build(NOW, _record())
    text = briefing.render(b)
    assert text.startswith("# Good morning, sir — Thursday 01 October 2026")
    assert "Night Mode finished without problems" in text
    assert "### Helios — ATTENTION" in text and "- Tests: PASS (30s" in text
    assert "- Build:" not in text                                   # not configured -> not shown as a row
    assert "- Changes detected: 2 commit(s), 3 uncommitted file(s)" in text
    assert "- Potential issues: 1: no tests configured" in text
    assert "- New memories: 1" in text and "(preferences) Likes tea" in text
    assert "- New rules/skills waiting for approval: 1" in text
    assert "- New findings: 3 (already known: 1)" in text and "Gemini 4 enters post-training — 9to5Google" in text
    assert "## Needs your approval" in text and "rule: Keep answers short — `helios learn approve rule-" in text
    for h in ("## Completed", "## Observed", "## Suggested", "## Failed", "## Skipped"):
        assert h in text
    assert "- Helios test: PASS (30s)" in text


def test_failures_are_never_reported_as_success(env):
    rec = _record(status="completed with errors")
    rec["tasks"]["run_checks"]["data"]["health"] = _health("FAILING", "FAIL (exit 1, 7 h ago) — 2 failed", "fail",
                                                           ("Tests failing",))
    rec["tasks"]["run_checks"]["failed"] = ["Helios test: FAIL (exit 1, 30s)"]
    rec["tasks"]["run_checks"]["completed"] = []
    rec["tasks"]["research"] = {"status": "failed", "summary": "boom", "completed": [], "observed": [],
                                "suggested": [], "approval": [], "failed": [], "data": {}}
    b = briefing.build(NOW, rec)
    text, said = briefing.render(b), briefing.spoken(b)
    completed = text.split("## Completed")[1].split("## Observed")[0]
    assert "FAIL" not in completed and "Helios test: FAIL" in text.split("## Failed")[1]
    assert "Night Mode finished, but 1 task(s) failed" in text
    assert "Helios is failing: the tests fail." in said and "Nothing failed" not in said
    assert "- failed: boom" in text                                  # research didn't run -> says so


def test_checks_not_run_tonight_are_flagged(env):
    rec = _record()
    del rec["tasks"]["run_checks"]
    assert "no projects configured" in briefing.render(briefing.build(NOW, rec))
    app = env.tmp / "app"
    app.mkdir()
    (env.tmp / "projects" / "app.yaml").write_text(f"name: App\npath: {app.as_posix()}\ndependencies: off\n",
                                                   encoding="utf-8")
    b = briefing.build(NOW, rec)
    assert not b["health_from_night"] and b["projects"][0]["name"] == "App"
    assert "latest known state — the checks didn't run tonight" in briefing.render(b)


def test_missed_and_missing_nights(env):
    missed = {"night": NIGHT, "kind": "scheduled", "status": "missed", "tasks": {},
              "reason": "the PC was asleep from 00:30 to 07:45"}
    b = briefing.build(NOW, missed)
    assert "Night Mode didn't run last night — the PC was asleep" in briefing.render(b)
    assert "didn't run last night" in briefing.spoken(b)
    none = briefing.build(NOW, {})
    assert "There's no Night Mode report for last night" in briefing.render(none)


def test_spoken_version_is_short_and_clean(env):
    memory_store.remember("Keep answers short", "rules", status="pending")
    said = briefing.spoken(briefing.build(NOW, _record()))
    assert said.startswith("Good morning, sir. Night Mode finished without problems.")
    assert "Research turned up 3 new findings, including: Gemini 4 enters post-training." in said
    assert "I learned 1 new thing, and 1 lesson is waiting for your approval." in said
    assert not re.search(r"https?://|res-\d|rule-\d|`|#|\*\*", said) and len(said) < 900


def test_spoken_picks_the_most_important_issue_in_plain_words():
    issues = ["pip check: resemblyzer 0.1.4 requires typing, which is not installed.",
              "3 dependency major version(s) behind", "no tests configured",
              "security advisories: 1 high, 1 moderate — postcss (high) (`npm audit` for details)"]
    assert briefing._top_issue(issues) == "1 high-severity security advisory"
    assert briefing._top_issue(issues[:3]) == "3 dependencies are a major version behind"
    assert briefing._top_issue(["2 commit(s) not pushed", "no tests configured"]) == "2 commits aren't pushed"
    b = {"night_status": "stopped (panic stop)", "night_reason": "", "sections": {}, "learned": [],
         "pending": [], "research": {}, "projects": [
             {"name": "Aegis", "verdict": "UNKNOWN", "lines": [],
              "issues": ["2 commit(s) not pushed", "health checks never run"]},
             {"name": "Site", "verdict": "OK", "lines": [], "issues": []}]}
    said = briefing.spoken(b)
    assert said == ("Good morning, sir. Night Mode was stopped before it finished. Aegis hasn't been "
                    "checked yet, and 2 commits aren't pushed. Site looks healthy.")


def test_last_night_record_is_recent_only(env):
    sched.save_run(_record())
    assert briefing.last_night_record(NOW)["night"] == NIGHT
    assert briefing.last_night_record(NOW + timedelta(days=3)) is None


# ------------------------------------------------------------------ delivery

def test_tick_prepares_once_at_the_configured_time(env, monkeypatch):
    sched.save_run(_record())
    said = []
    assert briefing.tick(NOW.replace(hour=7, minute=59), emit=lambda k, d: said.append(d)) == "early"
    monkeypatch.setattr(sched, "is_running", lambda: True)
    assert briefing.tick(NOW, emit=lambda k, d: said.append(d)) == "waiting"      # night still going
    assert briefing.tick(NOW + timedelta(hours=2)) == "prepared"                  # ...but not forever
    assert briefing.tick(NOW + timedelta(hours=3)) == "done"
    assert (env.tmp / "vault" / "Briefings" / "2026-10-01.md").exists()
    monkeypatch.setattr(sched, "is_running", lambda: False)
    env.bcfg["enabled"] = False
    assert briefing.tick(NOW) == "disabled"


def test_tick_notifies(env, monkeypatch):
    sched.save_run(_record())
    seen = []
    from helios import notify
    monkeypatch.setattr(notify, "toast", lambda t, m: seen.append(("toast", t)))
    briefing.tick(NOW, emit=lambda k, d: seen.append((k, d)))
    assert ("toast", "Helios — Good morning") in seen
    assert any(k == "status" and "briefing is ready" in d for k, d in seen)


def test_read_aloud_on_first_wake_only(env):
    sched.save_run(_record())
    pub = []
    assert not briefing.on_wake(lambda k, d: pub.append((k, d)), NOW.replace(hour=7))   # too early
    assert briefing.on_wake(lambda k, d: pub.append((k, d)), NOW)                        # prepares + says it
    assert pub[0][0] == "announce" and pub[0][1]["text"].startswith("Good morning, sir.")
    assert not briefing.on_wake(lambda k, d: pub.append((k, d)), NOW + timedelta(minutes=5))
    assert len(pub) == 1


def test_speak_on_wake_can_be_turned_off(env):
    sched.save_run(_record())
    env.bcfg["speak_on_wake"] = False
    assert not briefing.on_wake(lambda k, d: None, NOW)


def test_latest_text_on_request(env):
    sched.save_run(_record())
    assert briefing.latest_text("2026-01-01") == "No briefing for 2026-01-01."
    text = briefing.latest_text()
    assert text.startswith("# Good morning, sir")
    assert briefing.latest_text(spoken_version=True).startswith("Good morning, sir.")


def test_briefing_is_written_to_the_vault_not_the_code(env):
    out = briefing.prepare(NOW)
    assert Path(out["path"]).is_relative_to(env.tmp / "vault")


# ------------------------------------------------------------------ voice listener

def _daemon():
    from helios.voice.daemon import VoiceDaemon
    spoken = []

    class Tts:
        def speak(self, text):
            spoken.append(text)

        def is_speaking(self):
            return False
    d = SimpleNamespace(_announce_q=queue.Queue(), tts=Tts(), _stop=threading.Event(), dormant=False,
                        _states=[], _ANNOUNCE_MAX_AGE=VoiceDaemon._ANNOUNCE_MAX_AGE)
    d._set_state = lambda s, **k: d._states.append(s)
    d._wait_speaking_done = lambda timeout=20.0: None
    return VoiceDaemon, d, spoken


def test_announce_event_is_queued_not_spoken_from_the_sse_thread():
    VoiceDaemon, d, spoken = _daemon()
    VoiceDaemon._on_event(d, "announce", {"text": "Good morning, sir."})
    VoiceDaemon._on_event(d, "announce", {"text": "   "})
    assert d._announce_q.qsize() == 1 and spoken == []


def test_announcements_are_spoken_and_stale_ones_dropped():
    VoiceDaemon, d, spoken = _daemon()
    d._announce_q.put((time.monotonic() - 3600, "yesterday's news"))
    d._announce_q.put((time.monotonic(), "Good morning, sir."))
    assert VoiceDaemon._speak_announcements(d) is True
    assert spoken == ["Good morning, sir."] and d._states[-1] == "idle"


def test_a_pending_announcement_cuts_a_silent_listen_short():
    from helios.voice.daemon import VoiceDaemon

    class Vad:
        started = False

        def reset(self):
            pass

        def feed(self, f):
            return False

    class Mic:
        def flush(self):
            pass

        def read(self, t):
            import numpy as np
            return np.zeros(1280, dtype="int16")
    d = SimpleNamespace(vad=Vad(), mic=Mic(), _stop=threading.Event(), dormant=False, muted=False,
                        live_transcript=False,
                        _mic_level=0.0, _vu_state=None, _announce_q=queue.Queue())
    d._announce_q.put((time.monotonic(), "Good morning"))
    t0 = time.monotonic()
    assert VoiceDaemon._capture(d, start_timeout=8, max_sec=30) is None
    assert time.monotonic() - t0 < 2


# ------------------------------------------------------------------ tool

def test_mcp_morning_briefing_tool(env):
    sched.save_run(_record())
    spec = importlib.util.spec_from_file_location("helios_server_p10", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert "Good morning, sir" in srv.morning_briefing()
    assert srv.morning_briefing(spoken=True).startswith("Good morning, sir.")
    from helios import permissions
    assert permissions.classify("mcp__helios__morning_briefing", {}) == "allow"
